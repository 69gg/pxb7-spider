"""Probe 2: which XHR/fetch calls does a listing page make?

Run: python probes/02_page_probe.py [game_id]
"""

from __future__ import annotations

import asyncio
import sys

from _probe_common import dump, open_page, shutdown


async def main(game_id: str = "10161") -> None:
    url = f"https://www.pxb7.com/buy/{game_id}/1"
    pw, br, _ctx, page = await open_page(url)
    calls: list[tuple[str, str, str]] = []
    page.on(
        "request",
        lambda r: calls.append((r.method, r.url, r.post_data or ""))
        if r.resource_type in ("xhr", "fetch")
        else None,
    )
    try:
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_timeout(8000)
        print(f"title: {await page.title()}")
        print(f"html length: {len(await page.content())}")
        for method, call_url, body in calls:
            print(f"  {method} {call_url[:180]}  body={body[:120]}")
        dump("/tmp/pxb7_probe2_calls.json", calls)
    finally:
        await shutdown(pw, br)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "10161"))
