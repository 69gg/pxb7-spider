"""Thin wrappers around the JSON endpoints used by the pxb7 web client."""

from __future__ import annotations

from typing import Any

from .browser import BrowserSession

API_BASE = "https://api-pc.pxb7.com"

PRODUCT_URL = "https://www.pxb7.com/product/{product_id}/1"


class Pxb7API:
    def __init__(self, session: BrowserSession) -> None:
        self.session = session

    async def game_classes(self) -> list[dict[str, Any]]:
        """Top level categories (e.g. 手游 / 端游)."""
        data = await self.session.post(f"{API_BASE}/api/product/web/gameClass/selectAll", {})
        return data.get("data") or []

    async def games(self) -> list[dict[str, Any]]:
        """Every game listed on the site (~1600 entries)."""
        data = await self.session.post(
            f"{API_BASE}/api/bff/web/game/select/all",
            {"firstLetter": "", "gameClassId": "", "bizProd": 1},
        )
        return data.get("data") or []

    async def search_page(
        self,
        game_id: str,
        page_index: int,
        *,
        page_size: int = 100,
        biz_prod: int = 1,
        sort_type: str = "4",
    ) -> list[dict[str, Any]]:
        """One page of a game's product listing (newest first)."""
        payload = {
            "query": "",
            "gameId": str(game_id),
            "pageIndex": page_index,
            "pageSize": page_size,
            "bizProd": biz_prod,
            "type": sort_type,
            "posType": 1,
            "filterDTOList": [],
            "combineFilterList": [],
        }
        data = await self.session.post(
            f"{API_BASE}/api/search/product/v2/selectSearchPageList", payload
        )
        return (data.get("data") or {}).get("list") or []

    async def product_detail(self, product_id: str) -> dict[str, Any]:
        """Full detail payload; ``showKey`` is not required by the backend."""
        data = await self.session.post(
            f"{API_BASE}/api/product/web/product/detailPost", {"productId": str(product_id)}
        )
        detail = data.get("data")
        if not isinstance(detail, dict):
            raise ValueError(f"detail payload missing for product {product_id}")
        return detail
