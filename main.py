"""main.py — 变更点：lifespan 中关闭 renderer"""

import logging
import uvicorn
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import get_settings
from tencent_bot import router as bot_router, mimo, renderer  # ← 新增 renderer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("=" * 50)
    logger.info("MiMo-Tencent Bot 启动中...")
    logger.info(f"  MiMo 模型: {settings.mimo_model}")
    logger.info(f"  MiMo API:  {settings.mimo_api_base}")
    logger.info(f"  监听地址:  {settings.host}:{settings.port}")
    logger.info("=" * 50)

    yield

    await mimo.close()
    await renderer.close()  # ← 关闭 Playwright
    logger.info("MiMo-Tencent Bot 已关闭")


app = FastAPI(
    title="MiMo Tencent Bot",
    description="小米 MiMo-V2-Pro 驱动的腾讯机器人（富媒体版）",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(bot_router, prefix="/api/v1", tags=["bot"])


@app.get("/")
async def root():
    return {
        "service": "MiMo Tencent Bot",
        "version": "2.0.0",
        "model": "MiMo-V2-Pro",
        "features": ["text", "markdown", "image", "card", "reply"],
        "status": "running",
    }


@app.get("/health")
async def health():
    return {"status": "healthy"}


if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
        log_level="info",
    )
