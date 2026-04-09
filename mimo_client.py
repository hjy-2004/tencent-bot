"""MiMo API 客户端 — 支持流式、图片生成、Markdown 输出、Tool Calling"""

import base64
import json
import logging
import re
from typing import Optional

import httpx
from openai import AsyncOpenAI

from config import get_settings

logger = logging.getLogger(__name__)

# 压缩配置
COMPACT_THRESHOLD = 24   # 超过 24 条消息才压缩，减少额外摘要请求
KEEP_RECENT = 8          # 保留最近 8 条不压缩


class MiMoClient:
    def __init__(self):
        settings = get_settings()
        self.model = settings.mimo_model
        self.max_tokens = settings.mimo_max_tokens
        self.temperature = settings.mimo_temperature
        self.top_p = settings.mimo_top_p

        self.glm_api_key = settings.glm_api_key
        self.glm_api_base = settings.glm_api_base
        self.glm_model = settings.glm_model

        self.deepseek_api_key = settings.deepseek_api_key
        self.deepseek_api_base = settings.deepseek_api_base
        self.deepseek_model = settings.deepseek_model

        self.default_text_provider = (settings.text_provider or "auto").strip().lower()

        self.image_api_key = settings.image_api_key
        self.image_api_base = settings.image_api_base
        self.image_model = settings.image_model

        logger.info("MiMoClient 初始化完成")

        self.client = AsyncOpenAI(
            api_key=settings.mimo_api_key,
            base_url=settings.mimo_api_base,
        )
        self.glm_client = AsyncOpenAI(
            api_key=self.glm_api_key,
            base_url=self.glm_api_base,
        ) if self.glm_api_key else None
        self.deepseek_client = AsyncOpenAI(
            api_key=self.deepseek_api_key,
            base_url=self.deepseek_api_base,
        ) if self.deepseek_api_key else None
        self.image_client = AsyncOpenAI(
            api_key=self.image_api_key,
            base_url=self.image_api_base,
        ) if self.image_api_key else None

    def _provider_available(self, provider: str) -> bool:
        if provider == "deepseek":
            return bool(self.deepseek_client)
        if provider == "glm":
            return bool(self.glm_client)
        if provider == "mimo":
            return bool(self.client)
        return False

    def _pick_provider(self, provider: Optional[str] = None) -> str:
        selected = (provider or self.default_text_provider or "auto").strip().lower()
        if selected in ("mimo", "glm", "deepseek"):
            if self._provider_available(selected):
                return selected
            logger.warning(f"指定 provider={selected} 不可用，回退自动路由")

        if self.deepseek_client:
            return "deepseek"
        if self.glm_client:
            return "glm"
        return "mimo"

    def _get_text_client(self, provider: Optional[str] = None) -> AsyncOpenAI:
        picked = self._pick_provider(provider)
        if picked == "deepseek":
            return self.deepseek_client
        if picked == "glm":
            return self.glm_client
        return self.client

    def _get_text_model(self, provider: Optional[str] = None) -> str:
        picked = self._pick_provider(provider)
        if picked == "deepseek":
            return self.deepseek_model
        if picked == "glm":
            return self.glm_model
        return self.model

    def _get_text_provider_info(self, provider: Optional[str] = None) -> tuple[str, AsyncOpenAI, str]:
        picked = self._pick_provider(provider)
        return picked, self._get_text_client(picked), self._get_text_model(picked)

    def get_text_provider_status(self, selected_provider: Optional[str] = None) -> dict:
        selected = (selected_provider or self.default_text_provider or "auto").strip().lower()
        return {
            "selected": selected,
            "active": self._pick_provider(selected),
            "available": {
                "mimo": self._provider_available("mimo"),
                "glm": self._provider_available("glm"),
                "deepseek": self._provider_available("deepseek"),
            },
            "models": {
                "mimo": self.model,
                "glm": self.glm_model,
                "deepseek": self.deepseek_model,
            },
        }

    def set_default_text_provider(self, provider: str) -> dict:
        normalized = (provider or "").strip().lower()
        if normalized not in ("auto", "mimo", "glm", "deepseek"):
            raise ValueError("provider 必须是 auto/mimo/glm/deepseek 之一")

        self.default_text_provider = normalized
        active = self._pick_provider(normalized)
        logger.info(f"文本模型路由已切换: selected={normalized}, active={active}")
        return self.get_text_provider_status(normalized)

    # ──────────────────────────────────────────────
    #  核心：上下文压缩
    # ──────────────────────────────────────────────

    async def _compact_messages(self, messages: list[dict]) -> list[dict]:
        """
        压缩消息历史：
        - system prompt 永远保留
        - 最近 KEEP_RECENT 条保留原样
        - 中间旧消息交给 LLM 做摘要
        """
        if len(messages) <= COMPACT_THRESHOLD:
            return messages  # 没超阈值，不压缩

        # 三刀切分
        system_msg = messages[0]                    # system prompt
        old_messages = messages[1:-KEEP_RECENT]     # 旧消息 → 压缩
        recent_messages = messages[-KEEP_RECENT:]   # 最近消息 → 保留

        # 把旧消息拼成文本
        old_text = ""
        for msg in old_messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")

            if role == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    fname = tc["function"]["name"]
                    fargs = tc["function"]["arguments"]
                    old_text += f"[assistant]: 调用工具 {fname}({fargs})\n"
            elif role == "tool":
                # 工具结果可能很长，截取前 500 字符
                preview = content[:500] + ("..." if len(content) > 500 else "")
                old_text += f"[tool]: {preview}\n"
            elif content:
                old_text += f"[{role}]: {content}\n"

        # 调用文本模型生成摘要
        text_client = self._get_text_client()
        text_model = self._get_text_model()
        try:
            summary_response = await text_client.chat.completions.create(
                model=text_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "请将以下对话历史压缩成一段简洁的中文摘要。"
                            "保留所有重要事实：文件路径、命令结果、关键数据、已完成的步骤、"
                            "发现的问题。不要遗漏关键细节，但去掉冗余输出。"
                        ),
                    },
                    {"role": "user", "content": old_text},
                ],
                max_completion_tokens=800,
                temperature=0.3,
            )
            summary = summary_response.choices[0].message.content
            logger.info(
                f"上下文压缩完成 | "
                f"原始 {len(messages)} 条 → 压缩后 {KEEP_RECENT + 3} 条 | "
                f"摘要长度 {len(summary)} 字符"
            )
        except Exception as e:
            logger.warning(f"摘要生成失败，降级为保留原始上下文: {e}")
            # 降级：保留原始消息，不压缩（避免上下文丢失）
            return messages

        # 重新组装
        return [
            system_msg,
            {"role": "user", "content": f"[之前的对话摘要]: {summary}"},
            {"role": "assistant", "content": "明白了，我已了解之前的上下文，继续工作。"},
            *recent_messages,
        ]

    # ──────────────────────────────────────────────
    #  工具结果压缩（防止单条消息过长）
    # ──────────────────────────────────────────────

    @staticmethod
    def _compress_tool_result(result: str, tool_name: str, max_len: int = 2000) -> str:
        """压缩工具返回结果，防止单条消息过长"""
        if len(result) <= max_len:
            return result

        # 针对文件列表类工具
        if tool_name in ("fs_ls", "fs_list", "fs_find", "fs_search"):
            lines = [l for l in result.strip().split("\n") if l.strip()]
            total = len(lines)
            if total > 20:
                return (
                    f"共 {total} 项，前 15 项:\n"
                    + "\n".join(lines[:15])
                    + f"\n... (共 {total} 项，已截断，大量文件未列出)"
                )

        # 通用截断
        return result[:max_len] + f"\n... (已截断，原长度 {len(result)} 字符)"

    # ──────────────────────────────────────────────
    #  标准对话
    # ──────────────────────────────────────────────

    async def chat(
        self,
        messages: list[dict],
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """标准对话，返回纯文本"""
        payload_messages = []
        if system_prompt:
            payload_messages.append({"role": "system", "content": system_prompt})
        payload_messages.extend(messages)

        text_client = self._get_text_client()
        text_model = self._get_text_model()

        try:
            completion = await text_client.chat.completions.create(
                model=text_model,
                messages=payload_messages,
                max_completion_tokens=max_tokens or self.max_tokens,
                temperature=temperature or self.temperature,
                top_p=self.top_p,
                stream=False,
                frequency_penalty=0,
                presence_penalty=0,
            )
            reply = completion.choices[0].message.content
            usage = completion.usage
            logger.info(
                f"MiMo 响应成功 | "
                f"prompt={usage.prompt_tokens} | "
                f"completion={usage.completion_tokens}"
            )
            return reply

        except Exception as e:
            import traceback
            print(f"\n{'!'*50}")
            print(f"MiMo 调用失败详情:")
            print(f"  类型: {type(e).__name__}")
            print(f"  信息: {e}")
            traceback.print_exc()
            print(f"{'!'*50}\n")
            logger.error(f"MiMo API 异常: {e}", exc_info=True)
            return "抱歉，AI 服务暂时不可用，请稍后再试。"

    # ──────────────────────────────────────────────
    #  工具调用循环（集成上下文压缩）
    # ──────────────────────────────────────────────

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list,
        tool_executor,
        system_prompt: Optional[str] = None,
        max_rounds: int = 15,
    ) -> str:
        """
        自动工具调用循环 + 上下文压缩：
        1. 每轮开始前检查消息数量，超阈值就压缩
        2. 发送消息 + tools 给 MiMo
        3. 如果 MiMo 调用工具 → 执行 → 结果压缩后返回
        4. 重复直到 MiMo 给出最终回复或达到最大轮数
        """
        payload_messages = []
        if system_prompt:
            payload_messages.append({"role": "system", "content": system_prompt})
        payload_messages.extend(messages)

        for round_num in range(max_rounds):
            # ⚡ 每轮开始前：压缩上下文
            payload_messages = await self._compact_messages(payload_messages)
            logger.info(
                f"chat_with_tools 第{round_num+1}轮 | "
                f"payload_messages条数={len(payload_messages)} | "
                f"roles={[m['role'] for m in payload_messages]}"
            )

            try:
                text_client = self._get_text_client()
                kwargs = {
                    "model": self._get_text_model(),
                    "messages": payload_messages,
                    "max_completion_tokens": self.max_tokens,
                    "temperature": self.temperature,
                    "top_p": self.top_p,
                    "stream": False,
                    "tools": tools,
                    "tool_choice": "auto",
                }

                completion = await text_client.chat.completions.create(**kwargs)
                message = completion.choices[0].message

                logger.info(
                    f"MiMo 第{round_num + 1}轮 | "
                    f"消息数={len(payload_messages)} | "
                    f"prompt={completion.usage.prompt_tokens} | "
                    f"completion={completion.usage.completion_tokens}"
                )

                # 没有工具调用 → 返回最终回复
                if not message.tool_calls:
                    return message.content or "（无回复）"

                # 有工具调用 → 把 assistant 消息加入历史
                payload_messages.append({
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in message.tool_calls
                    ],
                })

                # 执行每个工具调用，压缩结果后加入历史
                for tc in message.tool_calls:
                    name = tc.function.name
                    args = json.loads(tc.function.arguments)
                    logger.info(f"执行工具: {name}({args})")

                    result = await tool_executor(name, args)

                    # ⚡ 压缩工具结果
                    compressed = self._compress_tool_result(result, name)

                    payload_messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": compressed,
                    })

            except Exception as e:
                error_str = str(e)
                if "402" in error_str or "insufficient_balance" in error_str.lower() or "payment required" in error_str.lower():
                    logger.error(f"账户余额不足: {e}")
                    return "文本模型余额不足。若你是在要生成图片，请使用 /img + 描述（例如：/img 海边美女）。"
                if "429" in error_str or "token" in error_str.lower() or "rate limit" in error_str.lower():
                    logger.error(f"Token/速率限制: {e}")
                    return "请求内容过多或频率过高，请稍后重试或简化问题。"
                logger.error(f"工具调用循环异常 (round {round_num + 1}): {e}", exc_info=True)
                return f"处理过程中出错: {e}"

        return "操作轮数过多，请简化你的请求后重试。"

    # ──────────────────────────────────────────────
    #  Markdown 对话
    # ──────────────────────────────────────────────

    async def chat_markdown(
        self,
        messages: list[dict],
        system_prompt: Optional[str] = None,
    ) -> str:
        """对话并要求 MiMo 输出 Markdown 格式"""
        md_system = (system_prompt or "") + """

请用 Markdown 格式组织你的回答：
- 使用标题（##、###）组织结构
- 代码用 ```language 代码块包裹
- 重点内容用 **加粗**
- 列表用 - 或 1.
- 适当使用引用 > """
        return await self.chat(messages, system_prompt=md_system)


    async def generate_image(self, prompt: str) -> Optional[bytes]:
        """调用第三方文生图接口，返回第一张图片字节"""
        if not self.image_client:
            logger.error("IMAGE_API_KEY 未配置")
            return None

        try:
            response = await self.image_client.images.generate(
                model=self.image_model,
                prompt=prompt,
                extra_body={
                    "negative_prompt": "blurry ugly bad",
                    "num_inference_steps": 9,
                    "guidance_scale": 1,
                    "image_scale": 1,
                },
            )

            if not response.data:
                logger.error("文生图返回为空")
                return None

            first = response.data[0]
            if first.url:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    img_resp = await client.get(first.url)
                    img_resp.raise_for_status()
                    return img_resp.content

            if first.b64_json:
                return base64.b64decode(first.b64_json)

            logger.error("文生图返回既无 url 也无 b64_json")
            return None
        except Exception as e:
            logger.error(f"文生图调用失败: {e}", exc_info=True)
            return None

    # ──────────────────────────────────────────────
    #  图片 HTML 生成
    # ──────────────────────────────────────────────

    async def generate_image_html(
        self,
        prompt: str,
        style: str = "modern",
    ) -> Optional[str]:
        """让 MiMo 生成可渲染为图片的 HTML 内容"""
        system = """你是一个 HTML 设计师。根据用户描述，生成一个精美的 HTML 片段。
要求：
- 不要包含 <html> <head> <body> 标签，只输出内联内容
- 使用 inline style
- 背景色深色渐变，文字浅色
- 字体用系统中文字体
- 适合截图分享
- 宽度 640px，高度自适应"""

        messages_payload = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"请生成：{prompt}\n风格：{style}"},
        ]

        try:
            html = await self.chat(messages_payload, max_tokens=2048)
            html = re.sub(r"^```html?\s*", "", html.strip())
            html = re.sub(r"\s*```$", "", html.strip())
            return html
        except Exception as e:
            logger.error(f"HTML 生成失败: {e}")
            return None

    # ──────────────────────────────────────────────
    #  流式对话
    # ──────────────────────────────────────────────

    async def chat_stream(
        self,
        messages: list[dict],
        system_prompt: Optional[str] = None,
    ):
        """流式对话，yield 每个文本片段"""
        payload_messages = []
        if system_prompt:
            payload_messages.append({"role": "system", "content": system_prompt})
        payload_messages.extend(messages)

        try:
            text_client = self._get_text_client()
            stream = await text_client.chat.completions.create(
                model=self._get_text_model(),
                messages=payload_messages,
                max_completion_tokens=self.max_tokens,
                temperature=self.temperature,
                top_p=self.top_p,
                stream=True,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta
                if delta.content:
                    yield delta.content

        except Exception as e:
            logger.error(f"流式调用异常: {e}", exc_info=True)
            yield "抱歉，AI 服务暂时不可用。"

    async def close(self):
        await self.client.close()
        if self.glm_client:
            await self.glm_client.close()
        if self.image_client:
            await self.image_client.close()
