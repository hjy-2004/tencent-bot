"""文件系统抽象基类 — 后续扩展 Linux/macOS 只需继承此类"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


@dataclass
class FileEntry:
    """文件/目录条目"""
    name: str
    path: str
    is_dir: bool
    size: int = 0                    # bytes
    modified: Optional[datetime] = None
    extension: str = ""
    permissions: str = ""            # rwx / readonly 等

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "is_dir": self.is_dir,
            "size": self.size,
            "size_human": self._human_size(),
            "modified": self.modified.isoformat() if self.modified else None,
            "extension": self.extension,
            "permissions": self.permissions,
        }

    def _human_size(self) -> str:
        if self.is_dir:
            return "-"
        for unit in ("B", "KB", "MB", "GB"):
            if self.size < 1024:
                return f"{self.size:.1f} {unit}"
            self.size /= 1024  # type: ignore
        return f"{self.size:.1f} TB"  # type: ignore


@dataclass
class FileContent:
    """文件内容"""
    path: str
    name: str
    content: str                     # 文本内容
    encoding: str = "utf-8"
    size: int = 0
    lines: int = 0
    extension: str = ""
    truncated: bool = False          # 是否被截断

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "name": self.name,
            "content": self.content,
            "encoding": self.encoding,
            "size": self.size,
            "lines": self.lines,
            "extension": self.extension,
            "truncated": self.truncated,
        }


@dataclass
class SearchResult:
    """搜索结果"""
    path: str
    name: str
    match_line: int = 0
    match_text: str = ""
    is_dir: bool = False

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "name": self.name,
            "match_line": self.match_line,
            "match_text": self.match_text,
            "is_dir": self.is_dir,
        }


class BaseFileSystem(ABC):
    """
    文件系统抽象基类

    扩展新系统时，实现以下方法即可：
    - list_directory
    - read_file
    - read_binary
    - search_files
    - get_disk_info
    - get_drives（Windows 特有，Linux 返回挂载点）
    - create_file
    - create_directory
    - delete_file
    - delete_directory
    - edit_file
    """

    @abstractmethod
    async def list_directory(self, path: str) -> list[FileEntry]:
        """列出目录内容"""
        ...

    @abstractmethod
    async def read_file(
        self,
        path: str,
        encoding: str = "utf-8",
        max_lines: int = 500,
        start_line: int = 0,
    ) -> FileContent:
        """读取文本文件内容"""
        ...

    @abstractmethod
    async def read_binary(self, path: str) -> tuple[bytes, str]:
        """读取二进制文件，返回 (数据, mime_type)"""
        ...

    @abstractmethod
    async def search_files(
        self,
        directory: str,
        pattern: str,
        content_search: bool = False,
        max_results: int = 50,
    ) -> list[SearchResult]:
        """搜索文件（按名称或内容）"""
        ...

    @abstractmethod
    async def get_disk_info(self) -> list[dict]:
        """获取磁盘/分区信息"""
        ...

    @abstractmethod
    async def get_drives(self) -> list[str]:
        """获取可用驱动器/挂载点列表"""
        ...

    @abstractmethod
    async def create_file(
        self,
        path: str,
        content: str = "",
        encoding: str = "utf-8",
        overwrite: bool = False,
    ) -> "FileEntry":
        """创建文件"""
        ...

    @abstractmethod
    async def create_directory(self, path: str) -> "FileEntry":
        """创建目录"""
        ...

    @abstractmethod
    async def delete_file(self, path: str) -> str:
        """删除文件，返回被删除的路径"""
        ...

    @abstractmethod
    async def delete_directory(self, path: str, recursive: bool = False) -> str:
        """删除目录，返回被删除的路径"""
        ...

    @abstractmethod
    async def edit_file(
        self,
        path: str,
        mode: str,
        content: str = "",
        line: int = 0,
        old_text: str = "",
        new_text: str = "",
        encoding: str = "utf-8",
    ) -> "FileContent":
        """
        修改文件
        mode: append | replace | insert | delete-line | replace-line | replace-text
        """
        ...



