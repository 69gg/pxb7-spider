"""Probe 4: why the crawler uses XHR instead of fetch.

``window.fetch`` is replaced by the site's request interceptor; calling it from
``page.evaluate`` hangs forever (or until our AbortController fires), while a
raw XMLHttpRequest returns JSON immediately.
"""

from __future__ import annotations

import asyncio
import json

from _probe_common import XHR_JS, open_page, shutdown

API = "https://api-pc.pxb7.com/api/search/product/v2/selectSearchPageList"
BODY = {
    "query": "", "gameId": "10161", "pageIndex": 1, "pageSize": 5,
    "bizProd": 1, "type": "4", "posType": 1,
    "filterDTOList": [], "combineFilterList": [],
}

FETCH_JS = """
async ({url, body, timeoutMs}) => {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body), credentials: "include", signal: ctl.signal});
    return {status: r.status, text: (await r.text()).slice(0, 200)};
  } catch (e) {
    return {error: String(e)};
  } finally {
    clearTimeout(timer);
  }
}
"""


async def main() -> None:
    pw, br, _ctx, page = await open_page("https://www.pxb7.com/buy/10161/1")
    try:
        print("fetch override:", await page.evaluate("String(window.fetch).slice(0, 120)"))
        try:
            res = await asyncio.wait_for(
                page.evaluate(FETCH_JS, {"url": API, "body": BODY, "timeoutMs": 8000}),
                timeout=20,
            )
            print("fetch result:", json.dumps(res, ensure_ascii=False)[:300])
        except asyncio.TimeoutError:
            print("fetch: page.evaluate never returned within 20s (interceptor hang)")

        res = await page.evaluate(XHR_JS, {"url": API, "body": BODY, "timeoutMs": 15000})
        print("xhr status:", res.get("status"), "| first 120 chars:", (res.get("text") or "")[:120])
    finally:
        await shutdown(pw, br)


if __name__ == "__main__":
    asyncio.run(main())
