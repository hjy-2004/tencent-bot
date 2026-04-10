"""MiMo API 客户端 — 支持流式、图片生成、Markdown 输出、Tool Calling

优化自 Claude Code 的 StreamingToolExecutor:
- 输入 JSON 预验证（提前失败，避免执行时才发现格式错误）
- 并发安全工具并行执行（isConcurrencySafe 标记）
- 单工具独立超时控制
- 工具结果流式输出（边执行边返回）
"""

import asyncio
import base64
import json
import logging
import re
import time
from typing import Optional, Callable, Awaitable, Any, Union

import httpx
from openai import AsyncOpenAI

from config import get_settings

logger = logging.getLogger(__name__)

# 压缩配置
KEEP_RECENT = 8          # 保留最近 8 条不压缩

# 重试配置
MAX_RETRIES = 3
BASE_DELAY_MS = 500
MAX_BACKOFF_MS = 30000

# Token 估算配置
# 模型上下文窗口（保守估计，留 2000 token 余量）
MAX_CONTEXT_TOKENS = 120000

# 压缩阈值（参考 CC: contextWindow - 20000 reserved - 13000 buffer）
# 触发压缩的 prompt tokens 阈值
COMPACT_TOKEN_THRESHOLD = MAX_CONTEXT_TOKENS - 20000 - 13000  # = 87000
# 保留给输出的 token 余量
COMPACT_RESERVED_TOKENS = 20000


# ==================== Token 估算 ====================

def _estimate_tokens_for_message(msg: dict) -> int:
    """
    估算单条消息的 token 数量。
    使用粗略启发式：中文约 2 字符/token，英文约 4 字符/token。
    """
    content = msg.get("content", "") or ""
    if isinstance(content, list):
        # 处理 content 是 blocks 的情况
        content = " ".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in content if isinstance(b, dict) and b.get("type") == "text"
        ) or ""

    # 基础开销（role、格式等）
    overhead = 10

    if not content:
        # tool_calls 或纯 assistant 消息
        if msg.get("tool_calls"):
            tc = msg["tool_calls"][0]
            fname = tc.get("function", {}).get("name", "") or ""
            fargs = tc.get("function", {}).get("arguments", "") or ""
            return overhead + len(fname) // 2 + len(fargs) // 4
        return overhead

    # 中英文混合估算
    chinese_chars = sum(1 for c in content if '\u4e00' <= c <= '\u9fff')
    other_chars = len(content) - chinese_chars
    return overhead + chinese_chars // 2 + other_chars // 4


def estimate_messages_tokens(messages: list[dict]) -> int:
    """估算消息列表的总 token 数"""
    return sum(_estimate_tokens_for_message(m) for m in messages)


def should_compact_tokens(messages: list[dict]) -> bool:
    """
    检查是否需要压缩（基于 token 估算）。
    参考 CC 的 shouldAutoCompact：contextWindow - reservedTokens - bufferTokens
    """
    total = estimate_messages_tokens(messages)
    return total >= COMPACT_TOKEN_THRESHOLD


# ==================== JSON 错误解析 ====================

def _parse_json_error(error: json.JSONDecodeError, raw_args: str) -> str:
    msg = str(error.msg)
    pos = error.pos
    if "Unterminated string" in msg:
        snippet = repr(raw_args[max(0, pos-20):pos+20])
        return (f"Unterminated string. Pos: line {error.lineno} col {error.colno} char {pos}. "
                f"Check: (1) quotes not closed, (2) backslash not escaped (C:\\test), (3) newlines. "
                f"Snippet: {snippet}")
    elif "Expecting" in msg:
        return (f"JSON syntax error: {msg}. Pos: line {error.lineno} col {error.colno}. "
                f"Check: (1) missing comma, (2) key without quotes, (3) trailing comma.")
    else:
        return f"JSON error: {msg} at line {error.lineno} col {error.colno}"


# ==================== 同文件连续编辑检测 ====================

def _check_repeated_edits(
    tool_results: list[dict],
    recent_edits: list[tuple[str, str]],  # [(path, mode), ...]
    max_consecutive: int = 3,
) -> Optional[str]:
    """
    检测是否对同一文件进行了连续编辑。
    返回警告信息，如果超过阈值则提示 LLM 确认。
    """
    for item in tool_results:
        if not isinstance(item, dict):
            continue
        content = item.get("content", "")
        # 从 tool_call_id 提取工具名
        tool_name = None
        # 检查是否是 fs_edit/fs_touch 等写操作
        if "文件已修改" in content or "文件已创建" in content or "文件已原子写入" in content:
            # 尝试从结果中提取路径
            for line in content.split("\n"):
                if "D:\\" in line or "C:\\" in line:
                    # 提取路径
                    import re
                    match = re.search(r"([A-Z]:\\[^\s]+)", line)
                    if match:
                        path = match.group(1).rstrip(".")
                        recent_edits.append((path, "edit"))

    # 检查连续编辑同一文件
    if len(recent_edits) >= max_consecutive:
        paths = [p for p, _ in recent_edits[-max_consecutive:]]
        if len(set(paths)) == 1:
            return (
                f"注意：你已连续 {max_consecutive} 次编辑同一文件 "
                f"({paths[0]})。\n"
                f"请确认是否完成？如需继续编辑请说明原因。\n"
                f"如已完成，请返回最终结果而非继续调用 fs_edit/fs_touch。"
            )
    return None


