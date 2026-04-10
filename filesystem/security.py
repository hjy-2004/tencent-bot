"""文件系统安全检查：路径校验、黑名单、大小限制、符号链接处理

基于 Claude Code 安全设计:
- 设备文件屏蔽（防止 /dev/zero 等导致进程挂起）
- UNC路径检测（防止 NTLM 凭证泄露）
- 符号链接链跟踪（防止路径穿越攻击）
- 安全路径解析（处理 symlink 和相对路径）
"""

import os
import stat
import logging
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ==================== 设备文件黑名单 ====================

# 会导致进程挂起或阻塞的设备文件
# 检查方式：纯路径检查，无 I/O
BLOCKED_DEVICE_PATHS = {
    # 无限输出 - 永远不会 EOF
    "/dev/zero",
    "/dev/random",
    "/dev/urandom",
    "/dev/full",
    # 阻塞等待输入
    "/dev/stdin",
    "/dev/tty",
    "/dev/console",
    # 无意义读取
    "/dev/stdout",
    "/dev/stderr",
    # fd 别名
    "/dev/fd/0",
    "/dev/fd/1",
    "/dev/fd/2",
}

# ==================== 绝对禁止访问的目录 ====================

BLOCKED_DIRS = {
    r"C:\Windows\System32\config",
    r"C:\Windows\System32\drivers\etc\hosts",
    r"C:\Windows\CSC",
    r"C:\$Recycle.Bin",
    r"C:\System Volume Information",
}

# 禁止的文件扩展名（可执行、系统核心）
BLOCKED_EXTENSIONS = {
    ".exe", ".dll", ".sys", ".msi", ".bat", ".cmd",
    ".ps1", ".vbs", ".scr", ".com", ".drv", ".ocx",
}

# 允许读取的扩展名（白名单模式，空 = 全部允许）
ALLOWED_EXTENSIONS: set[str] = set()

# 允许创建的扩展名（白名单，空 = 全部允许）
ALLOWED_CREATE_EXTENSIONS: set[str] = set()

# 禁止创建的目录路径
BLOCKED_CREATE_DIRS = {
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
}

# 禁止删除的目录
BLOCKED_DELETE_DIRS = {
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
    r"C:\$Recycle.Bin",
    r"C:\System Volume Information",
}

# 禁止删除的文件名
BLOCKED_DELETE_FILES = {
    "pagefile.sys",
    "swapfile.sys",
    "hiberfil.sys",
    "bootmgr",
    "bootnxt",
}

# 单文件最大读取大小（默认 10MB）
MAX_FILE_SIZE = 10 * 1024 * 1024

# 目录列表最大条目数
MAX_DIR_ENTRIES = 500

# 符号链接跟踪最大深度（防止循环符号链接）
MAX_SYMLINK_DEPTH = 40


def _build_sensitive_paths() -> set[str]:
    """构建用户级敏感路径集合"""
    sensitive = set(BLOCKED_DIRS)
    userprofile = os.environ.get("USERPROFILE", "")
    if userprofile:
        sensitive.update({
            os.path.join(userprofile, ".ssh"),
            os.path.join(userprofile, ".aws"),
            os.path.join(userprofile, ".config"),
            os.path.join(userprofile, "AppData", "Local", "Google", "Chrome", "User Data"),
            os.path.join(userprofile, "AppData", "Roaming", "Microsoft", "Credentials"),
        })
    return sensitive


SENSITIVE_PATHS = _build_sensitive_paths()


class SecurityError(Exception):
    """安全检查失败"""
    pass


# ==================== 设备文件检查 ====================

def is_blocked_device_path(path_str: str) -> bool:
    """
    检查是否是阻塞设备文件路径。
    纯路径检查，无 I/O。
    """
    # 直接匹配
    if path_str in BLOCKED_DEVICE_PATHS:
        return True

    # Linux: /proc/self/fd/0-2 和 /proc/<pid>/fd/0-2 是 stdio 的别名
    if path_str.startswith("/proc/"):
        if path_str.endswith("/fd/0") or path_str.endswith("/fd/1") or path_str.endswith("/fd/2"):
            return True

    return False


