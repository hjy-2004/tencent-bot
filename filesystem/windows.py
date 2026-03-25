"""Windows 文件系统实现"""

import os
import stat
import time
import logging
import string
import ctypes
from pathlib import Path
from datetime import datetime
from typing import Optional

from .base import BaseFileSystem, FileEntry, FileContent, SearchResult
from .security import (
    normalize_path, check_path_safety, check_path_exists,
    SecurityError, MAX_DIR_ENTRIES,
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

        return FileEntry(
            name=p.name,
            path=str(p),
            is_dir=p.is_dir(),
            size=st.st_size if p.is_file() else 0,
            modified=modified,
            extension=p.suffix.lower() if p.is_file() else "",
            permissions="".join(perms) + (" [只读]" if readonly else ""),
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

        return FileContent(
            path=str(p),
            name=p.name,
            content="\n".join(selected),
            encoding=detected_enc,
            size=size,
            lines=total_lines,
            extension=p.suffix.lower(),
            truncated=truncated,
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

        # ★ 改进：支持 glob 通配符（* 匹配任意），同时保留子串匹配
        # 如果 pattern 含 * 或 ? 则走 glob，否则走子串匹配
        use_glob = ("*" in pattern or "?" in pattern)

        # ★ 改进：提取纯扩展名（去掉 leading dot，方便比较）
        # 例如 ".jpg" 或 "jpg" → "jpg"
        ext_raw = pattern.lstrip("*.").lower()

        # 如果 pattern 看起来像扩展名列表（如 "jpg|jpeg|png"），拆成集合
        raw_pattern = pattern
        pattern_parts = [p.strip().lower() for p in raw_pattern.split("|")]

        # ★ 新增：如果传入的是文件，只搜这一个文件
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

        # 原有逻辑：传入的是目录，递归搜索
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
                if depth > 6:
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

                    # ★ 改进的匹配逻辑
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

    # ==================== 创建文件 ====================

    async def create_file(
        self,
        path: str,
        content: str = "",
        encoding: str = "utf-8",
        overwrite: bool = False,
    ) -> FileEntry:
        """创建文件"""
        from .security import check_write_safety

        p = normalize_path(path)
        check_write_safety(p)

        # 检查父目录是否存在
        parent = p.parent
        if not parent.exists():
            raise SecurityError(f"父目录不存在: {parent}")

        # 检查是否已存在
        if p.exists() and not overwrite:
            raise SecurityError(f"文件已存在（使用 overwrite=True 覆盖）: {p}")

        # 写入
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
        from .security import check_write_safety

        p = normalize_path(path)
        check_write_safety(p)

        if p.exists():
            raise SecurityError(f"路径已存在: {p}")

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
        from .security import check_delete_safety

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

        logger.info(f"文件已删除: {deleted_path}")
        return deleted_path

    async def delete_directory(self, path: str, recursive: bool = False) -> str:
        """删除目录"""
        from .security import check_delete_safety
        import shutil

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
        """修改文件"""
        from .security import check_write_safety

        p = normalize_path(path)
        check_path_safety(p)
        check_path_exists(p)

        if p.is_dir():
            raise SecurityError(f"路径是目录，不是文件: {path}")

        check_write_safety(p)

        # 读取原始内容
        original, detected_enc = self._read_with_encoding(p, encoding)
        lines = original.splitlines()

        if mode == "append":
            # ★ 追加到末尾
            if original and not original.endswith("\n"):
                original += "\n"
            original += content + "\n"

        elif mode == "replace":
            # ★ 替换整个文件
            original = content
            if not content.endswith("\n"):
                original += "\n"

        elif mode == "insert":
            # ★ 在指定行之前插入（1-based）
            if line < 1 or line > len(lines) + 1:
                raise SecurityError(f"行号超出范围: {line} (文件共 {len(lines)} 行)")
            insert_lines = content.splitlines()
            lines[line - 1:line - 1] = insert_lines
            original = "\n".join(lines) + "\n"


        elif mode == "delete-line":

            # ★ 删除指定行（支持范围如 "3-5"）

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

            # ★ 替换指定行（支持范围如 "3-5"）

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
            # ★ 全文查找替换
            if not old_text:
                raise SecurityError("replace-text 模式需要 --old 参数")
            count = original.count(old_text)
            if count == 0:
                raise SecurityError(f"未找到匹配文本: {old_text}")
            original = original.replace(old_text, new_text)

        else:
            raise SecurityError(f"不支持的编辑模式: {mode}")

        # 写入
        try:
            p.write_text(original, encoding=detected_enc)
        except PermissionError:
            raise SecurityError(f"无权写入（文件可能被占用）: {p}")
        except OSError as e:
            raise SecurityError(f"写入失败: {e}")

        logger.info(f"文件已修改: {p} mode={mode}")

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
        )



