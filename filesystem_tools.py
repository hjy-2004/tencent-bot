"""文件系统工具定义（OpenAI Function Calling 格式）+ 执行器"""

import logging
from pathlib import Path
from typing import Callable, Awaitable, Optional
from filesystem import FileSystemService

logger = logging.getLogger(__name__)

fs = FileSystemService()

# 发送器函数类型（由 tencent_bot 注入）
SendImageFn = Callable[[bytes, str, str], Awaitable[dict]]
SendTextFn  = Callable[[str, str], Awaitable[dict]]
_sender_fns: dict = {}


def register_senders(
    send_image: Optional[SendImageFn] = None,
    send_text:  Optional[SendTextFn]  = None,
    default_msg_id: str = "",
) -> None:
    """由 tencent_bot 在初始化时注入发送函数"""
    if send_image is not None:
        _sender_fns["send_image"] = send_image
    if send_text is not None:
        _sender_fns["send_text"] = send_text
    _sender_fns["default_msg_id"] = default_msg_id
    _sender_fns["image_sent_in_tool"] = False


def consume_image_sent_flag() -> bool:
    """读取并清空本轮 fs_send_image 发送标记"""
    sent = bool(_sender_fns.get("image_sent_in_tool", False))
    _sender_fns["image_sent_in_tool"] = False
    return sent


# ==================== 工具 Schema ====================

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "fs_ls",
            "description": "列出指定目录下的所有文件和子目录",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "目录路径，如 C:\\Users、D:\\Projects",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_read",
            "description": "读取文件内容，支持指定行数范围和编码",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件路径，如 D:\\project\\main.py",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "最大读取行数，默认80",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "从第几行开始读取（从0开始），默认0",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_find",
            "description": "搜索文件，支持 glob 通配符（* 匹配任意字符）和多扩展名（用 | 分隔，如 jpg|jpeg|png|gif|bmp|webp），默认返回最多 200 条结果",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "搜索的目录路径",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "搜索关键词、文件名模式或扩展名，支持 * 通配符和 | 分隔多扩展名（如 jpg|jpeg|png）",
                    },
                    "content_search": {
                        "type": "boolean",
                        "description": "是否搜索文件内容（true=内容搜索，false=文件名搜索），默认false",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最大返回结果数，默认 200，建议设为 500 以获得更完整计数",
                    },
                },
                "required": ["directory", "pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_touch",
            "description": "创建新文件，可同时写入内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要创建的文件路径",
                    },
                    "content": {
                        "type": "string",
                        "description": "文件内容，可为空",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "文件已存在时是否覆盖，默认false",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_mkdir",
            "description": "创建新目录（支持递归创建多级目录）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要创建的目录路径",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_rm",
            "description": "删除文件或目录",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要删除的文件或目录路径",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "是否递归删除非空目录，默认false",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_edit",
            "description": "修改文件内容，支持追加、替换、插入、删除行、替换行、全文替换",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要修改的文件路径",
                    },
                    "mode": {
                        "type": "string",
                        "enum": [
                            "append",
                            "replace",
                            "insert",
                            "delete-line",
                            "replace-line",
                            "replace-text",
                        ],
                        "description": (
                            "编辑模式：\n"
                            "- append: 追加内容到末尾\n"
                            "- replace: 替换整个文件内容\n"
                            "- insert: 在指定行前插入内容\n"
                            "- delete-line: 删除指定行\n"
                            "- replace-line: 替换指定行\n"
                            "- replace-text: 全文查找替换"
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "要写入的内容（append/replace/insert/replace-line 模式需要）",
                    },
                    "line": {
                        "type": "integer",
                        "description": "行号（insert/delete-line/replace-line 模式需要，从1开始）",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "要被替换的原文本（replace-text 模式需要）",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "替换后的新文本（replace-text 模式需要）",
                    },
                },
                "required": ["path", "mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_drives",
            "description": "查看所有磁盘分区的使用情况（容量、已用、可用）",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_send_image",
            "description": "读取本地图片文件并发送到聊天窗口（支持 JPG/PNG/GIF/WebP/BMP）。当用户要求发送、查看、发送图片时必须调用此工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "图片文件的完整路径，如 E:\\Photos\\anime\\1.jpg",
                    },
                    "caption": {
                        "type": "string",
                        "description": "图片的描述文字（可选），如「动漫壁纸」「随机图片」",
                    },
                },
                "required": ["path"],
            },
        },
    },
]


# ==================== 工具执行器 ====================

async def execute_tool(name: str, arguments: dict) -> str:
    """
    执行工具调用，返回格式化结果字符串
    """
    try:
        if name == "fs_ls":
            path = arguments.get("path", "C:\\")
            return await fs.format_ls(path)

        elif name == "fs_read":
            return await fs.format_cat(
                path=arguments["path"],
                max_lines=arguments.get("max_lines", 80),
                start_line=arguments.get("start_line", 0),
            )

        elif name == "fs_find":
            return await fs.format_find(
                directory=arguments["directory"],
                pattern=arguments["pattern"],
                content_search=arguments.get("content_search", False),
                max_results=arguments.get("max_results", 500),
            )

        elif name == "fs_touch":
            return await fs.format_touch(
                path=arguments["path"],
                content=arguments.get("content", ""),
                overwrite=arguments.get("overwrite", False),
            )

        elif name == "fs_mkdir":
            return await fs.format_mkdir(path=arguments["path"])

        elif name == "fs_rm":
            return await fs.format_rm(
                path=arguments["path"],
                recursive=arguments.get("recursive", False),
            )

        elif name == "fs_edit":
            return await fs.format_edit(
                path=arguments["path"],
                mode=arguments["mode"],
                content=arguments.get("content", ""),
                line=arguments.get("line", 0),
                old_text=arguments.get("old_text", ""),
                new_text=arguments.get("new_text", ""),
            )

        elif name == "fs_drives":
            return await fs.format_drives()

        elif name == "fs_send_image":
            img_path = arguments["path"]
            caption  = arguments.get("caption", "")
            msg_id = arguments.get("msg_id") or _sender_fns.get("default_msg_id", "")
            send_image = _sender_fns.get("send_image")

            if not send_image:
                return "图片发送功能暂不可用（未注册发送器）。"

            try:
                data, _ = await fs._impl.read_binary(img_path)
                fname   = Path(img_path).name
                result  = await send_image(data, caption, msg_id)
                # _request 成功返回 JSON dict；失败返回 {"status_code": code, "error": "..."}
                is_success = "status_code" not in result or result.get("status_code") in (200, 201)
                if is_success:
                    _sender_fns["image_sent_in_tool"] = True
                    return f"✅ 图片已发送：{fname}"
                else:
                    err = result.get("error", result.get("message", str(result)))
                    return f"❌ 图片发送失败：{err}"
            except FileNotFoundError:
                return f"❌ 文件不存在：{img_path}"
            except Exception as e:
                return f"❌ 读取图片失败：{e}"

        else:
            return f"未知工具: {name}"

    except Exception as e:
        logger.error(f"工具执行异常 [{name}]: {e}", exc_info=True)
        return f"执行失败: {e}"
