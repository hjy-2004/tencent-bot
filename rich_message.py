"""QQ Bot 富消息构建：Markdown、卡片(Ark)、图片、引用回复"""

from typing import Optional


class RichMessage:
    """链式构建 QQ Bot 富消息体"""

    def __init__(self):
        self._content: Optional[str] = None
        self._msg_type: int = 0  # 默认文本
        self._media: Optional[dict] = None
        self._ark: Optional[dict] = None
        self._embed: Optional[dict] = None
        self._markdown: Optional[dict] = None
        self._msg_id: Optional[str] = None
        self._msg_seq: Optional[int] = None
        self._event_id: Optional[str] = None
        self._timestamp: Optional[str] = None

    # ---------- 内容设置 ----------

    def text(self, content: str) -> "RichMessage":
        """纯文本消息"""
        self._content = content
        self._msg_type = 0
        return self

    def markdown(self, md_content: str) -> "RichMessage":
        """Markdown 消息 (msg_type=2)"""
        self._markdown = {"content": md_content}
        self._msg_type = 2
        return self

    def media(self, file_info: str, content: str = "") -> "RichMessage":
        """图片/文件消息 (msg_type=7)"""
        self._media = {"file_uuid": file_info, "file_info": file_info}
        self._content = content
        self._msg_type = 7
        return self

    def ark_template(self, template_id: int, kv_list: list) -> "RichMessage":
        """Ark 卡片消息 (msg_type=3)"""
        self._ark = {
            "template_id": template_id,
            "kv": kv_list,
        }
        self._msg_type = 3
        return self

    def embed_card(self, title: str, prompt: str = "", fields: list = None) -> "RichMessage":
        """Embed 富文本卡片 (msg_type=4)"""
        embed = {"title": title}
        if prompt:
            embed["prompt"] = prompt
        if fields:
            embed["fields"] = [
                {"name": f.get("name", ""), "value": f.get("value", "")}
                for f in fields
            ]
        self._embed = embed
        self._msg_type = 4
        return self

    # ---------- 回复/引用 ----------

    def reply_to(self, msg_id: str, msg_seq: int = 1) -> "RichMessage":
        """设置引用回复"""
        self._msg_id = msg_id
        self._msg_seq = msg_seq
        return self

    def with_event(self, event_id: str, timestamp: str = "") -> "RichMessage":
        """附带事件信息（用于被动回复）"""
        self._event_id = event_id
        self._timestamp = timestamp
        return self

    # ---------- 构建 ----------

    def build(self) -> dict:
        """构建最终 payload"""
        payload = {"msg_type": self._msg_type}

        if self._content is not None:
            payload["content"] = self._content

        if self._markdown is not None:
            payload["markdown"] = self._markdown

        if self._media is not None:
            payload["media"] = self._media

        if self._ark is not None:
            payload["ark"] = self._ark

        if self._embed is not None:
            payload["embed"] = self._embed

        if self._msg_id:
            payload["msg_id"] = self._msg_id

        if self._msg_seq:
            payload["msg_seq"] = self._msg_seq

        if self._event_id:
            payload["event_id"] = self._event_id

        if self._timestamp:
            payload["timestamp"] = self._timestamp

        return payload


# ========== 便捷工厂 ==========

def text_message(content: str, reply_to: str = "") -> dict:
    """快速构建文本消息"""
    m = RichMessage().text(content)
    if reply_to:
        m.reply_to(reply_to)
    return m.build()


def markdown_message(md: str, reply_to: str = "") -> dict:
    """快速构建 Markdown 消息"""
    m = RichMessage().markdown(md)
    if reply_to:
        m.reply_to(reply_to)
    return m.build()


def image_message(file_info: str, content: str = "", reply_to: str = "") -> dict:
    """快速构建图片消息"""
    m = RichMessage().media(file_info, content)
    if reply_to:
        m.reply_to(reply_to)
    return m.build()


def card_message(title: str, fields: list, reply_to: str = "") -> dict:
    """快速构建 Embed 卡片"""
    m = RichMessage().embed_card(title, fields=fields)
    if reply_to:
        m.reply_to(reply_to)
    return m.build()
