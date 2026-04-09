"""文件系统统一服务 — 对外暴露的接口"""

import logging
from typing import Optional

from .base import BaseFileSystem, FileEntry, FileContent, SearchResult
from .windows import WindowsFileSystem
from .security import SecurityError

logger = logging.getLogger(__name__)


class FileSystemService:
    """
    统一文件系统服务

    使用方式：
        fs = FileSystemService()          # 自动检测当前系统
        fs = FileSystemService("windows") # 手动指定

        entries = await fs.ls("C:\\Users")
        content = await fs.cat("C:\\test.txt")
        results = await fs.find("C:\\Projects", "*.py")
    """

    def __init__(self, platform: Optional[str] = None):
        self._impl: BaseFileSystem = self._create_impl(platform)

    def _create_impl(self, platform: Optional[str]) -> BaseFileSystem:
        """工厂方法：根据平台创建实现"""
        if platform is None:
            import sys
            platform = "windows" if sys.platform == "win32" else "linux"

        platform = platform.lower()

        if platform == "windows":
            return WindowsFileSystem()
        # elif platform == "linux":
        #     return LinuxFileSystem()    # 后续扩展
        # elif platform == "macos":
        #     return MacFileSystem()       # 后续扩展
        else:
            raise ValueError(f"不支持的平台: {platform}（目前仅支持 windows）")

    @property
    def platform(self) -> str:
        return type(self._impl).__name__.replace("FileSystem", "").lower()

    # ==================== 快捷方法 ====================

    async def ls(self, path: str) -> list[dict]:
        """列出目录 → dict 列表"""
        entries = await self._impl.list_directory(path)
        return [e.to_dict() for e in entries]

    async def cat(
        self,
        path: str,
        encoding: str = "utf-8",
        max_lines: int = 500,
        start_line: int = 0,
    ) -> dict:
        """读取文件内容 → dict"""
        content = await self._impl.read_file(path, encoding, max_lines, start_line)
        return content.to_dict()

    async def read(
        self,
        path: str,
        encoding: str = "utf-8",
        max_lines: int = 500,
        start_line: int = 0,
    ) -> FileContent:
        """读取文件内容 → FileContent 对象"""
        return await self._impl.read_file(path, encoding, max_lines, start_line)

    async def find(
        self,
        directory: str,
        pattern: str,
        content_search: bool = False,
        max_results: int = 500,
    ) -> list[dict]:
        """搜索文件 → dict 列表"""
        results = await self._impl.search_files(directory, pattern, content_search, max_results)
        return [r.to_dict() for r in results]

    async def drives(self) -> list[str]:
        """获取驱动器列表"""
        return await self._impl.get_drives()

    async def disk_info(self) -> list[dict]:
        """获取磁盘信息"""
        return await self._impl.get_disk_info()

    async def read_binary(self, path: str) -> tuple[bytes, str]:
        """读取二进制文件（如图片），返回 (bytes, filename)"""
        return await self._impl.read_binary(path)

    # ==================== 格式化输出（给 QQ 消息用） ====================

    async def format_ls(self, path: str) -> str:
        """格式化目录列表为可读文本"""
        try:
            entries = await self.ls(path)
        except SecurityError as e:
            return f"访问被拒绝: {e}"
        except Exception as e:
            return f"读取失败: {e}"

        if not entries:
            return f"目录为空: {path}"

        lines = [f"目录: {path}", "=" * 40]

        for entry in entries:
            if entry["is_dir"]:
                lines.append(f"  [DIR]  {entry['name']}/")
            else:
                size = entry.get("size_human", "")
                modified = entry.get("modified", "")[:16] if entry.get("modified") else ""
                lines.append(f"  [FILE] {entry['name']}  ({size})  {modified}")

        total = len(entries)
        if total > 0:
            dirs = sum(1 for e in entries if e["is_dir"])
            files = total - dirs
            lines.append(f"\n共 {dirs} 个目录, {files} 个文件")

        return "\n".join(lines)

    async def format_cat(
        self,
        path: str,
        max_lines: int = 80,
        start_line: int = 0,
    ) -> str:
        """格式化文件内容为可读文本"""
        try:
            fc = await self.cat(path, max_lines=max_lines, start_line=start_line)
        except SecurityError as e:
            return f"访问被拒绝: {e}"
        except Exception as e:
            return f"读取失败: {e}"

        lines = [
            f"文件: {fc['name']}",
            f"路径: {fc['path']}",
            f"编码: {fc['encoding']} | 行数: {fc['lines']} | 大小: {fc['size']} bytes",
            "=" * 40,
        ]

        content_lines = fc["content"].splitlines()
        for i, line in enumerate(content_lines, start=start_line + 1):
            lines.append(f"{i:4d} | {line}")

        if fc["truncated"]:
            next_start = start_line + max_lines
            lines.append(f"\n... 已截断，查看后续: /fs read \"{path}\" --start {next_start}")

        return "\n".join(lines)

    async def format_find(
        self,
        directory: str,
        pattern: str,
        content_search: bool = False,
        max_results: int = 500,
    ) -> str:
        """格式化搜索结果"""
        try:
            results = await self.find(directory, pattern, content_search, max_results=max_results)
        except SecurityError as e:
            return f"搜索被拒绝: {e}"
        except Exception as e:
            return f"搜索失败: {e}"

        lines = [f"搜索: \"{pattern}\" in {directory}", "=" * 40]

        if not results:
            return f"未找到匹配 \"{pattern}\" 的文件"

        for r in results:
            if r["is_dir"]:
                lines.append(f"  [DIR]  {r['path']}")
            elif r.get("match_line"):
                lines.append(f"  [LINE {r['match_line']}] {r['path']}")
                lines.append(f"          → {r['match_text']}")
            else:
                lines.append(f"  [FILE] {r['path']}")

        lines.append(f"\n共 {len(results)} 个结果（max_results={max_results}）")
        return "\n".join(lines)

    async def format_drives(self) -> str:
        """格式化驱动器和磁盘信息"""
        try:
            info = await self.disk_info()
        except Exception as e:
            return f"获取磁盘信息失败: {e}"

        lines = ["磁盘信息", "=" * 40]
        for d in info:
            if "error" in d:
                lines.append(f"  {d['drive']}  获取失败: {d['error']}")
            else:
                bar_len = 20
                filled = int(d["usage_pct"] / 100 * bar_len)
                bar = "█" * filled + "░" * (bar_len - filled)
                lines.append(
                    f"  {d['drive']}  [{bar}] {d['usage_pct']}%"
                    f"  {d['used_gb']:.0f}/{d['total_gb']:.0f} GB"
                )
        return "\n".join(lines)

    # ==================== 创建 ====================

    async def touch(
        self,
        path: str,
        content: str = "",
        encoding: str = "utf-8",
        overwrite: bool = False,
    ) -> dict:
        """创建文件 → dict"""
        entry = await self._impl.create_file(path, content, encoding, overwrite)
        return entry.to_dict()

    async def mkdir(self, path: str) -> dict:
        """创建目录 → dict"""
        entry = await self._impl.create_directory(path)
        return entry.to_dict()

    # ==================== 格式化输出 ====================

    async def format_touch(
        self,
        path: str,
        content: str = "",
        encoding: str = "utf-8",
        overwrite: bool = False,
    ) -> str:
        """格式化创建文件结果"""
        try:
            entry = await self.touch(path, content, encoding, overwrite)
        except SecurityError as e:
            return f"创建被拒绝: {e}"
        except Exception as e:
            return f"创建失败: {e}"

        from pathlib import Path
        p = Path(path)
        lines = [
            f"文件已创建 ✓",
            f"路径: {entry['path']}",
            f"大小: {entry.get('size_human', '0 B')}",
        ]
        if content:
            line_count = content.count("\n") + 1
            lines.append(f"行数: {line_count}")
        lines.append(f"编码: {encoding}")
        return "\n".join(lines)

    async def format_mkdir(self, path: str) -> str:
        """格式化创建目录结果"""
        try:
            entry = await self.mkdir(path)
        except SecurityError as e:
            return f"创建被拒绝: {e}"
        except Exception as e:
            return f"创建失败: {e}"

        return f"目录已创建 ✓\n路径: {entry['path']}"


    # ==================== 删除 ====================

    async def rm(self, path: str, recursive: bool = False) -> dict:
        """删除文件或目录 → dict"""
        from pathlib import Path
        p = Path(path).resolve()

        if p.is_dir():
            deleted = await self._impl.delete_directory(path, recursive)
            return {"path": deleted, "type": "directory"}
        else:
            deleted = await self._impl.delete_file(path)
            return {"path": deleted, "type": "file"}

    async def format_rm(self, path: str, recursive: bool = False) -> str:
        """格式化删除结果"""
        try:
            result = await self.rm(path, recursive)
        except SecurityError as e:
            return f"删除被拒绝: {e}"
        except Exception as e:
            return f"删除失败: {e}"

        type_name = "目录" if result["type"] == "directory" else "文件"
        return f"{type_name}已删除 ✓\n路径: {result['path']}"

    # ==================== 修改 ====================

    async def edit(
        self,
        path: str,
        mode: str,
        content: str = "",
        line: int = 0,
        old_text: str = "",
        new_text: str = "",
        encoding: str = "utf-8",
    ) -> dict:
        """修改文件 → dict"""
        fc = await self._impl.edit_file(
            path, mode, content, line, old_text, new_text, encoding
        )
        return fc.to_dict()

    async def format_edit(
        self,
        path: str,
        mode: str,
        content: str = "",
        line: int = 0,
        old_text: str = "",
        new_text: str = "",
        encoding: str = "utf-8",
    ) -> str:
        """格式化修改结果"""
        try:
            fc = await self.edit(path, mode, content, line, old_text, new_text, encoding)
        except SecurityError as e:
            return f"修改被拒绝: {e}"
        except Exception as e:
            return f"修改失败: {e}"

        mode_names = {
            "append": "追加内容",
            "replace": "替换全部内容",
            "insert": f"第 {line} 行前插入",
            "delete-line": f"删除第 {line} 行",
            "replace-line": f"替换第 {line} 行",
            "replace-text": f"替换文本 ({old_text} → {new_text})",
        }
        mode_name = mode_names.get(mode, mode)

        lines = [
            f"文件已修改 ✓ ({mode_name})",
            f"路径: {fc['path']}",
            f"行数: {fc['lines']} | 大小: {fc['size']} bytes",
            "=" * 40,
        ]

        # 显示修改后的前 30 行预览
        preview_lines = fc["content"].splitlines()[:30]
        for i, l in enumerate(preview_lines, 1):
            lines.append(f"{i:4d} | {l}")

        if fc["lines"] > 30:
            lines.append(f"... 共 {fc['lines']} 行")

        return "\n".join(lines)

