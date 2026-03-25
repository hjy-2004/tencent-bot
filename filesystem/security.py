"""文件系统安全检查：路径校验、黑名单、大小限制"""

import os
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ==================== 黑名单 ====================

# 绝对禁止访问的目录
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

# ★ 新增：允许创建的扩展名（白名单，空 = 全部允许）
ALLOWED_CREATE_EXTENSIONS: set[str] = set()

# ★ 新增：禁止创建的目录路径
BLOCKED_CREATE_DIRS = {
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
}

# ★ 新增：禁止删除的目录
BLOCKED_DELETE_DIRS = {
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
    r"C:\$Recycle.Bin",
    r"C:\System Volume Information",
}

# ★ 新增：禁止删除的文件名
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


def normalize_path(path_str: str) -> Path:
    """标准化路径：解析 ~、..、环境变量，返回绝对 Path"""
    expanded = os.path.expandvars(os.path.expanduser(path_str))
    p = Path(expanded).resolve()
    return p


def check_path_safety(path: Path) -> Path:
    """安全检查（读取用），通过则返回 Path，否则抛 SecurityError"""
    path_str = str(path).lower()

    for sensitive in SENSITIVE_PATHS:
        if path_str.startswith(sensitive.lower()):
            raise SecurityError(f"禁止访问敏感路径: {sensitive}")

    if path.suffix.lower() in BLOCKED_EXTENSIONS:
        raise SecurityError(f"禁止读取可执行文件: {path.suffix}")

    if ALLOWED_EXTENSIONS and path.is_file():
        if path.suffix.lower() not in ALLOWED_EXTENSIONS:
            raise SecurityError(f"文件类型不在白名单中: {path.suffix}")

    if path.is_file():
        size = path.stat().st_size
        if size > MAX_FILE_SIZE:
            raise SecurityError(
                f"文件过大: {size / 1024 / 1024:.1f}MB (上限 {MAX_FILE_SIZE / 1024 / 1024:.0f}MB)"
            )

    return path


# ★ 新增：写入安全检查
def check_write_safety(path: Path) -> Path:
    """写入安全检查，通过则返回 Path，否则抛 SecurityError"""
    path_str = str(path).lower()

    # 1. 检查是否在禁止写入的系统目录中
    for blocked in BLOCKED_CREATE_DIRS:
        if path_str.startswith(blocked.lower()):
            raise SecurityError(f"禁止在系统目录创建文件: {blocked}")

    # 2. 检查敏感目录
    for sensitive in SENSITIVE_PATHS:
        if path_str.startswith(sensitive.lower()):
            raise SecurityError(f"禁止在敏感路径创建文件: {sensitive}")

    # 3. 禁止创建可执行文件
    if path.suffix.lower() in BLOCKED_EXTENSIONS:
        raise SecurityError(f"禁止创建可执行文件: {path.suffix}")

    # 4. 白名单模式
    if ALLOWED_CREATE_EXTENSIONS and path.suffix.lower() not in ALLOWED_CREATE_EXTENSIONS:
        raise SecurityError(f"创建文件类型不在白名单中: {path.suffix}")

    return path


def check_path_exists(path: Path) -> Path:
    """检查路径是否存在"""
    if not path.exists():
        raise SecurityError(f"路径不存在: {path}")
    return path

# ★ 新增：删除安全检查
def check_delete_safety(path: Path) -> Path:
    """删除安全检查，通过则返回 Path，否则抛 SecurityError"""
    path_str = str(path).lower()

    # 1. 禁止删除根目录
    if path == Path(path.anchor):
        raise SecurityError(f"禁止删除驱动器根目录: {path}")

    # 2. 禁止删除系统目录
    for blocked in BLOCKED_DELETE_DIRS:
        if path_str == blocked.lower():
            raise SecurityError(f"禁止删除系统目录: {blocked}")

    # 3. 敏感目录
    for sensitive in SENSITIVE_PATHS:
        if path_str.startswith(sensitive.lower()):
            raise SecurityError(f"禁止删除敏感路径: {sensitive}")

    # 4. 禁止删除系统关键文件
    if path.is_file() and path.name.lower() in {f.lower() for f in BLOCKED_DELETE_FILES}:
        raise SecurityError(f"禁止删除系统关键文件: {path.name}")

    # 5. 禁止删除可执行文件（额外保护）
    if path.is_file() and path.suffix.lower() in BLOCKED_EXTENSIONS:
        raise SecurityError(f"禁止删除可执行文件: {path.suffix}")

    return path