# ==================== UNC路径检查 ====================

def is_unc_path(path_str: str) -> bool:
    """
    检查是否是 UNC 路径（网络路径）。
    Windows 上访问 UNC 路径可能触发 SMB 认证，导致凭证泄露。
    """
    return path_str.startswith("\\\\") or path_str.startswith("//")


# ==================== 符号链接链跟踪 ====================

def get_paths_for_permission_check(input_path: str) -> list[str]:
    """
    获取需要检查权限的所有路径。
    包括：原始路径、符号链接链中的所有中间目标、最终解析路径。

    例如: test.txt -> /etc/passwd -> /private/etc/passwd
    返回: [test.txt, /etc/passwd, /private/etc/passwd]

    这对安全很重要：/etc/passwd 的拒绝规则应该阻止访问，
    即使文件实际在 /private/etc/passwd（macOS 上的情况）。
    """
    # 防御性展开 ~  notation
    path = input_path
    if path == "~":
        path = os.path.expanduser("~")
    elif path.startswith("~/"):
        path = os.path.join(os.path.expanduser("~"), path[2:])

    path = os.path.normpath(path)

    path_set = set()
    path_set.add(path)

    # UNC 路径：跳过文件系统访问
    if is_unc_path(path):
        return list(path_set)

    # 跟踪符号链接链
    current_path = path
    visited = set()
    depth = 0

    while depth < MAX_SYMLINK_DEPTH:
        # 防止循环符号链接
        if current_path in visited:
            break
        visited.add(current_path)

        try:
            # 检查路径是否存在
            if not os.path.exists(current_path):
                break

            # 获取 lstat（不跟随符号链接）
            st = os.lstat(current_path)

            # 跳过特殊文件类型（FIFO、socket、设备）
            if stat.S_ISFIFO(st.st_mode) or stat.S_ISSOCK(st.st_mode):
                break

            # 如果不是符号链接，停止跟踪
            if not os.path.islink(current_path):
                break

            # 获取符号链接目标
            target = os.readlink(current_path)

            # 相对路径需要相对于符号链接所在目录解析
            if not os.path.isabs(target):
                target = os.path.join(os.path.dirname(current_path), target)

            # 规范化目标路径
            target = os.path.normpath(target)

            # 添加到检查列表
            path_set.add(target)
            current_path = target

        except (OSError, IOError):
            # 跟踪失败，停止
            break

    # 使用 realpath 获取最终解析路径
    try:
        real_path = os.path.realpath(path)
        if real_path != path:
            path_set.add(real_path)
    except (OSError, IOError):
        pass

    return list(path_set)


# ==================== 安全路径解析 ====================

def safe_resolve_path(path_str: str) -> Tuple[str, bool, bool]:
    """
    安全解析文件路径，处理符号链接和错误。

    返回: (resolved_path, is_symlink, is_canonical)

    错误处理策略:
    - 文件不存在：返回原始路径（允许创建文件）
    - 符号链接解析失败：返回原始路径，标记为非符号链接
    - 确保操作可以使用原始路径继续
    """
    # UNC 路径：跳过文件系统访问
    if is_unc_path(path_str):
        return path_str, False, False

    try:
        # 在调用 realpath 之前检查特殊文件类型
        # realpath 可能在 FIFO 上阻塞（等待写入器）
        try:
            st = os.lstat(path_str)
            if stat.S_ISFIFO(st.st_mode) or stat.S_ISSOCK(st.st_mode):
                return path_str, False, False
        except OSError:
            # lstat 失败（文件不存在），返回原始路径
            return path_str, False, False

        resolved = os.path.realpath(path_str)
        return (
            resolved,
            resolved != path_str,
            True,  # realpath 成功：路径是规范的（所有符号链接都已解析）
        )
    except (OSError, IOError):
        # lstat/realpath 失败，返回原始路径
        return path_str, False, False


