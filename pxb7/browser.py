"""Playwright-backed transport for pxb7's web API.

Why a browser at all?
---------------------
``api-pc.pxb7.com`` sits behind an Aliyun WAF that rejects plain HTTP clients
(requests/curl/httpx) with a slider-challenge HTML page.  A real Chromium
session passes the challenge once and keeps the resulting cookies, so we let
the browser own the network stack.

Why raw XMLHttpRequest instead of ``fetch``?
--------------------------------------------
The Nuxt app installs a request interceptor that wraps ``window.fetch`` (and
adds a ``timestamp__<id>`` signature).  The wrapped fetch never settles for
requests it did not originate, so calling it from ``page.evaluate`` hangs.
``XMLHttpRequest`` is left untouched by the app, and the WAF is happy with it
because the request originates from a real browser page on the right origin.

This means no JS reverse-engineering: the browser is used purely as a
transport, and responses are plain JSON.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)

# Any page on the target origin works as a bootstrap page; this one is the
# smallest (SSR shell only) and is not rate limited.
BOOTSTRAP_URL = "https://www.pxb7.com/aboutUs/gsjs?text=1"

WAF_MARKERS = ("aliyunCaptcha", "aliyun_waf", "_waf_", "滑动验证")

XHR_JS = """
async ({url, body, timeoutMs}) => {
  return await new Promise((resolve) => {
    const x = new XMLHttpRequest();
    x.open("POST", url, true);
    x.setRequestHeader("Content-Type", "application/json");
    x.timeout = timeoutMs;
    x.onload = () => resolve({
      status: x.status,
      text: x.responseText || "",
      contentType: x.getResponseHeader("content-type") || "",
    });
    x.onerror = () => resolve({status: x.status, text: "", error: "network"});
    x.ontimeout = () => resolve({status: x.status, text: "", error: "timeout"});
    x.send(body === null || body === undefined ? undefined : JSON.stringify(body));
  });
}
"""


class WafBlocked(RuntimeError):
    """Raised when the WAF challenge could not be cleared."""


class ApiError(RuntimeError):
    """Raised when the API answers with success=false or an unexpected body."""


@dataclass
class XhrResult:
    status: int
    text: str
    content_type: str = ""
    error: str | None = None

    @property
    def is_json(self) -> bool:
        head = self.text.lstrip()[:1]
        return head in ("{", "[")

    @property
    def looks_like_waf(self) -> bool:
        if self.is_json:
            return False
        head = self.text[:4000]
        return any(m in head for m in WAF_MARKERS)


class BrowserSession:
    """A small pool of Chromium tabs, each able to POST to the API."""

    def __init__(
        self,
        *,
        headless: bool = True,
        concurrency: int = 4,
        channel: str = "chrome",
        user_agent: str = DEFAULT_UA,
        timeout_ms: int = 25_000,
        retries: int = 4,
    ) -> None:
        self.headless = headless
        self.concurrency = max(1, concurrency)
        self.channel = channel or None
        self.user_agent = user_agent
        self.timeout_ms = timeout_ms
        self.retries = max(1, retries)

        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._pages: list[Page] = []
        self._cursor = 0
        self._refresh_lock = asyncio.Lock()
        self._last_refresh = 0.0

    # ------------------------------------------------------------------ setup
    async def start(self) -> None:
        self._pw = await async_playwright().start()
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ]
        try:
            self._browser = await self._pw.chromium.launch(
                channel=self.channel, headless=self.headless, args=launch_args
            )
        except Exception:
            if not self.channel:
                raise
            log.warning("browser channel %r unavailable, falling back to bundled chromium", self.channel)
            self._browser = await self._pw.chromium.launch(headless=self.headless, args=launch_args)

        self._context = await self._browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            user_agent=self.user_agent,
        )
        # Images/fonts/media are irrelevant for API calls and slow the tabs down.
        await self._context.route("**/*", self._block_heavy)

        for i in range(self.concurrency):
            page = await self._context.new_page()
            await self._prepare(page, label=f"tab-{i}")
            self._pages.append(page)
        log.info("browser ready (%d tab(s), headless=%s)", len(self._pages), self.headless)

    @staticmethod
    async def _block_heavy(route, request) -> None:  # type: ignore[no-untyped-def]
        if request.resource_type in ("image", "media", "font"):
            await route.abort()
        else:
            await route.continue_()

    async def _prepare(self, page: Page, *, label: str) -> None:
        for attempt in range(1, 4):
            try:
                await page.goto(BOOTSTRAP_URL, wait_until="domcontentloaded", timeout=45_000)
            except Exception as exc:  # noqa: BLE001 - network hiccup, retry
                log.warning("%s bootstrap failed (%s), retry %d", label, exc, attempt)
                await asyncio.sleep(2 * attempt)
                continue
            if not await self._is_challenged(page):
                return
            log.warning("%s hit the WAF slider challenge, retry %d", label, attempt)
            await asyncio.sleep(3 * attempt)
            try:
                await page.reload(wait_until="domcontentloaded", timeout=45_000)
            except Exception:  # noqa: BLE001
                pass
        raise WafBlocked(
            "pxb7 WAF keeps returning the slider challenge. Open the site in a headed "
            "browser once (--headed) to clear it, then retry."
        )

    @staticmethod
    async def _is_challenged(page: Page) -> bool:
        try:
            if "滑动验证" in (await page.title()):
                return True
            html = await page.content()
        except Exception:  # noqa: BLE001 - navigation races
            return True
        return any(m in html[:20000] for m in WAF_MARKERS)

    # ------------------------------------------------------------------- calls
    def _next_page(self) -> Page:
        page = self._pages[self._cursor % len(self._pages)]
        self._cursor += 1
        return page

    async def _refresh(self, page: Page) -> None:
        """Reload a tab to pick up fresh WAF cookies, globally rate limited."""
        async with self._refresh_lock:
            now = time.monotonic()
            wait = 3.0 - (now - self._last_refresh)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                await page.goto(BOOTSTRAP_URL, wait_until="domcontentloaded", timeout=45_000)
            except Exception as exc:  # noqa: BLE001
                log.warning("tab refresh failed: %s", exc)
            self._last_refresh = time.monotonic()
            if await self._is_challenged(page):
                raise WafBlocked("WAF challenge is back; stop and clear it manually (--headed).")

    async def post(self, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """POST JSON to the API from a real page and return the parsed body."""
        last_error: str = "unknown error"
        for attempt in range(1, self.retries + 1):
            page = self._next_page()
            try:
                result = await page.evaluate(
                    XHR_JS, {"url": url, "body": payload, "timeoutMs": self.timeout_ms}
                )
            except Exception as exc:  # noqa: BLE001 - tab crashed / navigated away
                last_error = f"evaluate failed: {exc}"
                log.warning("tab call failed (%s), reloading tab", exc)
                try:
                    await page.goto(BOOTSTRAP_URL, wait_until="domcontentloaded", timeout=45_000)
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(1.0 + attempt)
                continue
            res = XhrResult(
                status=int(result.get("status") or 0),
                text=result.get("text") or "",
                content_type=result.get("contentType") or "",
                error=result.get("error"),
            )

            if res.error:
                last_error = f"transport {res.error}"
            elif res.is_json:
                try:
                    data = json.loads(res.text)
                except json.JSONDecodeError:
                    last_error = "invalid JSON"
                else:
                    if data.get("success") is False:
                        raise ApiError(
                            f"{url} -> {data.get('errCode')} {data.get('errMessage')}"
                        )
                    return data
            elif res.looks_like_waf:
                log.warning("WAF challenge on API call, refreshing cookies (attempt %d)", attempt)
                await self._refresh(page)
                last_error = "waf challenge"
            else:
                last_error = f"HTTP {res.status} {res.content_type} {res.text[:120]!r}"

            backoff = min(30.0, 1.5 * (2 ** (attempt - 1))) + random.uniform(0, 0.5)
            log.warning("API call failed (%s), retrying in %.1fs", last_error, backoff)
            await asyncio.sleep(backoff)
        raise ApiError(f"{url} failed after {self.retries} attempts: {last_error}")

    # ----------------------------------------------------------------- teardown
    async def close(self) -> None:
        for page in self._pages:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass
        self._pages.clear()
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:  # noqa: BLE001
                pass
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # noqa: BLE001
                pass
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:  # noqa: BLE001
                pass

    async def __aenter__(self) -> BrowserSession:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
