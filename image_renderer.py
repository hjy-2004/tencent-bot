"""Markdown → 高质量 PNG 渲染器（同步 Playwright + 线程池，兼容 Windows）"""

import asyncio
import logging
import os

import markdown
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# 代码高亮 CSS（Dracula 主题）
PYGMENTS_CSS = ""
try:
    from pygments.formatters import HtmlFormatter
    PYGMENTS_CSS = HtmlFormatter(style="dracula").get_style_defs('.highlight')
except ImportError:
    pass

# 渲染用的 HTML 模板
HTML_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;700&family=JetBrains+Mono:wght@400&display=swap');

  * {{ margin: 0; padding: 0; box-sizing: border-box; }}

  body {{
    font-family: 'Noto Sans SC', -apple-system, sans-serif;
    background: linear-gradient(135deg, #0f0c29, #302b63, #24243e);
    color: #e8e6e3;
    padding: 40px 48px;
    width: {width}px;
    line-height: 1.75;
    -webkit-font-smoothing: antialiased;
  }}

  h1, h2, h3 {{
    font-weight: 700;
    margin: 1.2em 0 0.6em;
    background: linear-gradient(90deg, #a78bfa, #60a5fa);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
  }}
  h1 {{ font-size: 28px; }}
  h2 {{ font-size: 22px; }}
  h3 {{ font-size: 18px; }}

  p {{ margin: 0.6em 0; font-size: 15px; }}

  code {{
    font-family: 'JetBrains Mono', monospace;
    background: rgba(139, 92, 246, 0.15);
    padding: 2px 7px;
    border-radius: 4px;
    font-size: 13px;
    color: #c4b5fd;
  }}

  pre {{
    background: #1e1e2e;
    border: 1px solid #313244;
    border-radius: 10px;
    padding: 18px 22px;
    margin: 1em 0;
    overflow-x: auto;
  }}
  pre code {{
    background: none;
    padding: 0;
    font-size: 13px;
    color: #cdd6f4;
  }}

  blockquote {{
    border-left: 3px solid #a78bfa;
    padding: 8px 16px;
    margin: 1em 0;
    background: rgba(167, 139, 250, 0.06);
    border-radius: 0 8px 8px 0;
    color: #a6adc8;
  }}

  ul, ol {{ padding-left: 1.5em; margin: 0.5em 0; }}
  li {{ margin: 0.3em 0; font-size: 15px; }}

  table {{
    width: 100%;
    border-collapse: collapse;
    margin: 1em 0;
    font-size: 14px;
  }}
  th {{
    background: rgba(139, 92, 246, 0.2);
    padding: 10px 14px;
    text-align: left;
    font-weight: 700;
  }}
  td {{
    padding: 8px 14px;
    border-bottom: 1px solid #313244;
  }}

  a {{ color: #60a5fa; text-decoration: none; }}
  hr {{ border: none; border-top: 1px solid #45475a; margin: 1.5em 0; }}
  img {{ max-width: 100%; border-radius: 8px; }}

  {pygments_css}
</style>
</head><body>{body}</body></html>"""


# def _sync_render(html: str) -> bytes:
#     """同步渲染（在子线程中运行）"""
#     from playwright.sync_api import sync_playwright
#
#     with sync_playwright() as p:
#         browser = p.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
#         page = browser.new_page()
#         page.set_content(html, wait_until="networkidle")
#         page.wait_for_timeout(300)
#
#         height = page.evaluate("document.body.scrollHeight")
#         page.set_viewport_size({"width": 760, "height": height + 40})
#
#         png = page.screenshot(type="png", full_page=True)
#         browser.close()
#         return png

def _sync_render(html: str) -> bytes:
    """同步渲染（在子线程中运行）"""
    from playwright.sync_api import sync_playwright
    import os, shutil

    with sync_playwright() as p:
        chrome_path = shutil.which("chrome") or shutil.which("google-chrome")
        if not chrome_path:
            candidates = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                rf"C:\Users\{os.environ.get('USERNAME','')}\AppData\Local\Google\Chrome\Application\chrome.exe",
            ]
            for c in candidates:
                if os.path.exists(c):
                    chrome_path = c
                    break

        if chrome_path:
            browser = p.chromium.launch(
                executable_path=chrome_path,
                args=["--no-sandbox", "--disable-gpu", "--headless"],
            )
        else:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-gpu"])

        page = browser.new_page()

        # 限制视口宽度，减少渲染尺寸
        page.set_viewport_size({"width": 680, "height": 800})
        page.set_content(html, wait_until="networkidle")
        page.wait_for_timeout(500)

        # 截图为 PNG（后续在 _upload_file 中转 JPEG 压缩）
        png = page.screenshot(type="png", full_page=True)
        browser.close()
        return png




class ImageRenderer:
    """将 Markdown / HTML 渲染为 PNG 图片（线程池，兼容 Windows）"""

    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=2)
        self._md = markdown.Markdown(
            extensions=["fenced_code", "tables", "codehilite", "toc", "nl2br"],
            extension_configs={
                "codehilite": {"guess_lang": True, "css_class": "highlight"}
            },
        )

    async def markdown_to_image(
        self, md_text: str, width: int = 680
    ) -> Optional[bytes]:
        """Markdown 文本 → PNG bytes"""
        body = self._md.convert(md_text)
        self._md.reset()
        html = HTML_TEMPLATE.format(
            body=body, width=width, pygments_css=PYGMENTS_CSS
        )
        return await self.html_to_image(html)

    async def html_to_image(self, html: str) -> Optional[bytes]:
        """原始 HTML → PNG bytes"""
        try:
            loop = asyncio.get_event_loop()
            png = await loop.run_in_executor(self._executor, _sync_render, html)
            logger.info(f"渲染成功: {len(png)} bytes")
            return png
        except Exception as e:
            logger.error(f"图片渲染失败: {e}", exc_info=True)
            return None

    async def close(self):
        self._executor.shutdown(wait=False)