# ==================== 路径规范化 ====================

def normalize_path(path_str: str) -> Path:
    """标准化路径：解析 ~、..、环境变量，返回绝对 Path"""
    expanded = os.path.expandvars(os.path.expanduser(path_str))
    p = Path(expanded).resolve()
    return p


def normalize_path_for_comparison(path_str: str) -> str:
    """
    规范化路径用于比较。
    Windows 上处理路径大小写不敏感和斜杠/反斜杠问题。
    """
    normalized = os.path.normpath(path_str)

    # Windows：不区分大小写比较
    if os.name == "nt":
        normalized = normalized.replace("/", "\\").lower()

    return normalized


# ==================== 核心安全检查 ====================

def check_path_safety(path: Path, require_exists: bool = True) -> Path:
    """安全检查（读取用），通过则返回 Path，否则抛 SecurityError"""
    path_str = str(path).lower()

    # 1. 设备文件检查
    if is_blocked_device_path(str(path)):
        raise SecurityError(f"禁止访问设备文件: {path}")

    # 2. UNC 路径检查（防止 NTLM 凭证泄露）
    if is_unc_path(str(path)):
        raise SecurityError(f"禁止访问网络路径（UNC）: {path}")

    # 3. 敏感路径检查
    for sensitive in SENSITIVE_PATHS:
        if path_str.startswith(sensitive.lower()):
            raise SecurityError(f"禁止访问敏感路径: {sensitive}")

    # 4. 扩展名检查
    if path.suffix.lower() in BLOCKED_EXTENSIONS:
        raise SecurityError(f"禁止读取可执行文件: {path.suffix}")

    if ALLOWED_EXTENSIONS and path.is_file():
        if path.suffix.lower() not in ALLOWED_EXTENSIONS:
            raise SecurityError(f"文件类型不在白名单中: {path.suffix}")

    # 5. 文件大小检查
    if path.is_file():
        try:
            size = path.stat().st_size
            if size > MAX_FILE_SIZE:
                raise SecurityError(
                    f"文件过大: {size / 1024 / 1024:.1f}MB (上限 {MAX_FILE_SIZE / 1024 / 1024:.0f}MB)"
                )
        except OSError:
            pass

    # 6. 符号链接链安全检查
    all_paths = get_paths_for_permission_check(str(path))
    for check_path in all_paths:
        check_path_lower = check_path.lower()
        for sensitive in SENSITIVE_PATHS:
            if check_path_lower.startswith(sensitive.lower()):
                raise SecurityError(f"符号链接指向敏感路径: {sensitive}")

    return path


def check_write_safety(path: Path) -> Path:
    """写入安全检查，通过则返回 Path，否则抛 SecurityError"""
    path_str = str(path).lower()

    # 1. 设备文件检查
    if is_blocked_device_path(str(path)):
        raise SecurityError(f"禁止写入设备文件: {path}")

    # 2. UNC 路径检查
    if is_unc_path(str(path)):
        raise SecurityError(f"禁止写入网络路径（UNC）: {path}")

    # 3. 检查是否在禁止写入的系统目录中
    for blocked in BLOCKED_CREATE_DIRS:
        if path_str.startswith(blocked.lower()):
            raise SecurityError(f"禁止在系统目录创建文件: {blocked}")

    # 4. 敏感目录检查
    for sensitive in SENSITIVE_PATHS:
        if path_str.startswith(sensitive.lower()):
            raise SecurityError(f"禁止在敏感路径创建文件: {sensitive}")

    # 5. 禁止创建可执行文件
    if path.suffix.lower() in BLOCKED_EXTENSIONS:
        raise SecurityError(f"禁止创建可执行文件: {path.suffix}")

    # 6. 白名单模式
    if ALLOWED_CREATE_EXTENSIONS and path.suffix.lower() not in ALLOWED_CREATE_EXTENSIONS:
        raise SecurityError(f"创建文件类型不在白名单中: {path.suffix}")

    # 7. 符号链接链安全检查
    all_paths = get_paths_for_permission_check(str(path))
    for check_path in all_paths:
        check_path_lower = check_path.lower()
        for sensitive in SENSITIVE_PATHS:
            if check_path_lower.startswith(sensitive.lower()):
                raise SecurityError(f"符号链接指向敏感路径: {sensitive}")

    return path


