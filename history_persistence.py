"""对话历史持久化 — JSON 文件存储，重启后自动恢复"""

import json
import logging
import asyncio
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_HISTORY_FILE = Path(__file__).parent / ".conversation_history.json"
_SAVE_INTERVAL = 300  # 距离上次保存超过 300s 才写盘（避免频繁 IO）
_BACKUP_SUFFIX = ".bak"

_last_save_time: float = 0


def save_history(history: dict) -> bool:
    """
    将对话历史保存到 JSON 文件（带原子写入 + 备份）。
    保存频率受 _SAVE_INTERVAL 节流。
    返回是否实际执行了保存。
    """
    global _last_save_time
    now = time.time()

    if now - _last_save_time < _SAVE_INTERVAL and _last_save_time > 0:
        return False  # 节流：距离上次保存不足 _SAVE_INTERVAL 秒

    try:
        # 备份旧文件
        if _HISTORY_FILE.exists():
            backup_path = _HISTORY_FILE.with_suffix(_HISTORY_FILE.suffix + _BACKUP_SUFFIX)
            _HISTORY_FILE.rename(backup_path)

        # 原子写入：新文件 → 写完再 rename
        tmp_path = _HISTORY_FILE.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False)
        tmp_path.replace(_HISTORY_FILE)

        _last_save_time = now
        logger.info(f"对话历史已保存到 {_HISTORY_FILE}，共 {len(history)} 个 session")
        return True
    except Exception as e:
        logger.error(f"保存对话历史失败: {e}", exc_info=True)
        return False


def load_history() -> dict:
    """
    从 JSON 文件加载对话历史。
    文件不存在或解析失败时返回空 dict。
    """
    try:
        if not _HISTORY_FILE.exists():
            logger.info("未找到历史记录文件，跳过加载")
            return {}

        with open(_HISTORY_FILE, encoding="utf-8") as f:
            history = json.load(f)

        if not isinstance(history, dict):
            logger.warning(f"历史记录文件格式错误，清空后重启")
            return {}

        total_msgs = sum(len(v) for v in history.values())
        logger.info(f"对话历史已加载：从 {_HISTORY_FILE} 恢复 {len(history)} 个 session，共 {total_msgs} 条消息")
        return history
    except json.JSONDecodeError as e:
        logger.error(f"历史记录 JSON 解析失败: {e}，尝试加载备份文件")
        backup_path = _HISTORY_FILE.with_suffix(_HISTORY_FILE.suffix + _BACKUP_SUFFIX)
        if backup_path.exists():
            try:
                with open(backup_path, encoding="utf-8") as f:
                    history = json.load(f)
                logger.info(f"已从备份文件恢复 {len(history)} 个 session")
                return history
            except Exception:
                pass
        logger.warning("历史记录和备份均不可用，从空历史开始")
        return {}
    except Exception as e:
        logger.error(f"加载对话历史失败: {e}", exc_info=True)
        return {}


def get_history_path() -> Path:
    return _HISTORY_FILE
