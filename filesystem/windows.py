"""Windows 文件系统实现

基于 Claude Code 设计:
- 原子写入（临时文件模式）
- 文件修改检测（mtime + 内容双重检测）
- 符号链接安全处理
- 设备文件屏蔽
- 读取状态追踪
"""

import os
import stat
import time
import logging
import string
import ctypes
import shutil
import tempfile
from pathlib import Path
from datetime import datetime
from typing import Optional

from .base import (
    BaseFileSystem, FileEntry, FileContent, SearchResult,
    FileReadState, WriteResult
)
from .security import (
    normalize_path, check_path_safety, check_path_exists,
    check_write_safety, check_delete_safety, SecurityError,
    is_blocked_device_path, is_unc_path, safe_resolve_path,
    get_file_modification_time, has_file_changed_since,
    get_paths_for_permission_check, MAX_DIR_ENTRIES,
)

logger = logging.getLogger(__name__)

# Windows 常见编码列表（读取时按顺序尝试）
ENCODING_CANDIDATES = ["utf-8", "gbk", "gb2312", "gb18030", "latin-1"]

# 文本文件扩展名
TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".xml", ".html", ".htm",
    ".css", ".scss", ".less", ".csv", ".log", ".sql", ".sh",
    ".bat", ".cmd", ".ps1", ".env", ".gitignore", ".dockerfile",
    ".rs", ".go", ".java", ".c", ".cpp", ".h", ".hpp", ".cs",
    ".rb", ".php", ".lua", ".r", ".m", ".swift", ".kt",
}