def check_path_exists(path: Path) -> Path:
    """检查路径是否存在"""
    if not path.exists():
        raise SecurityError(f"路径不存在: {path}")
    return path


def check_delete_safety(path: Path) -> Path:
    """删除安全检查，通过则返回 Path，否则抛 SecurityError"""
    path_str = str(path).lower()

    # 1. 设备文件检查
    if is_blocked_device_path(str(path)):
        raise SecurityError(f"禁止删除设备文件: {path}")

    # 2. UNC 路径检查
    if is_unc_path(str(path)):
        raise SecurityError(f"禁止删除网络路径（UNC）: {path}")

    # 3. 禁止删除根目录
    if path == Path(path.anchor):
        raise SecurityError(f"禁止删除驱动器根目录: {path}")

    # 4. 禁止删除系统目录
    for blocked in BLOCKED_DELETE_DIRS:
        if path_str == blocked.lower():
            raise SecurityError(f"禁止删除系统目录: {blocked}")

    # 5. 敏感目录检查
    for sensitive in SENSITIVE_PATHS:
        if path_str.startswith(sensitive.lower()):
            raise SecurityError(f"禁止删除敏感路径: {sensitive}")

    # 6. 禁止删除系统关键文件
    if path.is_file() and path.name.lower() in {f.lower() for f in BLOCKED_DELETE_FILES}:
        raise SecurityError(f"禁止删除系统关键文件: {path.name}")

    # 7. 禁止删除可执行文件
    if path.is_file() and path.suffix.lower() in BLOCKED_EXTENSIONS:
        raise SecurityError(f"禁止删除可执行文件: {path.suffix}")

    # 8. 符号链接链安全检查
    all_paths = get_paths_for_permission_check(str(path))
    for check_path in all_paths:
        check_path_lower = check_path.lower()
        for sensitive in SENSITIVE_PATHS:
            if check_path_lower.startswith(sensitive.lower()):
                raise SecurityError(f"符号链接指向敏感路径: {sensitive}")

    return path


# ==================== 文件修改时间检查 ====================

def get_file_modification_time(path: Path) -> int:
    """
    获取文件的标准化修改时间（毫秒精度）。
    使用 Math.floor 确保跨文件操作的时间戳比较一致，
    减少因 IDE 文件监视器触发的假阳性（修改文件但不改变内容）。
    """
    return int(path.stat().st_mtime * 1000)


def has_file_changed_since(
    path: Path,
    since_timestamp: int,
    content: Optional[str] = None,
) -> bool:
    """
    检查文件自指定时间戳以来是否已修改。

    Windows 上时间戳可能在内容未实际改变的情况下改变
    （云同步、杀毒软件等）。对于完整读取，使用内容
    比较作为后备方案以避免误报。
    """
    current_mtime = get_file_modification_time(path)

    # 时间戳未变：文件未修改
    if current_mtime <= since_timestamp:
        return False

    # 时间戳改变：如果提供了内容，执行内容比较
    if content is not None:
        try:
            current_content = path.read_text(encoding="utf-8")
            if current_content == content:
                # 内容未变，可能是 Windows 云同步导致的时间戳变化
                return False
        except (UnicodeDecodeError, OSError):
            pass

    # 时间戳改变且内容也改变（或无法读取比较）
    return True