def _is_write_tool(name: str) -> bool:
    """判断是否为写操作工具"""
    return name in ("fs_edit", "fs_touch", "fs_mkdir", "fs_rm", "fs_send_image")


def _is_retryable_error(error: Exception) -> bool:
    """判断错误是否可重试"""
    error_str = str(error).lower()
    status_code = None

    # 尝试提取状态码
    if hasattr(error, 'status_code'):
        status_code = error.status_code
    elif hasattr(error, 'response') and hasattr(error.response, 'status_code'):
        status_code = error.response.status_code

    # 429/529/500/502/503 可重试
    if status_code in (429, 500, 502, 503, 529):
        return True
    # 服务端超时可重试
    if "timeout" in error_str or "timed out" in error_str:
        return True
    # 连接错误可重试
    if "connection" in error_str or "network" in error_str:
        return True
    return False


def _is_auth_error(error: Exception) -> bool:
    """判断是否是认证/授权错误（不可重试）"""
    error_str = str(error).lower()
    if hasattr(error, 'status_code'):
        return error.status_code in (401, 403, 402)
    return "auth" in error_str or "credential" in error_str or "unauthorized" in error_str


def _get_error_status_code(error: Exception) -> Optional[int]:
    """从错误中提取状态码"""
    if hasattr(error, 'status_code'):
        return error.status_code
    if hasattr(error, 'response') and hasattr(error.response, 'status_code'):
        return error.response.status_code
    return None


async def _sleep_with_jitter(base_ms: int, attempt: int) -> None:
    """指数退避 + 随机抖动"""
    import random
    import asyncio
    delay = min(base_ms * (2 ** attempt) + random.randint(0, base_ms), MAX_BACKOFF_MS)
    await asyncio.sleep(delay / 1000)


# ==================== 工具执行器 ====================

class TrackedTool:
    """跟踪中的工具"""
    def __init__(
        self,
        id: str,
        name: str,
        args: dict,
        is_concurrency_safe: bool = True,
    ):
        self.id = id
        self.name = name
        self.args = args
        self.is_concurrency_safe = is_concurrency_safe
        self.status = "queued"  # queued | executing | completed | yielded
        self.result: Optional[dict] = None
        self.error: Optional[str] = None
        self.start_time: float = 0
        self.progress: list = []  # 进度消息