class WindowsFileSystem(BaseFileSystem):
    """Windows 文件系统读取实现"""

    def __init__(self):
        self._max_read_bytes = 2 * 1024 * 1024  # 单文件最大读取 2MB 文本
        self._read_states: dict[str, FileReadState] = {}  # 读取状态追踪

    # ==================== 读取状态追踪 ====================

    def get_read_state(self, path: str) -> Optional[FileReadState]:
        """获取文件读取状态"""
        p = normalize_path(path)
        key = str(p)
        return self._read_states.get(key)

    def update_read_state(self, path: str, state: FileReadState) -> None:
        """更新文件读取状态"""
        p = normalize_path(path)
        key = str(p)
        self._read_states[key] = state

    def clear_read_state(self, path: str) -> None:
        """清除文件读取状态"""
        p = normalize_path(path)
        key = str(p)
        self._read_states.pop(key, None)

    # ==================== 安全路径解析 ====================

    def safe_resolve_path(self, path: str) -> tuple[str, bool, bool]:
        """
        安全解析文件路径，处理符号链接。

        Returns:
            (resolved_path, is_symlink, is_canonical)
        """
        return safe_resolve_path(path)

    def paths_equal(self, path1: str, path2: str) -> bool:
        """比较两个路径是否指向同一文件"""
        try:
            p1 = normalize_path(path1)
            p2 = normalize_path(path2)
            return str(p1).lower() == str(p2).lower()
        except:
            return False

    # ==================== 文件修改检测 ====================

    def check_file_modified(
        self,
        path: str,
        since_timestamp: int,
        content: Optional[str] = None,
    ) -> bool:
        """检查文件是否已修改"""
        p = normalize_path(path)
        if not p.exists():
            return True  # 文件不存在，视为已修改
        return has_file_changed_since(p, since_timestamp, content)

    # ==================== 驱动器 ====================

    async def get_drives(self) -> list[str]:
        """获取 Windows 可用盘符"""
        drives = []
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            if bitmask & (1 << i):
                drives.append(f"{letter}:\\")
        return drives

    async def get_disk_info(self) -> list[dict]:
        """获取各盘符信息"""
        info = []
        for drive in await self.get_drives():
            try:
                free = ctypes.c_ulonglong(0)
                total = ctypes.c_ulonglong(0)
                ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                    drive, None, ctypes.byref(total), ctypes.byref(free)
                )
                total_gb = total.value / (1024**3)
                free_gb = free.value / (1024**3)
                used_gb = total_gb - free_gb
                pct = (used_gb / total_gb * 100) if total_gb > 0 else 0

                info.append({
                    "drive": drive,
                    "total_gb": round(total_gb, 1),
                    "used_gb": round(used_gb, 1),
                    "free_gb": round(free_gb, 1),
                    "usage_pct": round(pct, 1),
                })
            except Exception as e:
                logger.warning(f"获取 {drive} 信息失败: {e}")
                info.append({"drive": drive, "error": str(e)})
        return info

    # ==================== 列出目录 ====================

    async def list_directory(self, path: str) -> list[FileEntry]:
        """列出目录内容"""
        p = normalize_path(path)
        check_path_safety(p)
        check_path_exists(p)

        if not p.is_dir():
            # 如果是文件，返回该文件的单条信息
            entry = self._path_to_entry(p)
            return [entry]

        entries = []
        count = 0

        try:
            for item in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if count >= MAX_DIR_ENTRIES:
                    entries.append(FileEntry(
                        name=f"... 还有更多文件 (已截断到 {MAX_DIR_ENTRIES} 条)",
                        path="",
                        is_dir=False,
                    ))
                    break

                try:
                    check_path_safety(item)
                    entries.append(self._path_to_entry(item))
                    count += 1
                except SecurityError:
                    # 跳过黑名单文件，不中断
                    continue

        except PermissionError:
            raise SecurityError(f"无权访问目录: {path}")

        return entries

    def _path_to_entry(self, p: Path) -> FileEntry:
        """Path → FileEntry"""
        st = p.stat()
        modified = datetime.fromtimestamp(st.st_mtime)

        # 权限
        perms = []
        perms.append("R" if os.access(p, os.R_OK) else "-")
        perms.append("W" if os.access(p, os.W_OK) else "-")
        perms.append("X" if os.access(p, os.X_OK) else "-")

        # 只读标记
        readonly = bool(st.st_file_attributes & stat.FILE_ATTRIBUTE_READONLY) if hasattr(st, 'st_file_attributes') else False

        # 是否为符号链接
        is_symlink = p.is_symlink()

        return FileEntry(
            name=p.name,
            path=str(p),
            is_dir=p.is_dir(),
            size=st.st_size if p.is_file() else 0,
            modified=modified,
            extension=p.suffix.lower() if p.is_file() else "",
            permissions="".join(perms) + (" [只读]" if readonly else ""),
            is_symlink=is_symlink,
        )

    # ==================== 读取文件 ====================

    async def read_file(
        self,
        path: str,
        encoding: str = "utf-8",
        max_lines: int = 500,
        start_line: int = 0,
    ) -> FileContent:
        """读取文本文件"""
        p = normalize_path(path)
        check_path_safety(p)
        check_path_exists(p)

        if p.is_dir():
            raise SecurityError(f"路径是目录，不是文件: {path}")

        size = p.stat().st_size
        if size > self._max_read_bytes:
            raise SecurityError(
                f"文件过大无法读取全文: {size / 1024 / 1024:.1f}MB"
            )

        # 自动检测编码
        content, detected_enc = self._read_with_encoding(p, encoding)

        lines = content.splitlines()
        total_lines = len(lines)

        # 截取指定范围
        end_line = start_line + max_lines
        selected = lines[start_line:end_line]
        truncated = end_line < total_lines

        # 获取修改时间
        mtime = get_file_modification_time(p)

        # 更新读取状态
        read_state = FileReadState(
            content=content,
            timestamp=mtime,
            offset=start_line if start_line > 0 else None,
            limit=max_lines if max_lines < total_lines else None,
            is_partial_view=(start_line > 0 or truncated),
        )
        self.update_read_state(path, read_state)

        return FileContent(
            path=str(p),
            name=p.name,
            content="\n".join(selected),
            encoding=detected_enc,
            size=size,
            lines=total_lines,
            extension=p.suffix.lower(),
            truncated=truncated,
            mtime=mtime,
        )

    async def read_binary(self, path: str) -> tuple[bytes, str]:
        """读取二进制文件"""
        p = normalize_path(path)
        check_path_safety(p)
        check_path_exists(p)

        if p.is_dir():
            raise SecurityError(f"路径是目录: {path}")

        size = p.stat().st_size
        if size > self._max_read_bytes:
            raise SecurityError(f"文件过大: {size / 1024 / 1024:.1f}MB")

        data = p.read_bytes()
        mime = self._guess_mime(p.suffix)
        return data, mime

    def _read_with_encoding(self, path: Path, preferred: str) -> tuple[str, str]:
        """尝试多种编码读取文件"""
        encodings = [preferred] + [e for e in ENCODING_CANDIDATES if e != preferred]

        for enc in encodings:
            try:
                content = path.read_text(encoding=enc)
                return content, enc
            except (UnicodeDecodeError, LookupError):
                continue

        # 全部失败，用 latin-1（永不报错）
        content = path.read_text(encoding="latin-1")
        return content, "latin-1"

    def _guess_mime(self, ext: str) -> str:
        """简单 MIME 猜测"""
        mime_map = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
            ".pdf": "application/pdf", ".zip": "application/zip",
            ".json": "application/json", ".xml": "application/xml",
            ".csv": "text/csv", ".html": "text/html", ".css": "text/css",
        }
        return mime_map.get(ext.lower(), "application/octet-stream")

    # ==================== 搜索 ====================

    async def search_files(
            self,
            directory: str,
            pattern: str,
            content_search: bool = False,
            max_results: int = 500,
    ) -> list[SearchResult]:
        """搜索文件，支持 glob 通配符模式（* 匹配任意字符）"""
        import fnmatch

        p = normalize_path(directory)
        check_path_safety(p)
        check_path_exists(p)

        results = []

        # 支持 glob 通配符（* 匹配任意）
        use_glob = ("*" in pattern or "?" in pattern)

        # 提取纯扩展名
        ext_raw = pattern.lstrip("*.").lower()

        # 扩展名列表
        raw_pattern = pattern
        pattern_parts = [p.strip().lower() for p in raw_pattern.split("|")]

        # 传入的是文件，只搜这一个文件
        if p.is_file():
            if content_search and p.suffix.lower() in TEXT_EXTENSIONS:
                try:
                    if p.stat().st_size > self._max_read_bytes:
                        return results
                    content, _ = self._read_with_encoding(p, "utf-8")
                    for i, line in enumerate(content.splitlines(), 1):
                        line_lower = line.lower()
                        matched = False
                        if use_glob:
                            for pp in pattern_parts:
                                if fnmatch.fnmatch(p.name.lower(), f"*{pp}*"):
                                    matched = True
                                    break
                        else:
                            for pp in pattern_parts:
                                if pp in line_lower:
                                    matched = True
                                    break
                        if matched:
                            match_text = line.strip()[:120]
                            results.append(SearchResult(
                                path=str(p),
                                name=p.name,
                                match_line=i,
                                match_text=match_text,
                                is_dir=False,
                            ))
                except Exception as e:
                    logger.error(f"搜索文件内容失败: {e}")
            elif not content_search:
                fname_lower = p.name.lower()
                matched = False
                if use_glob:
                    for pp in pattern_parts:
                        if fnmatch.fnmatch(fname_lower, f"*{pp}*"):
                            matched = True
                            break
                else:
                    for pp in pattern_parts:
                        if pp in fname_lower:
                            matched = True
                            break
                if matched:
                    results.append(SearchResult(
                        path=str(p),
                        name=p.name,
                        is_dir=False,
                    ))
            return results

        # 传入的是目录，递归搜索
        if not p.is_dir():
            raise SecurityError(f"搜索路径不是目录: {directory}")

        try:
            for root, dirs, files in os.walk(str(p)):
                if len(results) >= max_results:
                    break

                root_path = Path(root)

                try:
                    check_path_safety(root_path)
                except SecurityError:
                    dirs.clear()
                    continue

                depth = len(root_path.relative_to(p).parts)
                if depth > self.MAX_SEARCH_DEPTH:
                    dirs.clear()
                    continue

                for fname in files:
                    if len(results) >= max_results:
                        break

                    fpath = root_path / fname
                    fname_lower = fname.lower()

                    try:
                        check_path_safety(fpath)
                    except SecurityError:
                        continue

                    # 匹配逻辑
                    matched = False
                    if use_glob:
                        for pp in pattern_parts:
                            if fnmatch.fnmatch(fname_lower, f"*{pp}*"):
                                matched = True
                                break
                    else:
                        for pp in pattern_parts:
                            if pp in fname_lower:
                                matched = True
                                break

                    if matched:
                        results.append(SearchResult(
                            path=str(fpath),
                            name=fname,
                            is_dir=False,
                        ))
                        continue

                    if content_search and fpath.suffix.lower() in TEXT_EXTENSIONS:
                        try:
                            if fpath.stat().st_size > self._max_read_bytes:
                                continue
                            content, _ = self._read_with_encoding(fpath, "utf-8")
                            for i, line in enumerate(content.splitlines(), 1):
                                line_lower = line.lower()
                                line_matched = False
                                for pp in pattern_parts:
                                    if pp in line_lower:
                                        line_matched = True
                                        break
                                if line_matched:
                                    match_text = line.strip()[:120]
                                    results.append(SearchResult(
                                        path=str(fpath),
                                        name=fname,
                                        match_line=i,
                                        match_text=match_text,
                                        is_dir=False,
                                    ))
                                    break
                        except Exception:
                            continue

        except PermissionError:
            logger.warning(f"部分目录无权访问: {directory}")

        return results

    # ==================== 原子写入 ====================

    def write_file_atomic(
        self,
        path: str,
        content: str,
        encoding: str = "utf-8",
        backup: bool = True,
    ) -> WriteResult:
        """
        原子写入文件（临时文件模式）。

        1. 写入临时文件
        2. 保留原文件权限
        3. 原子重命名覆盖原文件

        Returns:
            WriteResult: 写入结果
        """
        p = normalize_path(path)
        check_write_safety(p)

        # 确保父目录存在
        parent = p.parent
        if not parent.exists():
            raise SecurityError(f"父目录不存在: {parent}")

        original_content = None
        backup_path = None

        # 如果文件存在，读取原始内容（用于可能的回滚）
        if p.exists():
            try:
                original_content = p.read_text(encoding=encoding)
            except (UnicodeDecodeError, OSError):
                original_content = None

            # 创建备份
            if backup and original_content is not None:
                try:
                    backup_dir = parent / ".backup"
                    backup_dir.mkdir(exist_ok=True)
                    backup_path = backup_dir / f"{p.name}.{int(time.time())}.bak"
                    shutil.copy2(p, backup_path)
                    logger.info(f"备份已创建: {backup_path}")
                except (OSError, PermissionError) as e:
                    logger.warning(f"创建备份失败: {e}")
                    backup_path = None

        # 生成临时文件路径
        temp_fd, temp_path = tempfile.mkstemp(
            suffix=".tmp",
            prefix=f".{p.name}.",
            dir=str(p.parent),
        )
        temp_file = Path(temp_path)

        try:
            # 写入临时文件
            with os.fdopen(temp_fd, 'w', encoding=encoding) as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())  # 确保写入磁盘

            # 如果原文件存在，复制权限
            if p.exists():
                try:
                    src_stat = p.stat()
                    os.chmod(temp_path, src_stat.st_mode)
                except (OSError, PermissionError):
                    pass  # 忽略权限设置失败

            # 原子重命名（Windows 上会覆盖目标文件）
            os.replace(temp_path, str(p))

            # 计算行数变化
            old_lines = (original_content or "").count("\n")
            new_lines = content.count("\n")
            lines_changed = abs(new_lines - old_lines)

            logger.info(f"文件已原子写入: {p}")

            return WriteResult(
                path=str(p),
                success=True,
                original_content=original_content,
                backup_path=str(backup_path) if backup_path else None,
                lines_changed=lines_changed,
            )

        except Exception as e:
            # 清理临时文件
            try:
                if temp_file.exists():
                    temp_file.unlink()
            except:
                pass

            raise SecurityError(f"原子写入失败: {e}")

    # ==================== 创建文件 ====================

    async def create_file(
        self,
        path: str,
        content: str = "",
        encoding: str = "utf-8",
        overwrite: bool = False,
    ) -> FileEntry:
        """创建文件"""
        p = normalize_path(path)
        check_write_safety(p)

        # 检查父目录是否存在
        parent = p.parent
        if not parent.exists():
            raise SecurityError(f"父目录不存在: {parent}")

        # 检查是否已存在
        if p.exists() and not overwrite:
            raise SecurityError(f"文件已存在（使用 overwrite=True 覆盖）: {p}")

        # 使用原子写入
        if p.exists() and overwrite:
            result = self.write_file_atomic(path, content, encoding, backup=True)
            if not result.success:
                raise SecurityError(f"写入失败")
            return self._path_to_entry(p)

        # 新建文件
        try:
            p.write_text(content, encoding=encoding)
        except PermissionError:
            raise SecurityError(f"无权写入: {p}")
        except OSError as e:
            raise SecurityError(f"写入失败: {e}")

        logger.info(f"文件已创建: {p} ({len(content.encode(encoding))} bytes, {encoding})")
        return self._path_to_entry(p)

    async def create_directory(self, path: str) -> FileEntry:
        """创建目录（递归创建）"""
        p = normalize_path(path)
        check_write_safety(p)

        if p.exists():
            raise SecurityError(f"路径已存在: {path}")

        try:
            p.mkdir(parents=True, exist_ok=False)
        except PermissionError:
            raise SecurityError(f"无权创建目录: {p}")
        except OSError as e:
            raise SecurityError(f"创建目录失败: {e}")

        logger.info(f"目录已创建: {p}")
        return self._path_to_entry(p)

    # ==================== 删除文件 ====================

    async def delete_file(self, path: str) -> str:
        """删除单个文件"""
        p = normalize_path(path)
        check_path_safety(p)
        check_path_exists(p)
        check_delete_safety(p)

        if p.is_dir():
            raise SecurityError(f"路径是目录，请使用 delete_directory: {p}")

        deleted_path = str(p)
        try:
            p.unlink()
        except PermissionError:
            raise SecurityError(f"无权删除（文件可能被占用）: {p}")
        except OSError as e:
            raise SecurityError(f"删除失败: {e}")

        # 清除读取状态
        self.clear_read_state(path)

        logger.info(f"文件已删除: {deleted_path}")
        return deleted_path

    async def delete_directory(self, path: str, recursive: bool = False) -> str:
        """删除目录"""
        p = normalize_path(path)
        check_path_safety(p)
        check_path_exists(p)
        check_delete_safety(p)

        if p.is_file():
            raise SecurityError(f"路径是文件，请使用 delete_file: {p}")

        # 非空目录保护
        if not recursive and any(p.iterdir()):
            raise SecurityError(f"目录非空，使用 --recursive 递归删除: {p}")

        deleted_path = str(p)
        try:
            if recursive:
                shutil.rmtree(str(p))
            else:
                p.rmdir()
        except PermissionError:
            raise SecurityError(f"无权删除（目录中文件可能被占用）: {p}")
        except OSError as e:
            raise SecurityError(f"删除失败: {e}")

        # 清除读取状态
        self.clear_read_state(path)

        logger.info(f"目录已删除: {deleted_path}")
        return deleted_path

    # ==================== 修改文件 ====================

    async def edit_file(
        self,
        path: str,
        mode: str,
        content: str = "",
        line: int = 0,
        old_text: str = "",
        new_text: str = "",
        encoding: str = "utf-8",
    ) -> FileContent:
        """修改文件（使用原子写入保证安全）"""
        p = normalize_path(path)
        check_path_safety(p)
        check_path_exists(p)

        if p.is_dir():
            raise SecurityError(f"路径是目录，不是文件: {path}")

        check_write_safety(p)

        # 检查文件是否已被外部修改
        read_state = self.get_read_state(path)
        if read_state and not read_state.is_partial_view:
            if self.check_file_modified(path, read_state.timestamp, read_state.content):
                raise SecurityError(
                    "文件已被修改（外部编辑或云同步），请重新读取后再操作"
                )

        # 读取原始内容
        original, detected_enc = self._read_with_encoding(p, encoding)
        lines = original.splitlines()

        if mode == "append":
            if original and not original.endswith("\n"):
                original += "\n"
            original += content + "\n"

        elif mode == "replace":
            original = content
            if not content.endswith("\n"):
                original += "\n"

        elif mode == "insert":
            if line < 1 or line > len(lines) + 1:
                raise SecurityError(f"行号超出范围: {line} (文件共 {len(lines)} 行)")
            insert_lines = content.splitlines()
            lines[line - 1:line - 1] = insert_lines
            original = "\n".join(lines) + "\n"

        elif mode == "delete-line":
            line_str = str(line)
            if "-" in line_str:
                parts = line_str.split("-")
                start = int(parts[0])
                end = int(parts[1])
            else:
                start = end = int(line_str)

            if start < 1 or end > len(lines) or start > end:
                raise SecurityError(
                    f"行号超出范围: {start}-{end} (文件共 {len(lines)} 行)"
                )

            del lines[start - 1:end]
            original = "\n".join(lines) + "\n"

        elif mode == "replace-line":
            line_str = str(line)
            if "-" in line_str:
                parts = line_str.split("-")
                start = int(parts[0])
                end = int(parts[1])
            else:
                start = end = int(line_str)

            if start < 1 or end > len(lines) or start > end:
                raise SecurityError(
                    f"行号超出范围: {start}-{end} (文件共 {len(lines)} 行)"
                )

            replace_lines = content.splitlines()
            lines[start - 1:end] = replace_lines
            original = "\n".join(lines) + "\n"

        elif mode == "replace-text":
            if not old_text:
                raise SecurityError("replace-text 模式需要 --old 参数")
            count = original.count(old_text)
            if count == 0:
                raise SecurityError(f"未找到匹配文本: {old_text}")
            original = original.replace(old_text, new_text)

        else:
            raise SecurityError(f"不支持的编辑模式: {mode}")

        # 使用原子写入
        try:
            result = self.write_file_atomic(str(p), original, detected_enc, backup=True)
            if not result.success:
                raise SecurityError("原子写入失败")
        except PermissionError:
            raise SecurityError(f"无权写入（文件可能被占用）: {p}")
        except OSError as e:
            raise SecurityError(f"写入失败: {e}")

        logger.info(f"文件已修改: {p} mode={mode}")

        # 获取新的修改时间
        mtime = get_file_modification_time(p)

        # 返回修改后的结果
        new_lines = original.splitlines()
        return FileContent(
            path=str(p),
            name=p.name,
            content=original,
            encoding=detected_enc,
            size=len(original.encode(detected_enc)),
            lines=len(new_lines),
            extension=p.suffix.lower(),
            truncated=False,
            mtime=mtime,
        )
