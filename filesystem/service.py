"""文件系统统一服务 — 对外暴露的接口

基于 Claude Code 设计:
- 文件历史追踪（支持撤销）
- 原子写入支持
- 文件修改检测
"""

import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from .base import BaseFileSystem, FileEntry, FileContent, SearchResult, WriteResult
from .windows import WindowsFileSystem
from .security import SecurityError

logger = logging.getLogger(__name__)


class FileHistory:
    """
    文件历史追踪器。

    记录文件的修改历史，支持撤销。
    基于 Claude Code 的 fileHistory 设计。
    """

    def __init__(self, backup_dir: Optional[str] = None):
        """
        Args:
            backup_dir: 备份目录路径，默认在 ~/.filesystem_history
        """
        if backup_dir is None:
            home = os.path.expanduser("~")
            backup_dir = os.path.join(home, ".filesystem_history")
        self._backup_dir = Path(backup_dir)
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        self._history: dict[str, list] = {}  # path -> list of history entries

    def _get_history_file(self, path: str) -> Path:
        """获取历史文件路径"""
        # 使用安全的哈希作为文件名
        import hashlib
        key = hashlib.sha256(path.encode()).hexdigest()[:16]
        return self._backup_dir / f"{key}.json"

    def record_edit(
        self,
        path: str,
        original_content: str,
        new_content: str,
        message: str = "",
    ) -> Optional[str]:
        """
        记录编辑历史。

        Returns:
            备份文件路径，失败返回 None
        """
        import json
        from datetime import datetime

        try:
            history_file = self._get_history_file(path)
            path_hash = history_file.stem

            # 加载现有历史
            history = []
            if history_file.exists():
                try:
                    history = json.loads(history_file.read_text())
                except (json.JSONDecodeError, OSError):
                    history = []

            # 创建备份
            backup_name = f"{path_hash}.{int(os.path.getmtime(path) * 1000) if os.path.exists(path) else 0}.bak"
            backup_path = self._backup_dir / backup_name

            if original_content:
                try:
                    backup_path.write_text(original_content, encoding="utf-8")
                except OSError as e:
                    logger.warning(f"创建历史备份失败: {e}")
                    return None

            # 添加历史记录
            entry = {
                "timestamp": datetime.now().isoformat(),
                "path": path,
                "backup_path": str(backup_path),
                "original_size": len(original_content) if original_content else 0,
                "new_size": len(new_content) if new_content else 0,
                "message": message,
            }
            history.append(entry)

            # 限制历史条目数量
            max_entries = 100
            if len(history) > max_entries:
                # 删除最旧的备份
                old_entries = history[:-max_entries]
                for entry in old_entries:
                    bp = entry.get("backup_path")
                    if bp and os.path.exists(bp):
                        try:
                            os.unlink(bp)
                        except OSError:
                            pass
                history = history[-max_entries:]

            # 保存历史
            history_file.write_text(
                json.dumps(history, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

            return str(backup_path)

        except Exception as e:
            logger.warning(f"记录历史失败: {e}")
            return None

    def get_history(self, path: str, limit: int = 10) -> list:
        """获取历史记录"""
        import json

        history_file = self._get_history_file(path)
        if not history_file.exists():
            return []

        try:
            history = json.loads(history_file.read_text())
            return history[-limit:]
        except (json.JSONDecodeError, OSError):
            return []

    def restore(self, path: str, backup_path: str) -> bool:
        """
        从备份恢复文件。

        Returns:
            是否成功
        """
        try:
            backup = Path(backup_path)
            if not backup.exists():
                logger.error(f"备份文件不存在: {backup_path}")
                return False

            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)
            return True
        except Exception as e:
            logger.error(f"恢复失败: {e}")
            return False


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

    def __init__(self, platform: Optional[str] = None, enable_history: bool = True):
        self._impl: BaseFileSystem = self._create_impl(platform)
        self._history: Optional[FileHistory] = FileHistory() if enable_history else None

    def _create_impl(self, platform: Optional[str]) -> BaseFileSystem:
        """工厂方法：根据平台创建实现"""
        if platform is None:
            import sys
            platform = "windows" if sys.platform == "win32" else "linux"

        platform = platform.lower()

        if platform == "windows":
            return WindowsFileSystem()
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
        """读取二进制文件（如图片），返回 (bytes, mime_type)"""
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

        lines = [f"📁 目录: {path}", "─" * 40]

        dirs = []
        files = []

        for entry in entries:
            if entry.get("is_symlink"):
                icon = "🔗"
            elif entry["is_dir"]:
                icon = "📂"
                dirs.append(entry)
            else:
                icon = "📄"
                files.append(entry)

        # 排序输出
        for entry in sorted(dirs, key=lambda x: x["name"].lower()):
            name = entry["name"]
            modified = entry.get("modified", "")[:16] if entry.get("modified") else ""
            lines.append(f"  {icon} {name}/  {modified}")

        for entry in sorted(files, key=lambda x: x["name"].lower()):
            name = entry["name"]
            size = entry.get("size_human", "")
            modified = entry.get("modified", "")[:16] if entry.get("modified") else ""
            lines.append(f"  📄 {name}  {size:>8}  {modified}")

        total = len(entries)
        if total > 0:
            dir_count = len(dirs)
            file_count = len(files)
            lines.append("─" * 40)
            lines.append(f"共 {dir_count} 个目录, {file_count} 个文件")

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
            f"📄 文件: {fc['name']}",
            f"📍 路径: {fc['path']}",
            f"📊 编码: {fc['encoding']} | 行数: {fc['lines']} | 大小: {fc['size']} bytes",
        ]

        if fc.get('mtime'):
            from datetime import datetime
            mtime = datetime.fromtimestamp(fc['mtime'] / 1000).isoformat()
            lines.append(f"🕐 修改: {mtime[:19]}")

        lines.append("─" * 40)

        content_lines = fc["content"].splitlines()
        for i, line in enumerate(content_lines, start=start_line + 1):
            lines.append(f"{i:4d} │ {line}")

        if fc["truncated"]:
            next_start = start_line + max_lines
            lines.append(f"\n... 已截断（剩余 {fc['lines'] - next_start} 行）")

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

        lines = [f"🔍 搜索: \"{pattern}\" in {directory}", "─" * 40]

        if not results:
            return f"未找到匹配 \"{pattern}\" 的文件"

        for r in results:
            if r["is_dir"]:
                lines.append(f"  📂 {r['path']}")
            elif r.get("match_line"):
                lines.append(f"  📄 {r['path']} [第 {r['match_line']} 行]")
                lines.append(f"      → {r['match_text']}")
            else:
                lines.append(f"  📄 {r['path']}")

        lines.append("─" * 40)
        lines.append(f"共 {len(results)} 个结果")
        if len(results) >= max_results:
            lines.append(f"（已达上限 {max_results}，可增加 max_results 获取更多）")
        return "\n".join(lines)

    async def format_drives(self) -> str:
        """格式化驱动器和磁盘信息"""
        try:
            info = await self.disk_info()
        except Exception as e:
            return f"获取磁盘信息失败: {e}"

        lines = ["💾 磁盘信息", "─" * 40]
        for d in info:
            if "error" in d:
                lines.append(f"  ❌ {d['drive']} 获取失败: {d['error']}")
            else:
                bar_len = 16
                filled = int(d["usage_pct"] / 100 * bar_len)
                bar = "█" * filled + "░" * (bar_len - filled)
                lines.append(
                    f"  💿 {d['drive']}  [{bar}] {d['usage_pct']:>5.1f}%"
                    f"  {d['used_gb']:>5.1f}/{d['total_gb']:>5.1f} GB"
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

        lines = [
            f"✅ 文件已创建",
            f"📍 路径: {entry['path']}",
            f"📊 大小: {entry.get('size_human', '0 B')}",
        ]
        if content:
            line_count = content.count("\n") + 1
            lines.append(f"📝 行数: {line_count}")
        lines.append(f"🔤 编码: {encoding}")
        return "\n".join(lines)

    async def format_mkdir(self, path: str) -> str:
        """格式化创建目录结果"""
        try:
            entry = await self.mkdir(path)
        except SecurityError as e:
            return f"创建被拒绝: {e}"
        except Exception as e:
            return f"创建失败: {e}"

        return f"✅ 目录已创建\n📍 路径: {entry['path']}"

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

        type_name = "📂 目录" if result["type"] == "directory" else "📄 文件"
        return f"✅ {type_name}已删除\n📍 路径: {result['path']}"

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
            return f"❌ 修改被拒绝: {e}"
        except Exception as e:
            return f"❌ 修改失败: {e}"

        mode_names = {
            "append": "追加内容",
            "replace": "替换全部内容",
            "insert": f"第 {line} 行前插入",
            "delete-line": f"删除第 {line} 行",
            "replace-line": f"替换第 {line} 行",
            "replace-text": f"替换文本",
        }
        mode_name = mode_names.get(mode, mode)

        lines = [
            f"✅ 文件已修改 ({mode_name})",
            f"📍 路径: {fc['path']}",
            f"📊 行数: {fc['lines']} | 大小: {fc['size']} bytes",
            "─" * 40,
        ]

        # 显示修改后的前 30 行预览
        preview_lines = fc["content"].splitlines()[:30]
        for i, l in enumerate(preview_lines, 1):
            lines.append(f"{i:4d} │ {l}")

        if fc["lines"] > 30:
            lines.append(f"... 共 {fc['lines']} 行")

        return "\n".join(lines)

    # ==================== 文件历史 ====================

    def get_file_history(self, path: str, limit: int = 10) -> list:
        """获取文件历史"""
        if not self._history:
            return []
        return self._history.get_history(path, limit)

    def restore_from_history(self, path: str, backup_path: str) -> bool:
        """从历史备份恢复文件"""
        if not self._history:
            return False
        return self._history.restore(path, backup_path)

    # ==================== 原子写入（直接调用） ====================

    def write_atomic(
        self,
        path: str,
        content: str,
        encoding: str = "utf-8",
        backup: bool = True,
    ) -> WriteResult:
        """
        原子写入文件（临时文件模式）。

        适用于需要直接写入文件的场景。

        Returns:
            WriteResult: 写入结果
        """
        if hasattr(self._impl, 'write_file_atomic'):
            return self._impl.write_file_atomic(path, content, encoding, backup)
        else:
            raise NotImplementedError("当前文件系统实现不支持原子写入")

    # ==================== 文件修改检测 ====================

    def is_file_modified(self, path: str, since_timestamp: int, content: str = None) -> bool:
        """
        检查文件是否已修改。

        Args:
            path: 文件路径
            since_timestamp: 之前的时间戳（毫秒）
            content: 之前读取的内容（可选，用于内容比较）

        Returns:
            bool: 文件是否已修改
        """
        if hasattr(self._impl, 'check_file_modified'):
            return self._impl.check_file_modified(path, since_timestamp, content)
        return False

    def get_read_state(self, path: str) -> Optional[dict]:
        """获取文件读取状态"""
        if hasattr(self._impl, 'get_read_state'):
            state = self._impl.get_read_state(path)
            return state.to_dict() if state else None
        return None
