"""文件系统抽象基类 — 后续扩展 Linux/macOS 只需继承此类

基于 Claude Code 设计:
- 原子写入支持（临时文件模式）
- 文件修改检测（mtime + 内容双重检测）
- 文件历史追踪接口
- 安全路径解析
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Callable
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
    is_symlink: bool = False        # 是否为符号链接

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
            "is_symlink": self.is_symlink,
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
    mtime: int = 0                   # 修改时间（毫秒时间戳）
    is_binary: bool = False          # 是否为二进制文件

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
            "mtime": self.mtime,
            "is_binary": self.is_binary,
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


@dataclass
class FileReadState:
    """
    文件读取状态追踪（用于检测文件是否被外部修改）
    """
    content: str                      # 读取时的内容
    timestamp: int                    # 读取时的 mtime（毫秒）
    offset: Optional[int] = None      # 读取起始行（None 表示完整读取）
    limit: Optional[int] = None       # 读取限制行数
    is_partial_view: bool = False     # 是否为部分读取

    def to_dict(self) -> dict:
        return {
            "content": self.content,
            "timestamp": self.timestamp,
            "offset": self.offset,
            "limit": self.limit,
            "is_partial_view": self.is_partial_view,
        }


@dataclass
class WriteResult:
    """写入结果"""
    path: str
    success: bool
    original_content: Optional[str] = None  # 写入前的内容（用于撤销）
    backup_path: Optional[str] = None        # 备份文件路径
    lines_changed: int = 0                   # 改变的行数

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "success": self.success,
            "original_content": self.original_content,
            "backup_path": self.backup_path,
            "lines_changed": self.lines_changed,
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

    新增安全特性：
    - 原子写入（临时文件模式）
    - 文件修改检测（mtime + 内容比较）
    - 符号链接安全处理
    - 设备文件屏蔽
    """

    # 子类可以设置此值来限制递归搜索深度
    MAX_SEARCH_DEPTH: int = 10

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
    ) -> FileEntry:
        """创建文件"""
        ...

    @abstractmethod
    async def create_directory(self, path: str) -> FileEntry:
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
    ) -> FileContent:
        """
        修改文件
        mode: append | replace | insert | delete-line | replace-line | replace-text
        """
        ...

    # ==================== 新增：原子写入 ====================

    @abstractmethod
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

        这确保了：
        - 写入崩溃不会损坏原文件
        - 读取-修改-写入 操作是原子的

        Args:
            path: 文件路径
            content: 要写入的内容
            encoding: 编码
            backup: 是否创建备份

        Returns:
            WriteResult: 写入结果
        """
        ...

    # ==================== 新增：文件修改检测 ====================

    @abstractmethod
    def check_file_modified(
        self,
        path: str,
        since_timestamp: int,
        content: Optional[str] = None,
    ) -> bool:
        """
        检查文件自指定时间戳以来是否已修改。

        Windows 上时间戳可能在内容未实际改变的情况下改变
        （云同步、杀毒软件等）。对于完整读取，使用内容
        比较作为后备方案以避免误报。

        Args:
            path: 文件路径
            since_timestamp: 之前的时间戳（毫秒）
            content: 之前读取的内容（用于内容比较）

        Returns:
            bool: 文件是否已修改
        """
        ...

    # ==================== 新增：安全路径解析 ====================

    @abstractmethod
    def safe_resolve_path(self, path: str) -> tuple[str, bool, bool]:
        """
        安全解析文件路径，处理符号链接。

        Returns:
            (resolved_path, is_symlink, is_canonical)
            - resolved_path: 解析后的路径
            - is_symlink: 是否为符号链接
            - is_canonical: 是否为规范路径（所有符号链接已解析）
        """
        ...

    # ==================== 新增：路径比较 ====================

    @abstractmethod
    def paths_equal(self, path1: str, path2: str) -> bool:
        """比较两个路径是否指向同一文件（处理 Windows 大小写不敏感）"""
        ...

    # ==================== 新增：读取状态追踪 ====================

    def get_read_state(self, path: str) -> Optional[FileReadState]:
        """
        获取文件读取状态（供外部追踪）。
        默认实现返回 None（不追踪）。
        子类可以实现此方法以支持文件修改检测。
        """
        return None

    def update_read_state(self, path: str, state: FileReadState) -> None:
        """
        更新文件读取状态（供外部追踪）。
        默认实现为空操作。
        子类可以实现此方法以支持文件修改检测。
        """
        pass
