# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A QQ bot powered by Xiaomi MiMo-V2-Pro AI, with support for multiple text models (MiMo/GLM/DeepSeek auto-routing), image generation, file system operations, and rich media messages (Markdown, cards, images).

## Run Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Install Playwright browser (run once)
python install.py

# Start the bot (development with auto-reload)
python main.py

# Run tests
python test_mimo.py
python test_webhook.py
python test_verify.py
```

## Architecture

```
main.py                 # FastAPI app + lifespan (cleanup on shutdown)
├── tencent_bot.py      # Bot core: webhook handler, QQ API, message routing
│   ├── QQBotAPI        # C2C/group/channel message + image upload
│   ├── _dispatch_event # Routes events: C2C_MESSAGE_CREATE, GROUP_AT_MESSAGE_CREATE, etc.
│   └── _process_and_reply  # Intent detection → command/image/AI handling
├── mimo_client.py      # Multi-provider AI client
│   ├── text routing    # auto/mimo/glm/deepseek (provider picks first available)
│   ├── chat_with_tools # Tool-calling loop with context compression (>24 msgs)
│   ├── _compact_messages  # Summarizes old messages to stay within context
│   └── generate_image  # Third-party image API (z-image-turbo)
├── config.py           # Pydantic BaseSettings from .env
├── image_renderer.py   # Markdown/HTML → PNG via Playwright (ThreadPoolExecutor)
├── filesystem_tools.py # OpenAI Function Calling tools + executor
│   └── TOOLS           # fs_ls, fs_read, fs_find, fs_send_image, fs_edit, fs_touch, fs_mkdir, fs_rm, fs_drives
└── filesystem/        # Cross-platform FS abstraction
    ├── base.py         # Abstract class + dataclasses (FileEntry, FileContent, SearchResult)
    ├── service.py      # FileSystemService (factory + format_* output methods)
    └── windows.py      # WindowsFileSystem implementation
```

## Key Patterns

**Text Model Routing**: `mimo_client.py` — `text_provider` setting or `/model <auto|mimo|glm|deepseek>` command switches active provider. Provider availability is checked at runtime; fallback order is deepseek → glm → mimo.

**Context Compression**: `chat_with_tools` in `mimo_client.py` compresses message history when it exceeds 24 messages, keeping system prompt + recent 8 messages + LLM-generated summary of older messages.

**Tool Result Compression**: `_compress_tool_result` truncates long tool results (e.g., file listings) to prevent single-message overflow.

**Image Intent Detection**: `detect_image_intent` in `tencent_bot.py` parses natural language to route to IMAGE_INTENT_GENERATE (calls image API), IMAGE_INTENT_SEARCH (searches local dirs), or IMAGE_INTENT_SEND (sends existing file).

**Session Concurrency**: Per-session `asyncio.Lock` in `_run_session_serialized` prevents interleaved replies for the same user/group conversation.

**Webhook Verification**: Tencent sends op=13 for callback URL verification — responds with Ed25519 signature computed from `app_secret + event_ts + plain_token`.

## QQ API Message Types

- `msg_type=0` — plain text
- `msg_type=2` — Markdown (QQ official rendering)
- `msg_type=7` — image (media file uploaded first, then referenced by `file_info`)
- Image compression: PIL → JPEG, max 300KB, max 800px width (done before upload)

## Dependencies

Core: `fastapi`, `uvicorn`, `httpx`, `openai`, `pydantic`, `pydantic-settings`, `python-dotenv`, `cryptography`
Optional (for image rendering): `playwright`, `markdown`, `Pillow`, `pygments`
