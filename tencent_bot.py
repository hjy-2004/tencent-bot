from filesystem import FileSystemService
from filesystem_tools import TOOLS, execute_tool, register_senders, consume_image_sent_flag

"""腾讯 QQ 机器人 — 富媒体版本"""

import json
import time
import logging
import re
import binascii
import httpx
from typing import Optional
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import nacl.signing

from mimo_client import MiMoClient
from image_renderer import ImageRenderer

from config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()

mimo = MiMoClient()
renderer = ImageRenderer()

processed_messages: set[str] = set()
MAX_PROCESSED = 10000

conversation_history: dict[str, list[dict]] = defaultdict(list)
MAX_HISTORY = 20

SYSTEM_PROMPT = """你是一个部署在QQ平台上的智能助手，由小米 MiMo-V2-Pro 驱动。
你友好、专业、乐于助人。请用简洁清晰的语言回答用户的问题。
如果不确定答案，请诚实说明。

你可以使用文件系统工具来帮助用户：
- /fs ls <路径> — 列出目录
- /fs find <路径> --find <模式> — 按文件名搜索文件
- /fs read <文件> — 读取文本文件
- /fs_send_image path=<图片路径> caption=<描述> — 读取本地图片并发送给对方（当用户要求发送、查看、发送图片时，必须调用此工具！）
- /fs touch/mkdir/rm/edit/drives 等其他文件操作

【重要：发送图片的用法】
当用户提到"发送图片"、"发张图片"、"查看图片"、"随机发一张图片"时，必须立即调用 fs_send_image 工具：
- path: 图片的完整路径（如 E:\\Photos\\anime\\12.jpg）
- caption: 可选描述文字（如"随机动漫图片"）

搜索完成后，如果找到了图片，应直接调用 fs_send_image 发送，不要只返回文字。

【搜索文件的重要规则】
1. 使用 fs_find 时，建议一次指定多个扩展名（如 jpg|jpeg|png|gif|bmp|webp），避免逐个扩展名单独搜索
2. fs_find 默认最多返回 200 条结果，如果要获得更完整计数，请指定 max_results=500
3. 搜索结果中会显示 "共 N 个结果（max_results=M）"，如果 N == M，说明可能有更多未列出的结果
4. 对于大目录（有很多文件），不要逐个子目录分别搜索，直接用 fs_find 搜索整棵树
5. 搜索完成后，用简洁语言告诉用户"共找到 X 张图片"即可，不要列出所有文件名
6. 避免重复搜索相同目录或相同扩展名模式

操作完成后，用简洁的语言告诉用户结果。"""

fs_service = FileSystemService()  # 自动选择 Windows 实现


# ==================== 数据模型 ====================

class WebhookPayload(BaseModel):
    op: int
    d: Optional[dict] = None
    s: Optional[int] = None
    t: Optional[str] = None


# ==================== Ed25519 签名 ====================

def generate_signature(bot_secret: str, event_ts: str, plain_token: str) -> str:
    seed = bot_secret
    while len(seed) < 32:
        seed += bot_secret
    seed_bytes = seed[:32].encode("utf-8")

    signing_key = nacl.signing.SigningKey(seed_bytes)
    message = (event_ts + plain_token).encode("utf-8")
    signed = signing_key.sign(message)
    return binascii.hexlify(signed.signature).decode("utf-8")


# ==================== QQ Bot API（富媒体增强版） ====================

def _compress_image(image_bytes: bytes, max_size_kb: int = 300, max_width: int = 800) -> bytes:
    """压缩图片：转 JPEG，缩放，控制大小"""
    try:
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(image_bytes))

        # 缩放
        if img.width > max_width:
            ratio = max_width / img.width
            new_height = int(img.height * ratio)
            img = img.resize((max_width, new_height), Image.LANCZOS)

        # RGBA → RGB（JPEG 不支持透明度）
        if img.mode in ("RGBA", "P"):
            bg = Image.new("RGB", img.size, (15, 12, 41))  # 深色背景
            if img.mode == "P":
                img = img.convert("RGBA")
            bg.paste(img, mask=img.split()[-1])
            img = bg

        # 尝试不同质量直到满足大小
        for quality in (85, 70, 55, 40):
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
            if len(data) <= max_size_kb * 1024:
                logger.info(f"图片压缩: {len(image_bytes)} → {len(data)} bytes (quality={quality})")
                return data

        # 都不行，返回最小的
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=40, optimize=True)
        data = buf.getvalue()
        logger.info(f"图片压缩(最低质量): {len(image_bytes)} → {len(data)} bytes")
        return data

    except ImportError:
        logger.warning("Pillow 未安装，跳过压缩")
        return image_bytes
    except Exception as e:
        logger.error(f"图片压缩失败: {e}")
        return image_bytes