class StreamingToolExecutor:
    """
    流式工具执行器 - 参考 Claude Code 的 StreamingToolExecutor

    核心优化：
    1. 并发安全工具可并行执行
    2. 独立超时控制
    3. 进度实时输出
    4. 结果按顺序返回（即使并行执行）
    """

    def __init__(
        self,
        tool_executor: Callable[[str, dict], Awaitable[str]],
        concurrency_safe_tools: set[str] = None,
        default_timeout: float = 30.0,
    ):
        """
        Args:
            tool_executor: 工具执行函数 (name, args) -> result_str
            concurrency_safe_tools: 并发安全工具集合（可并行执行）
            default_timeout: 默认超时时间（秒）
        """
        self._executor = tool_executor
        self._safe_tools = concurrency_safe_tools or {"fs_ls", "fs_read", "fs_find", "fs_drives"}
        self._default_timeout = default_timeout
        self._tools: list[TrackedTool] = []
        self._aborted = False

    def add_tool(
        self,
        id: str,
        name: str,
        args: dict,
        timeout: float = None,
    ) -> None:
        """添加工具到执行队列"""
        tool = TrackedTool(
            id=id,
            name=name,
            args=args,
            is_concurrency_safe=name in self._safe_tools,
        )
        # 为每个工具存储超时时间
        tool.timeout = timeout or self._default_timeout
        self._tools.append(tool)

    def _can_execute(self, tool: TrackedTool) -> bool:
        """检查工具是否可以执行"""
        if self._aborted:
            return False

        # 正在执行的工具
        executing = [t for t in self._tools if t.status == "executing"]

        if not executing:
            return True

        # 并发安全工具：只要没有非安全工具在执行就可以
        if tool.is_concurrency_safe:
            return all(t.is_concurrency_safe for t in executing)

        # 非安全工具：必须单独执行
        return False

    async def _execute_tool(self, tool: TrackedTool) -> None:
        """执行单个工具"""
        tool.status = "executing"
        tool.start_time = time.time()

        try:
            result = await asyncio.wait_for(
                self._executor(tool.name, tool.args),
                timeout=tool.timeout,
            )
            tool.result = {
                "role": "tool",
                "tool_call_id": tool.id,
                "content": result,
            }
        except asyncio.TimeoutError:
            tool.error = f"执行超时（{tool.timeout}s），请简化操作后重试。"
            logger.warning(f"工具 {tool.name} 执行超时（{tool.timeout}s）")
        except Exception as e:
            tool.error = f"执行异常: {e}"
            logger.error(f"工具 {tool.name} 执行异常: {e}")
        finally:
            tool.status = "completed"

    async def execute_all(self) -> list[dict]:
        """
        执行所有工具，返回结果列表（按原始顺序）

        优化点：
        - 并发安全工具并行执行
        - 非安全工具串行执行
        """
        if not self._tools:
            return []

        # 第一阶段：执行所有并发安全的工具（并行）
        safe_tools = [t for t in self._tools if t.is_concurrency_safe and t.status == "queued"]
        if safe_tools:
            await asyncio.gather(
                *[self._execute_tool(t) for t in safe_tools],
                return_exceptions=True,
            )

        # 第二阶段：执行非安全工具（串行，保持顺序）
        unsafe_tools = [t for t in self._tools if not t.is_concurrency_safe and t.status == "queued"]
        for tool in unsafe_tools:
            await self._execute_tool(tool)
            # 如果失败且是危险操作（如写文件），停止后续执行
            if tool.error and tool.name in ("fs_edit", "fs_touch", "fs_rm"):
                break

        # 按原始顺序返回结果
        results = []
        for tool in self._tools:
            if tool.error:
                results.append({
                    "role": "tool",
                    "tool_call_id": tool.id,
                    "content": tool.error,
                    "is_error": True,
                })
            elif tool.result:
                results.append(tool.result)

        return results

    def abort(self) -> None:
        """中止所有正在执行的工具"""
        self._aborted = True


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

        # ==================== 工具执行优化配置 ====================
        # 并发安全工具：可并行执行，不阻塞其他工具
        self._concurrency_safe_tools = {
            "fs_ls",      # 读目录
            "fs_read",    # 读文件
            "fs_find",    # 搜索文件
            "fs_drives",  # 获取驱动器
        }

        # 各工具超时时间（秒）
        self._tool_timeouts = {
            "fs_ls": 10.0,
            "fs_read": 15.0,
            "fs_find": 20.0,
            "fs_drives": 5.0,
            "fs_touch": 15.0,
            "fs_mkdir": 10.0,
            "fs_rm": 10.0,
            "fs_edit": 20.0,
            "fs_send_image": 30.0,
        }
        self._default_tool_timeout = 30.0

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
        if not should_compact_tokens(messages):
            return messages  # 没超 token 阈值，不压缩

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

        for attempt in range(MAX_RETRIES + 1):
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
                if attempt < MAX_RETRIES and _is_retryable_error(e):
                    status = _get_error_status_code(e)
                    logger.warning(
                        f"MiMo API 可重试错误 (attempt {attempt + 1}/{MAX_RETRIES + 1}): "
                        f"{type(e).__name__} status={status} - {e}"
                    )
                    await _sleep_with_jitter(BASE_DELAY_MS, attempt)
                    continue

                # 认证/授权错误不重试
                if _is_auth_error(e):
                    logger.error(f"MiMo 认证错误（不重试）: {e}")
                    return "抱歉，AI 服务认证失败，请检查配置。"

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
    #  工具调用循环（集成上下文压缩 + 并行执行优化）
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
        自动工具调用循环 + 上下文压缩 + 并行执行优化：

        优化点（来自 Claude Code StreamingToolExecutor）：
        1. JSON 输入预验证 - 提前失败，避免执行时才发现格式错误
        2. 并发安全工具并行执行 - fs_ls/fs_read/fs_find 可同时执行
        3. 单工具独立超时 - 不同工具不同超时时间
        4. 工具结果流式压缩 - 边执行边压缩
        """
        payload_messages = []
        if system_prompt:
            payload_messages.append({"role": "system", "content": system_prompt})
        payload_messages.extend(messages)

        # 同文件连续编辑检测
        recent_edits: list[tuple[str, str]] = []  # [(path, tool_name), ...]

        # 首次发送前：检查是否需要压缩（避免超限）
        if should_compact_tokens(payload_messages):
            estimated = estimate_messages_tokens(payload_messages)
            logger.info(f"初始压缩触发：估算 {estimated} tokens（阈值 {COMPACT_TOKEN_THRESHOLD}）")
            payload_messages = await self._compact_messages(payload_messages)

        for round_num in range(max_rounds):
            # ⚡ 每轮 API 调用前：检查是否需要压缩（基于 token 估算）
            if should_compact_tokens(payload_messages):
                estimated = estimate_messages_tokens(payload_messages)
                logger.info(f"第{round_num+1}轮前压缩触发：估算 {estimated} tokens（阈值 {COMPACT_TOKEN_THRESHOLD}）")
                payload_messages = await self._compact_messages(payload_messages)

            logger.info(
                f"chat_with_tools 第{round_num+1}轮 | "
                f"payload_messages条数={len(payload_messages)} | "
                f"roles={[m['role'] for m in payload_messages]}"
            )

            try:
                success = False
                for attempt in range(MAX_RETRIES + 1):
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
                        success = True
                        break

                    except Exception as e:
                        if attempt < MAX_RETRIES and _is_retryable_error(e):
                            status = _get_error_status_code(e)
                            logger.warning(
                                f"chat_with_tools 第{round_num + 1}轮 可重试错误 "
                                f"(attempt {attempt + 1}/{MAX_RETRIES + 1}): "
                                f"{type(e).__name__} status={status} - {e}"
                            )
                            await _sleep_with_jitter(BASE_DELAY_MS, attempt)
                            continue

                        if _is_auth_error(e):
                            return "抱歉，AI 服务认证失败，请检查配置。"

                        raise

                if not success:
                    raise Exception("重试次数耗尽")

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

                # ========== 工具执行（参考 CC：不做 JSON 预验证，让 tool 自然报错）==========

                # ⚡ 硬拦截：同文件连续写操作检测（第 3 次时阻止）
                blocked_calls: list[tuple] = []  # [(tc_id, name, reason), ...]
                if len(recent_edits) >= 3:
                    paths = [p for p, _ in recent_edits[-3:]]
                    if len(set(paths)) == 1:
                        # 阻止本次所有写操作
                        for tc in message.tool_calls:
                            if _is_write_tool(tc.function.name):
                                blocked_calls.append((
                                    tc.id,
                                    tc.function.name,
                                    f"已连续 3 次编辑同一文件 ({paths[0]})，已阻止本次操作。"
                                ))
                        logger.warning(f"同文件连续编辑检测触发，阻止 {len(blocked_calls)} 个写操作")

                # 创建流式执行器（并发安全工具并行执行）
                executor = StreamingToolExecutor(
                    tool_executor=tool_executor,
                    concurrency_safe_tools=self._concurrency_safe_tools,
                    default_timeout=self._default_tool_timeout,
                )

                # 添加所有工具调用（不预验证 JSON，让执行时自然报错）
                for tc in message.tool_calls:
                    name = tc.function.name
                    # 跳过被阻止的写操作
                    if any(bc[0] == tc.id for bc in blocked_calls):
                        continue
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        # 传递原始字符串，由 tool 执行时处理错误
                        args = tc.function.arguments
                    timeout = self._tool_timeouts.get(name, self._default_tool_timeout)
                    executor.add_tool(tc.id, name, args, timeout=timeout)

                # 3. 执行所有工具（自动并行/串行切换）
                logger.info(f"执行 {len(message.tool_calls)} 个工具调用")
                tool_results = await executor.execute_all()

                # 4. 加入被阻止的结果（硬拦截）
                for tc_id, name, reason in blocked_calls:
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": reason,
                        "is_error": True,
                    })

                # 5. 压缩结果后加入历史
                for item in tool_results:
                    if isinstance(item, dict) and "content" in item:
                        tool_name = None
                        # 从原始 tool_calls 中找到工具名
                        for tc in message.tool_calls:
                            if tc.id == item.get("tool_call_id"):
                                tool_name = tc.function.name
                                break
                        if tool_name:
                            item["content"] = self._compress_tool_result(
                                item["content"], tool_name
                            )
                    payload_messages.append(item)

                # ⚡ 同文件连续编辑检测 - 更新计数器
                if _is_write_tool(tool_name or ""):
                    # 从结果中提取路径
                    content = item.get("content", "") if isinstance(item, dict) else ""
                    import re
                    for line in content.split("\n"):
                        match = re.search(r"([A-Z]:\\[^ \n]+)", line)
                        if match:
                            path = match.group(1).rstrip(".").rstrip("\\")
                            recent_edits.append((path, tool_name or ""))
                            break

                # 硬拦截后，重置计数器（允许后续不同文件的编辑）
                if blocked_calls:
                    recent_edits.clear()

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
