"""Shared helpers for the exploration scripts in this folder.

These scripts are development artifacts kept for reference; the crawler itself
lives in ``pxb7/``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.async_api import async_playwright

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)

XHR_JS = """
async ({url, body, timeoutMs}) => {
  return await new Promise((resolve) => {
    const x = new XMLHttpRequest();
    x.open("POST", url, true);
    x.setRequestHeader("Content-Type", "application/json");
    x.timeout = timeoutMs || 20000;
    x.onload = () => resolve({status: x.status, text: x.responseText || ""});
    x.onerror = () => resolve({status: x.status, error: "network"});
    x.ontimeout = () => resolve({status: x.status, error: "timeout"});
    x.send(body === undefined ? undefined : JSON.stringify(body));
  });
}
"""


async def open_page(url: str, *, headless: bool = True, wait_ms: int = 6000):
    """Return (playwright, browser, context, page) with a page already loaded."""
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        channel="chrome",
        headless=headless,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )
    ctx = await browser.new_context(
        viewport={"width": 1440, "height": 900}, locale="zh-CN", user_agent=UA
    )
    page = await ctx.new_page()
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(wait_ms)
    return pw, browser, ctx, page


async def shutdown(pw, browser) -> None:
    await browser.close()
    await pw.stop()


def dump(path: str, data) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    print(f"saved {path}")