class QQBotAPI:
    """QQ 机器人 API 客户端 — 支持文本、Markdown、图片、Ark 卡片"""

    def __init__(self):
        settings = get_settings()
        self.app_id = settings.tencent_app_id
        self.app_secret = settings.tencent_app_secret
        self.base_url = "https://api.sgroup.qq.com"
        self.access_token: Optional[str] = None
        self._token_fetched: bool = False  # 是否已获取过 token

    # ---------- Token 管理 ----------

    async def get_access_token(self) -> str:
        """从腾讯服务器获取新的 access_token 并缓存"""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    "https://bots.qq.com/app/getAppAccessToken",
                    json={"appId": self.app_id, "clientSecret": self.app_secret},
                )
                if resp.status_code != 200:
                    logger.error(f"获取 token 失败 {resp.status_code}: {resp.text}")
                    raise Exception(f"Token 获取失败: {resp.text}")

                data = resp.json()
                token = data.get("access_token", "")
                expires_in = int(data.get("expires_in", 7200))

                if not token:
                    raise Exception("获取的 access_token 为空")

                self.access_token = token
                self._token_fetched = True
                logger.info(f"Token 已更新并缓存 (expires_in={expires_in}s)")
                return self.access_token
        except Exception as e:
            logger.error(f"获取 token 异常: {e}", exc_info=True)
            raise

    async def _ensure_token(self):
        """仅在从未获取过 token 时才拉取，已缓存的 token 直接复用"""
        if not self._token_fetched:
            logger.info("首次获取 Token...")
            await self.get_access_token()

    def _is_token_error(self, resp: httpx.Response) -> bool:
        if resp.status_code == 401:
            return True
        if resp.status_code >= 400 and resp.text:
            try:
                err = resp.json()
                if err.get("err_code") == 11244:
                    return True
                msg = err.get("message", "").lower()
                if "token" in msg and ("expire" in msg or "not exist" in msg):
                    return True
            except Exception:
                pass
        return False

    # ---------- 通用请求 ----------

    async def _request(
        self, method: str, path: str,
        json_data: dict = None,
        data: dict = None,
        files: dict = None,
    ) -> dict:
        await self._ensure_token()

        async with httpx.AsyncClient(timeout=30.0) as client:
            url = f"{self.base_url}{path}"

            def make_headers():
                h = {"Authorization": f"QQBot {self.access_token}"}
                if not files:
                    h["Content-Type"] = "application/json"
                return h

            async def do_request(headers):
                if method == "POST":
                    return await client.post(
                        url, headers=headers,
                        json=json_data, data=data, files=files,
                    )
                return await client.get(url, headers=headers)

            resp = await do_request(make_headers())

            # ★ 核心：只有 QQ API 返回 401 时才刷新 token 并重试
            if self._is_token_error(resp):
                logger.warning(f"Token 失效 ({resp.status_code})，刷新后重试...")
                await self.get_access_token()
                resp = await do_request(make_headers())
                if resp.status_code >= 400:
                    logger.error(f"重试后仍然失败 {resp.status_code}: {resp.text}")
                    return {"status_code": resp.status_code, "error": resp.text}

            if resp.status_code >= 400:
                logger.warning(f"QQ API {method} {path} 返回 {resp.status_code}: {resp.text[:500]}")
                return {"status_code": resp.status_code, "error": resp.text}
            logger.debug(f"QQ API {method} {path} -> {resp.status_code}")
            return resp.json() if resp.text else {"status_code": resp.status_code}

    # ---------- 文件上传（也用同样的逻辑）----------

    async def _upload_file(
            self, path_prefix: str,
            file_data: bytes,
            filename: str = "image.jpg",
            file_type: int = 1,
    ) -> Optional[str]:
        try:
            import base64

            file_data = _compress_image(file_data)
            b64_data = base64.b64encode(file_data).decode("utf-8")

            await self._ensure_token()
            url = f"{self.base_url}{path_prefix}/files"

            payload = {
                "file_type": file_type,
                "file_data": b64_data,
                "srv_send_msg": False,
            }

            async def do_upload(token: str):
                async with httpx.AsyncClient(timeout=30.0) as client:
                    return await client.post(
                        url,
                        headers={
                            "Authorization": f"QQBot {token}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )

            resp = await do_upload(self.access_token)

            # ★ 同样：401 才刷新重试
            if self._is_token_error(resp):
                logger.warning(f"上传时 Token 失效，刷新后重试...")
                await self.get_access_token()
                resp = await do_upload(self.access_token)

            logger.info(f"上传响应 {resp.status_code}: {resp.text[:300]}")

            if resp.status_code in (200, 201):
                result = resp.json()
                file_info = result.get("file_info", "")
                ttl = result.get("ttl", 0)
                if file_info:
                    logger.info(f"文件上传成功 (ttl={ttl})")
                    return file_info
                else:
                    logger.error(f"上传成功但无 file_info: {resp.text}")
                    return None
            else:
                logger.error(f"文件上传失败 {resp.status_code}: {resp.text}")
                return None
        except Exception as e:
            logger.error(f"文件上传异常: {e}", exc_info=True)
            return None


    async def upload_c2c_image(self, user_openid: str, image_data: bytes) -> Optional[str]:
        return await self._upload_file(f"/v2/users/{user_openid}", image_data)

    async def upload_group_image(self, group_openid: str, image_data: bytes) -> Optional[str]:
        return await self._upload_file(f"/v2/groups/{group_openid}", image_data)

    # ---------- 消息发送 ----------

    async def send_c2c_message(self, user_openid, content, msg_id=""):
        payload = {"content": content, "msg_type": 0}
        if msg_id:
            payload["msg_id"] = msg_id
        return await self._request("POST", f"/v2/users/{user_openid}/messages", payload)

    async def send_group_message(self, group_openid, content, msg_id=""):
        payload = {"content": content, "msg_type": 0}
        if msg_id:
            payload["msg_id"] = msg_id
        return await self._request("POST", f"/v2/groups/{group_openid}/messages", payload)

    async def send_channel_message(self, channel_id, content, msg_id=""):
        payload = {"content": content, "msg_type": 0}
        if msg_id:
            payload["msg_id"] = msg_id
        return await self._request("POST", f"/channels/{channel_id}/messages", payload)

    async def send_c2c_image(self, user_openid, image_data, caption="", msg_id=""):
        file_info = await self._upload_file(f"/v2/users/{user_openid}", image_data)
        if not file_info:
            return await self.send_c2c_message(user_openid, caption or "[图片发送失败]", msg_id)
        # C2C 私聊发图片：msg_type=7（media），media 字段引用 file_info
        payload = {
            "msg_type": 7,
            "media": {"file_info": file_info},
        }
        if msg_id:
            payload["msg_id"] = msg_id

        logger.info(f"C2C 图片: msg_type=7, file_info_len={len(file_info)}, msg_id={'有' if msg_id else '无'}")
        return await self._request("POST", f"/v2/users/{user_openid}/messages", payload)

    async def send_group_image(self, group_openid, image_data, caption="", msg_id=""):
        file_info = await self._upload_file(f"/v2/groups/{group_openid}", image_data)
        if not file_info:
            return await self.send_group_message(group_openid, caption or "[图片发送失败]", msg_id)
        # 群聊发图片：msg_type=7（media），media 字段引用 file_info
        # content 为必填字段，不能省略
        # 带上 msg_id 作为被动回复，避免被识别为主动消息（无权限会报 40034102）
        payload = {
            "msg_type": 7,
            "content": caption,
            "media": {"file_info": file_info},
        }
        if msg_id:
            payload["msg_id"] = msg_id

        logger.info(f"群聊图片 payload: {json.dumps(payload, ensure_ascii=False)[:300]}")
        return await self._request("POST", f"/v2/groups/{group_openid}/messages", payload)

    async def send_c2c_card(self, user_openid, title, fields, msg_id=""):
        lines = [f"✦ {title}", ""]
        for f in fields:
            lines.append(f"▸ {f['name']}: {f['value']}")
        content = "\n".join(lines)
        return await self.send_c2c_message(user_openid, content, msg_id)

    async def send_group_card(self, group_openid, title, fields, msg_id=""):
        lines = [f"✦ {title}", ""]
        for f in fields:
            lines.append(f"▸ {f['name']}: {f['value']}")
        content = "\n".join(lines)
        return await self.send_group_message(group_openid, content, msg_id)

    async def send_c2c_markdown(self, user_openid, md_content, msg_id=""):
        payload = {
            "msg_type": 2,
            "markdown": {"content": md_content},
        }
        if msg_id:
            payload["msg_id"] = msg_id
        return await self._request("POST", f"/v2/users/{user_openid}/messages", payload)

    async def send_group_markdown(self, group_openid, md_content, msg_id=""):
        payload = {
            "msg_type": 2,
            "markdown": {"content": md_content},
        }
        if msg_id:
            payload["msg_id"] = msg_id
        return await self._request("POST", f"/v2/groups/{group_openid}/messages", payload)





# 全局实例
qq_api = QQBotAPI()


# ==================== 消息分析 ====================

def extract_message_text(content: str) -> str:
    """移除 @提及，提取纯文本"""
    return re.sub(r"<@!\d+>", "", content).strip()


def detect_command(text: str) -> tuple[str, str]:
    """
    检测特殊命令前缀
    返回 (command, remaining_text)
    支持: /img /card /md /help /fs
    """
    # /fs 子命令特殊处理
    fs_match = re.match(r"^/fs\s+(\w+)\s*(.*)", text, re.DOTALL | re.IGNORECASE)
    if fs_match:
        return f"fs_{fs_match.group(1).lower()}", fs_match.group(2).strip()

    match = re.match(r"^/(img|card|md|help)\s*(.*)", text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).lower(), match.group(2).strip()
    return "", text

# ==================== 文件系统命令解析 ====================
def parse_fs_args(args_str: str) -> tuple[str, dict]:
    """
    解析 /fs 子命令参数
    返回 (path, options)

    示例：
      "C:\\Users"                    → ("C:\\Users", {})
      "C:\\test.txt --lines 100"     → ("C:\\test.txt", {"max_lines": 100})
      "C:\\Projects --grep keyword"  → ("C:\\Projects", {"pattern": "keyword", "content_search": True})
    """
    parts = args_str.strip().split()
    if not parts:
        return "", {}

    path = parts[0]
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]

    options = {}
    i = 1
    while i < len(parts):
        if parts[i] == "--lines" and i + 1 < len(parts):
            options["max_lines"] = int(parts[i + 1])
            i += 2
        elif parts[i] == "--start" and i + 1 < len(parts):
            options["start_line"] = int(parts[i + 1])
            i += 2
        elif parts[i] == "--encoding" and i + 1 < len(parts):
            options["encoding"] = parts[i + 1]
            i += 2
        elif parts[i] == "--grep" and i + 1 < len(parts):
            options["pattern"] = parts[i + 1]
            options["content_search"] = True
            i += 2
        elif parts[i] == "--find" and i + 1 < len(parts):
            options["pattern"] = parts[i + 1]
            options["content_search"] = False
            i += 2
        # ★ 修复：--content 只取到下一个 --flag 之前
        elif parts[i] == "--content":
            content_parts = []
            i += 1
            while i < len(parts) and not parts[i].startswith("--"):
                content_parts.append(parts[i])
                i += 1
            options["content"] = " ".join(content_parts)
            # 不 break，继续解析后面的 --overwrite 等
        elif parts[i] == "--overwrite":
            options["overwrite"] = True
            i += 1
            # ★ 新增
        elif parts[i] == "--recursive" or parts[i] == "-r":
            options["recursive"] = True
            i += 1
            # ★ 新增
        elif parts[i] == "--append":
            content_parts = []
            i += 1
            while i < len(parts) and not parts[i].startswith("--"):
                content_parts.append(parts[i])
                i += 1
            options["mode"] = "append"
            options["content"] = " ".join(content_parts)
        elif parts[i] == "--replace":
            # 替换整个文件内容
            content_parts = []
            i += 1
            while i < len(parts) and not parts[i].startswith("--"):
                content_parts.append(parts[i])
                i += 1
            options["mode"] = "replace"
            options["content"] = " ".join(content_parts)
        elif parts[i] == "--insert" and i + 1 < len(parts):
            options["mode"] = "insert"
            options["line"] = int(parts[i + 1])
            i += 2
            # 剩余内容作为插入内容
            content_parts = []
            while i < len(parts) and not parts[i].startswith("--"):
                content_parts.append(parts[i])
                i += 1
            options["content"] = " ".join(content_parts)
        elif parts[i] == "--delete-line" and i + 1 < len(parts):
            options["mode"] = "delete-line"
            # 支持行号或范围 3-5
            line_val = parts[i + 1]
            if "-" in line_val:
                options["line"] = line_val  # 保持字符串给 service 层解析
            else:
                options["line"] = int(line_val)
            i += 2
        elif parts[i] == "--replace-line" and i + 1 < len(parts):
            options["mode"] = "replace-line"
            line_val = parts[i + 1]
            if "-" in line_val:
                options["line"] = line_val
            else:
                options["line"] = int(line_val)
            i += 2
            content_parts = []
            while i < len(parts) and not parts[i].startswith("--"):
                content_parts.append(parts[i])
                i += 1
            options["content"] = " ".join(content_parts)
        elif parts[i] == "--old" and i + 1 < len(parts):
            options["mode"] = "replace-text"
            options["old_text"] = parts[i + 1]
            i += 2
        elif parts[i] == "--new" and i + 1 < len(parts):
            options["new_text"] = parts[i + 1]
            i += 2
        else:
            i += 1

    return path, options


# ==================== 图片意图识别 ====================
from dataclasses import dataclass

IMAGE_INTENT_GENERATE = "generate"
IMAGE_INTENT_SEARCH  = "search"
IMAGE_INTENT_SEND   = "send"

@dataclass
class ImageIntent:
    intent_type: str
    description: str = ""   # 用于生成
    path: str = ""          # 文件/目录路径
    query: str = ""          # 搜索关键词

_DRIVE_ANCHOR_RE = re.compile(r"[A-Za-z]:[/\\]", re.IGNORECASE)
_IMAGE_EXT_RE   = re.compile(
    r"\.(png|jpg|jpeg|gif|webp|bmp|tiff?|svg)$", re.IGNORECASE
)


def _extract_path_at_anchor(text: str, anchor_start: int) -> str:
    """
    给定 X:\ 在文本中的锚点起始位置，向后贪婪提取完整路径。
    遇到空白或非法字符停止。
    """
    remainder = text[anchor_start:]
    m2 = re.match(r"[A-Za-z]:[/\\]", remainder)
    if not m2:
        return ""
    anchor_end = anchor_start + m2.end()

    path_chars = []
    for ch in text[anchor_end:]:
        if ch.isspace():
            break
        path_chars.append(ch)

    raw = text[anchor_start:anchor_end] + "".join(path_chars)
    # 去掉尾部分隔符
    raw = raw.rstrip("/\\")
    return raw


def _find_image_path(text: str) -> str | None:
    """
    在文本中扫描所有 X:\ 锚点，返回第一个匹配图片扩展名的完整路径。
    """
    for m in _DRIVE_ANCHOR_RE.finditer(text):
        candidate = _extract_path_at_anchor(text, m.start())
        if candidate and _IMAGE_EXT_RE.search(candidate):
            return candidate
    return None


def detect_image_intent(text: str) -> ImageIntent | None:
    """
    检测自然语言中的图片意图，返回 ImageIntent 或 None。
    优先级：绝对路径 > 明确发送指令 > 搜索 > 生成。
    """
    text = text.strip()
    if not text:
        return None

    # ── 意图 1: 文本中直接包含图片绝对路径 ──
    img_path = _find_image_path(text)
    if img_path:
        return ImageIntent(intent_type=IMAGE_INTENT_SEND, path=img_path)

    # ── 意图 2: 发送图片的明确指令（即使没有路径也标记，后续由调用方处理）──
    send_phrases = [
        r"发[送送]?.*图",
        r"给我看(?:看)?.*图",
        r"看(?:看)?.*图",
        r"发个?图",
        r"把这[张个]?图",
        r"这张.*图",
        r"那张.*图",
        r"上[一膜张]?图",
    ]
    for ph in send_phrases:
        if re.search(ph, text):
            # 指令中可能夹带路径
            if img_path:
                return ImageIntent(intent_type=IMAGE_INTENT_SEND, path=img_path)
            # 没有路径 → 不拦截，交给 AI 决策
            return None

    # ── 意图 3: 图片搜索 ──
    search_patterns = [
        r"查?找.{0,10}的?图片",
        r"搜[索].{0,10}的?图片",
        r"找.{0,10}的?图片",
        r"有.{0,10}图片吗",
        r".{0,10}图片在哪里",
        r".{0,10}图(?:片)?放(?:在|哪)",
    ]
    for pat in search_patterns:
        if re.search(pat, text):
            query = re.sub(r"(的|图片|图)+$", "", text).strip()
            return ImageIntent(intent_type=IMAGE_INTENT_SEARCH, query=query, description=query)

    # ── 意图 4: 生成图片 ──
    gen_patterns = [
        r"发[张个幅]?(?:图|画|照片|图)",
        r"画[张个幅]?(?:图|画|照片)",
        r"生成[张个幅]?(?:图|画|照片)",
        r"帮我?画",
        r"帮我?生[成]?",
        r"画[张]?(?:给?我)?",
        r"给我?画",
        r"给?我?生成",
    ]
    for pat in gen_patterns:
        if re.search(pat, text):
            desc = re.sub(
                r"^(?:发[张个幅]?|画[张个幅]?|生成[张个幅]?|帮我?|给我?|我)[的得]?",
                "", text,
            ).strip()
            desc = re.sub(r"(的|图|图片|照片)$", "", desc).strip()
            if desc:
                return ImageIntent(intent_type=IMAGE_INTENT_GENERATE, description=desc)
            return None  # 仅有"发一张图"无描述

    return None



# ==================== 消息处理器 ====================

async def _process_and_reply(
    text: str,
    session_key: str,
    send_text_fn,
    send_image_fn=None,
    send_card_fn=None,
    send_markdown_fn=None,
    msg_id: str = "",
):
    command, prompt = detect_command(text)

    # ---- 自然语言图片意图拦截 ----
    if not command and send_image_fn:
        intent = detect_image_intent(text)
        if intent:
            logger.info(f"图片意图识别: type={intent.intent_type}, desc={intent.description!r}, path={intent.path!r}")
            if intent.intent_type == IMAGE_INTENT_SEND and intent.path:
                try:
                    data, mime = await fs_service._impl.read_binary(intent.path)
                    await send_image_fn(data, f"📷 {Path(intent.path).name}", msg_id)
                    return
                except Exception as e:
                    await send_text_fn(f"读取图片失败: {e}", msg_id)
                    return

            if intent.intent_type == IMAGE_INTENT_GENERATE and intent.description:
                html_content = await mimo.generate_image_html(intent.description)
                if html_content:
                    image_data = await renderer.html_to_image(html_content)
                    if image_data:
                        await send_image_fn(image_data, f"「{intent.description}」", msg_id)
                        return
                await send_text_fn("图片生成失败，请稍后再试。", msg_id)
                return

            if intent.intent_type == IMAGE_INTENT_SEARCH and intent.query:
                # 搜索本地图片（按关键词搜扩展名，过滤目录，只返回图片文件）
                # 搜索范围默认用户主目录和常用图片目录
                search_paths = [
                    str(Path.home()),
                    "E:\\镜像官网插图",
                    "E:\\图片",
                    "E:\\Photos",
                    "D:\\图片",
                ]
                results = []
                for sp in search_paths:
                    try:
                        r = await fs_service.format_find(sp, intent.query, content_search=False)
                        # 过滤出图片文件
                        for line in r.split("\n"):
                            stripped = line.strip()
                            if _IMAGE_EXT_RE.search(stripped):
                                results.append(stripped)
                    except Exception:
                        pass
                if results:
                    # 取第一个图片发送
                    first = results[0]
                    # 提取路径（去序号）
                    parts = first.split()
                    path = parts[-1] if parts else first
                    try:
                        data, _ = await fs_service._impl.read_binary(path)
                        await send_image_fn(data, f"🔍 {intent.query}\n📄 {path}", msg_id)
                        return
                    except Exception as e:
                        await send_text_fn(f"找到图片但读取失败: {e}\n路径: {path}", msg_id)
                        return
                else:
                    await send_text_fn(f"未找到「{intent.query}」相关的图片，试试直接描述你想生成的内容：\n例如：「发一张{intent.query}」", msg_id)
                    return

    # ---- /help ----
    if command == "help":
        help_text = (
            "QQ Bot 指令：\n"
            "/img <描述> — 生成图片\n"
            "/md <问题> — Markdown 渲染\n"
            "/card <问题> — 卡片格式\n"
            "——————————————\n"
            "/fs ls <路径> — 列出目录\n"
            "/fs read <文件> — 读取文件\n"
            "  --lines <行数>  --start <行号>  --encoding <编码>\n"
            "/fs find <路径> --find <模式> — 按文件名搜索\n"
            "/fs find <路径> --grep <关键词> — 按内容搜索\n"
            "/fs touch <路径> — 创建文件\n"
            "  --content <内容>  --overwrite\n"
            "/fs mkdir <路径> — 创建目录\n"
            "/fs rm <路径> — 删除文件/目录\n"
            "  --recursive\n"
            "/fs edit <文件> — 修改文件\n"
            "  --append <内容>  追加到末尾\n"
            "  --replace <内容>  替换全部内容\n"
            "  --insert <行号> <内容>  指定行前插入\n"
            "  --delete-line <行号>  删除指定行\n"
            "  --replace-line <行号> <内容>  替换指定行\n"
            "  --old <原文本> --new <新文本>  全文替换\n"
            "/fs drives — 磁盘信息\n"
            "——————————————\n"
            "/help — 帮助"
        )

        await send_text_fn(help_text, msg_id)
        return

    # ---- /fs 文件系统命令 ----
    if command.startswith("fs_"):
        sub_cmd = command[3:]
        path, opts = parse_fs_args(prompt)

        try:
            if sub_cmd == "ls":
                if not path:
                    path = "C:\\"
                result = await fs_service.format_ls(path)
                await send_text_fn(result, msg_id)

            elif sub_cmd == "read" or sub_cmd == "cat":
                if not path:
                    await send_text_fn("用法: /fs read <文件路径> [--lines 100] [--start 0]", msg_id)
                    return

                # 检测图片文件，直接发送图片而非显示乱码
                image_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
                if Path(path).suffix.lower() in image_exts and send_image_fn:
                    try:
                        data, mime = await fs_service._impl.read_binary(path)
                        await send_image_fn(data, f"📷 {Path(path).name}", msg_id)
                        return
                    except Exception as e:
                        await send_text_fn(f"读取图片失败: {e}", msg_id)
                        return

                result = await fs_service.format_cat(
                    path,
                    max_lines=opts.get("max_lines", 80),
                    start_line=opts.get("start_line", 0),
                )
                await send_text_fn(result, msg_id)

            elif sub_cmd == "find" or sub_cmd == "search":
                if not path:
                    await send_text_fn(
                        "用法: /fs find <路径> --find <文件名模式>\n"
                        "     /fs find <路径> --grep <关键词>", msg_id
                    )
                    return
                pattern = opts.get("pattern", "")
                if not pattern:
                    await send_text_fn("请指定搜索模式: --find <文件名> 或 --grep <关键词>", msg_id)
                    return
                result = await fs_service.format_find(
                    path,
                    pattern,
                    content_search=opts.get("content_search", False),
                )
                await send_text_fn(result, msg_id)

            elif sub_cmd == "drives":
                result = await fs_service.format_drives()
                await send_text_fn(result, msg_id)

            # ★ 新增：touch 创建文件
            elif sub_cmd == "touch":
                if not path:
                    await send_text_fn(
                        "用法: /fs touch <文件路径>\n"
                        "     /fs touch <文件路径> --content <文件内容>\n"
                        "     /fs touch <文件路径> --content <内容> --overwrite", msg_id
                    )
                    return
                result = await fs_service.format_touch(
                    path,
                    content=opts.get("content", ""),
                    encoding=opts.get("encoding", "utf-8"),
                    overwrite=opts.get("overwrite", False),
                )
                await send_text_fn(result, msg_id)

                # ★ 新增：edit 修改文件
            elif sub_cmd == "edit":
                if not path:
                    await send_text_fn(
                        "用法:\n"
                        "/fs edit <文件> --append <内容> — 追加到末尾\n"
                        "/fs edit <文件> --replace <内容> — 替换全部内容\n"
                        "/fs edit <文件> --insert <行号> <内容> — 在指定行前插入\n"
                        "/fs edit <文件> --delete-line <行号> — 删除指定行\n"
                        "/fs edit <文件> --replace-line <行号> <内容> — 替换指定行\n"
                        "/fs edit <文件> --old <原文本> --new <新文本> — 全文替换", msg_id
                    )
                    return

                mode = opts.get("mode", "")
                if not mode:
                    await send_text_fn(
                        "请指定编辑模式:\n"
                        "--append | --replace | --insert | --delete-line | --replace-line | --old --new", msg_id
                    )
                    return

                result = await fs_service.format_edit(
                    path,
                    mode=mode,
                    content=opts.get("content", ""),
                    line=opts.get("line", 0),
                    old_text=opts.get("old_text", ""),
                    new_text=opts.get("new_text", ""),
                    encoding=opts.get("encoding", "utf-8"),
                )
                await send_text_fn(result, msg_id)

                # ★ 新增：rm 删除文件/目录
            elif sub_cmd == "rm" or sub_cmd == "del":
                if not path:
                    await send_text_fn(
                        "用法: /fs rm <文件路径> — 删除文件\n"
                        "     /fs rm <目录路径> — 删除空目录\n"
                        "     /fs rm <目录路径> --recursive — 递归删除非空目录", msg_id
                    )
                    return
                result = await fs_service.format_rm(
                    path,
                    recursive=opts.get("recursive", False),
                )
                await send_text_fn(result, msg_id)

            # ★ 新增：mkdir 创建目录
            elif sub_cmd == "mkdir":
                if not path:
                    await send_text_fn("用法: /fs mkdir <目录路径>", msg_id)
                    return
                result = await fs_service.format_mkdir(path)
                await send_text_fn(result, msg_id)


            else:
                await send_text_fn(
                    f"未知的 fs 子命令: {sub_cmd}\n"
                    "可用: ls, read, find, drives, touch, mkdir", msg_id
                )


        except Exception as e:
            logger.error(f"文件系统命令异常: {e}", exc_info=True)
            await send_text_fn(f"操作失败: {e}", msg_id)

        return



    # ---- /img 生成图片 ----
    if command == "img" and send_image_fn:
        if not prompt:
            await send_text_fn("请描述你想生成的图片，例如：/img 一只可爱的猫咪", msg_id)
            return

        html_content = await mimo.generate_image_html(prompt)
        if html_content:
            image_data = await renderer.html_to_image(html_content)
            if image_data:
                await send_image_fn(image_data, f"「{prompt}」", msg_id)
                return

        await send_text_fn("图片生成失败，请稍后再试。", msg_id)
        return

    # ---- /card 卡片消息 ----
    if command == "card":
        if not prompt:
            prompt = "请简单介绍一下你自己"

        conversation_history[session_key].append({"role": "user", "content": prompt})
        reply = await mimo.chat(
            messages=conversation_history[session_key][-MAX_HISTORY:],
            system_prompt=SYSTEM_PROMPT,
        )
        conversation_history[session_key].append({"role": "assistant", "content": reply})

        lines = [l.strip() for l in reply.split("\n") if l.strip()]
        fields = []
        for i, line in enumerate(lines[:6]):
            if len(line) > 80:
                line = line[:77] + "..."
            fields.append({"name": f"第{i+1}点", "value": line})

        if not fields:
            fields = [{"name": "回复", "value": reply[:200]}]

        await send_text_fn(
            "✦ MiMo 回复\n" + "\n".join(f"▸ {f['name']}: {f['value']}" for f in fields),
            msg_id,
        )
        return

    # ---- /md Markdown 官方渲染 ----
    if command == "md" and send_markdown_fn:
        if not prompt:
            prompt = "请介绍一下你自己"

        conversation_history[session_key].append({"role": "user", "content": prompt})
        reply = await mimo.chat(
            messages=conversation_history[session_key][-MAX_HISTORY:],
            system_prompt=SYSTEM_PROMPT,
        )
        conversation_history[session_key].append({"role": "assistant", "content": reply})

        await send_markdown_fn(reply, msg_id)
        return

    # ---- 普通对话：走 Tool Calling ----
    conversation_history[session_key].append({"role": "user", "content": text})
    if len(conversation_history[session_key]) > MAX_HISTORY:
        conversation_history[session_key] = conversation_history[session_key][-MAX_HISTORY:]

    # ★ 使用 chat_with_tools 自动处理工具调用
    reply = await mimo.chat_with_tools(
        messages=conversation_history[session_key],
        tools=TOOLS,
        tool_executor=execute_tool,
        system_prompt=SYSTEM_PROMPT,
        max_rounds=20,
    )

    conversation_history[session_key].append({"role": "assistant", "content": reply})

    # 若工具链路中已发送图片，则不再额外发送文本，避免重复消息/去重冲突
    if consume_image_sent_flag():
        logger.info("本轮已通过工具发送图片，跳过后续文本回复")
        return

    # 优先使用 QQ 官方 Markdown 渲染
    if send_markdown_fn:
        try:
            await send_markdown_fn(reply, msg_id)
            return
        except Exception as e:
            logger.warning(f"Markdown 发送失败，降级纯文本: {e}")

    # 降级：纯文本
    await send_text_fn(reply, msg_id)


async def handle_c2c_message(event_data: dict):
    """处理 C2C 私聊消息"""
    content = event_data.get("content", "").strip()
    msg_id = event_data.get("id", "")
    author = event_data.get("author", {})
    user_openid = author.get("user_openid", "")

    if msg_id in processed_messages:
        return
    processed_messages.add(msg_id)
    if len(processed_messages) > MAX_PROCESSED:
        processed_messages.clear()

    text = extract_message_text(content)
    if not text:
        await qq_api.send_c2c_message(user_openid, "你好！有什么可以帮你的吗？", msg_id)
        return

    session_key = f"c2c:{user_openid}"
    logger.info(f"私聊 {user_openid}: {text}")

    register_senders(
        send_image=lambda data, cap, mid: qq_api.send_c2c_image(user_openid, data, cap, mid),
        send_text =lambda t,   mid: qq_api.send_c2c_message(user_openid, t, mid),
        default_msg_id=msg_id,
    )
    await _process_and_reply(
        text=text,
        session_key=session_key,
        send_text_fn=lambda t, mid=msg_id: qq_api.send_c2c_message(user_openid, t, mid),
        send_image_fn=lambda data, cap, mid=msg_id: qq_api.send_c2c_image(user_openid, data, cap, mid),
        send_card_fn=lambda title, fields, mid=msg_id: qq_api.send_c2c_card(user_openid, title, fields, mid),
        send_markdown_fn=lambda md, mid=msg_id: qq_api.send_c2c_markdown(user_openid, md, mid),
        msg_id=msg_id,
    )


async def handle_group_at_message(event_data: dict):
    """处理群聊 @消息"""
    content = event_data.get("content", "").strip()
    msg_id = event_data.get("id", "")

    if msg_id in processed_messages:
        return
    processed_messages.add(msg_id)

    group_openid = event_data.get("group_openid", "")
    author_openid = event_data.get("author", {}).get("member_openid", "")

    text = extract_message_text(content)
    if not text:
        return

    session_key = f"group:{group_openid}:{author_openid}"
    logger.info(f"群聊 {group_openid}: {text}")

    register_senders(
        send_image=lambda data, cap, mid: qq_api.send_group_image(group_openid, data, cap, mid),
        send_text =lambda t,   mid: qq_api.send_group_message(group_openid, t, mid),
        default_msg_id=msg_id,
    )
    await _process_and_reply(
        text=text,
        session_key=session_key,
        send_text_fn=lambda t, mid=msg_id: qq_api.send_group_message(group_openid, t, mid),
        send_image_fn=lambda data, cap, mid=msg_id: qq_api.send_group_image(group_openid, data, cap, mid),
        send_card_fn=lambda title, fields, mid=msg_id: qq_api.send_group_card(group_openid, title, fields, mid),
        send_markdown_fn=lambda md, mid=msg_id: qq_api.send_group_markdown(group_openid, md, mid),
        msg_id=msg_id,
    )


async def handle_direct_message(event_data: dict):
    """处理频道私信"""
    content = event_data.get("content", "").strip()
    msg_id = event_data.get("id", "")

    if msg_id in processed_messages:
        return
    processed_messages.add(msg_id)

    author = event_data.get("author", {})
    user_openid = author.get("user_openid", "")
    channel_id = event_data.get("channel_id", "")

    text = extract_message_text(content)
    if not text:
        return

    session_key = f"dm:{user_openid}"
    logger.info(f"频道私信 {user_openid}: {text}")

    register_senders(
        send_image=lambda data, cap, mid: qq_api.send_c2c_image(user_openid, data, cap, mid),
        send_text =lambda t,   mid: qq_api.send_c2c_message(user_openid, t, mid),
        default_msg_id=msg_id,
    )
    await _process_and_reply(
        text=text,
        session_key=session_key,
        send_text_fn=lambda t, mid=msg_id: qq_api.send_c2c_message(user_openid, t, mid),
        send_image_fn=lambda data, cap, mid=msg_id: qq_api.send_c2c_image(user_openid, data, cap, mid),
        send_markdown_fn=lambda md, mid=msg_id: qq_api.send_c2c_markdown(user_openid, md, mid),
        msg_id=msg_id,
    )


async def handle_at_message(event_data: dict):
    """处理频道 @消息"""
    content = event_data.get("content", "").strip()
    msg_id = event_data.get("id", "")

    if msg_id in processed_messages:
        return
    processed_messages.add(msg_id)

    author_id = event_data.get("author", {}).get("id", "")
    channel_id = event_data.get("channel_id", "")

    text = extract_message_text(content)
    if not text:
        return

    session_key = f"channel:{channel_id}:{author_id}"
    logger.info(f"频道 @消息 {channel_id}: {text}")

    register_senders(
        send_image=lambda data, cap, mid: qq_api.send_c2c_image(author_id, data, cap, mid),
        send_text =lambda t,   mid: qq_api.send_channel_message(channel_id, t, mid),
        default_msg_id=msg_id,
    )
    await _process_and_reply(
        text=text,
        session_key=session_key,
        send_text_fn=lambda t, mid=msg_id: qq_api.send_channel_message(channel_id, t, mid),
        send_image_fn=lambda data, cap, mid=msg_id: qq_api.send_c2c_image(author_id, data, cap, mid),
        send_markdown_fn=lambda md, mid=msg_id: qq_api.send_channel_message(channel_id, md, mid),
        msg_id=msg_id,
    )



# ==================== Webhook 路由 ====================

@router.post("/webhook")
async def webhook_handler(request: Request):
    try:
        body = await request.json()
        print(f"\n========== 收到事件: {json.dumps(body, ensure_ascii=False)} ==========\n")

        event = WebhookPayload(**body)

        # op=13: 回调地址验证
        if event.op == 13:
            settings = get_settings()
            d = event.d or {}
            plain_token = d.get("plain_token", "")
            event_ts = d.get("event_ts", "")
            signature = generate_signature(settings.tencent_app_secret, event_ts, plain_token)
            result = {"plain_token": plain_token, "signature": signature}
            print(f"验证响应: {json.dumps(result)}")
            return JSONResponse(content=result)

        # op=0: 事件分发
        event_name = event.t or ""
        event_data = event.d or {}

        if event_name == "C2C_MESSAGE_CREATE":
            await handle_c2c_message(event_data)
        elif event_name == "DIRECT_MESSAGE_CREATE":
            await handle_direct_message(event_data)
        elif event_name == "AT_MESSAGE_CREATE":
            await handle_at_message(event_data)
        elif event_name == "GROUP_AT_MESSAGE_CREATE":
            await handle_group_at_message(event_data)

        return JSONResponse(content={"op": 0})

    except Exception as e:
        logger.error(f"处理 webhook 异常: {e}", exc_info=True)
        return JSONResponse(
            content={"status": "error", "message": str(e)},
            status_code=500,
        )


@router.get("/webhook")
async def webhook_get():
    return {"status": "ok", "message": "QQ Bot webhook is running"}


# ==================== 管理接口 ====================

@router.post("/admin/clear-history")
async def clear_history(session_key: Optional[str] = None):
    if session_key:
        conversation_history.pop(session_key, None)
        return {"status": "cleared", "session": session_key}
    conversation_history.clear()
    return {"status": "cleared_all"}


@router.get("/admin/active-sessions")
async def active_sessions():
    return {
        "sessions": {k: len(v) for k, v in conversation_history.items()},
        "total": len(conversation_history),
    }


@router.post("/admin/test-image")
async def test_image_render(prompt: str = "测试渲染"):
    """管理接口：测试图片渲染"""
    html = await mimo.generate_image_html(prompt)
    if not html:
        return {"status": "error", "message": "HTML 生成失败"}

    image_data = await renderer.html_to_image(html)
    if not image_data:
        return {"status": "error", "message": "图片渲染失败"}

    return {
        "status": "ok",
        "image_size": len(image_data),
        "html_preview": html[:200],
    }

# ==================== 文件系统管理接口 ====================

@router.get("/admin/fs/ls")
async def admin_ls(path: str = "C:\\"):
    """管理接口：列出目录"""
    try:
        return {"status": "ok", "entries": await fs_service.ls(path)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/admin/fs/read")
async def admin_read(path: str, max_lines: int = 200, start_line: int = 0):
    """管理接口：读取文件"""
    try:
        return {"status": "ok", "file": await fs_service.cat(path, max_lines=max_lines, start_line=start_line)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/admin/fs/find")
async def admin_find(directory: str, pattern: str, content: bool = False):
    """管理接口：搜索文件"""
    try:
        return {"status": "ok", "results": await fs_service.find(directory, pattern, content)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/admin/fs/drives")
async def admin_drives():
    """管理接口：磁盘信息"""
    try:
        return {"status": "ok", "drives": await fs_service.disk_info()}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/admin/fs/touch")
async def admin_touch(
    path: str,
    content: str = "",
    encoding: str = "utf-8",
    overwrite: bool = False,
):
    """管理接口：创建文件"""
    try:
        return {"status": "ok", "entry": await fs_service.touch(path, content, encoding, overwrite)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/admin/fs/mkdir")
async def admin_mkdir(path: str):
    """管理接口：创建目录"""
    try:
        return {"status": "ok", "entry": await fs_service.mkdir(path)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.delete("/admin/fs/rm")
async def admin_rm(path: str, recursive: bool = False):
    """管理接口：删除文件或目录"""
    try:
        return {"status": "ok", "result": await fs_service.rm(path, recursive)}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.post("/admin/fs/edit")
async def admin_edit(
    path: str,
    mode: str,
    content: str = "",
    line: int = 0,
    old_text: str = "",
    new_text: str = "",
    encoding: str = "utf-8",
):
    """管理接口：修改文件"""
    try:
        return {
            "status": "ok",
            "file": await fs_service.edit(path, mode, content, line, old_text, new_text, encoding),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}




