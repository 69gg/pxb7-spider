"""Probe 1: how does pxb7 react to plain HTTP clients vs a real browser?

Findings (2026-11-01):
  * ``curl`` against https://www.pxb7.com/ and api-pc.pxb7.com returns an
    Aliyun WAF slider challenge (HTML, not JSON).
  * A Playwright Chromium session loads product/list pages normally; the
    challenge only appears for some pages/rates.
  * Automating the slider with synthetic mouse events is rejected
    ("验证失败"), so we do not try to solve it. We rely on a real page and
    reload the tab when the challenge shows up.
"""

from __future__ import annotations

import asyncio
import urllib.request

from _probe_common import open_page, shutdown

URLS = [
    "https://www.pxb7.com/",
    "https://www.pxb7.com/buy/10161/1",
    "https://www.pxb7.com/product/1558020236084103445/1",
    "https://api-pc.pxb7.com/api/product/web/gameClass/selectAll",
]


def plain_http() -> None:
    for url in URLS:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8", "ignore")
                waf = any(m in body for m in ("aliyunCaptcha", "_waf_", "滑动验证"))
                print(f"[plain] {url} -> {resp.status} waf={waf} len={len(body)}")
        except Exception as exc:  # noqa: BLE001
            print(f"[plain] {url} -> error {exc}")


async def browser() -> None:
    pw, br, _ctx, page = await open_page("https://www.pxb7.com/buy/10161/1", headless=True)
    try:
        print(f"[browser] title={await page.title()!r}")
        html = await page.content()
        print(f"[browser] challenged={'aliyunCaptcha' in html} len={len(html)}")
        await page.screenshot(path="/tmp/pxb7_probe1.png")
        print("[browser] screenshot -> /tmp/pxb7_probe1.png")
    finally:
        await shutdown(pw, br)


if __name__ == "__main__":
    plain_http()
    asyncio.run(browser())
