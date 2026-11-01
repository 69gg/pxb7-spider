"""Probe 3: call the private API from inside a real page with raw XHR.

Run: python probes/03_api_probe.py [game_id] [product_id]

This is the mechanism the crawler uses.  Note we deliberately use
``XMLHttpRequest`` and not ``fetch``: the Nuxt app wraps ``window.fetch`` with
a signing interceptor and the wrapped version never settles for our calls
(see 04_fetch_vs_xhr.py).
"""

from __future__ import annotations

import asyncio
import json
import sys

from _probe_common import XHR_JS, dump, open_page, shutdown

API = "https://api-pc.pxb7.com"


async def main(game_id: str = "10161", product_id: str = "") -> None:
    pw, br, _ctx, page = await open_page(f"https://www.pxb7.com/buy/{game_id}/1")
    try:
        async def call(name: str, path: str, body: dict | None):
            res = await page.evaluate(XHR_JS, {"url": API + path, "body": body})
            text = res.get("text") or ""
            print(f"\n## {name}: status={res.get('status')} len={len(text)}")
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                print(text[:300])
                return None
            dump(f"/tmp/pxb7_probe3_{name}.json", data)
            return data

        await call("classes", "/api/product/web/gameClass/selectAll", {})
        await call("games", "/api/bff/web/game/select/all",
                   {"firstLetter": "", "gameClassId": "", "bizProd": 1})
        listing = await call(
            "list",
            "/api/search/product/v2/selectSearchPageList",
            {"query": "", "gameId": game_id, "pageIndex": 1, "pageSize": 100,
             "bizProd": 1, "type": "4", "posType": 1,
             "filterDTOList": [], "combineFilterList": []},
        )
        if product_id:
            await call("detail", "/api/product/web/product/detailPost", {"productId": product_id})
        elif listing:
            first = listing["data"]["list"][0]["productId"]
            print(f"\n(first listing product: {first})")
            await call("detail", "/api/product/web/product/detailPost", {"productId": first})
    finally:
        await shutdown(pw, br)


if __name__ == "__main__":
    asyncio.run(main(
        sys.argv[1] if len(sys.argv) > 1 else "10161",
        sys.argv[2] if len(sys.argv) > 2 else "",
    ))
