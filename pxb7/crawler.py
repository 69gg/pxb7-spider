"""Crawl orchestration: recursive category -> game -> listing page -> product."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .api import PRODUCT_URL, Pxb7API
from .browser import WafBlocked
from .store import JsonlWriter, StateStore

log = logging.getLogger(__name__)


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def build_record(item: dict[str, Any], detail: dict[str, Any] | None) -> dict[str, Any]:
    """Flatten a product into one JSONL-friendly record."""
    d = detail or {}
    product_id = str(d.get("productId") or item.get("productId"))
    game_id = str(d.get("gameId") or item.get("gameId") or "")
    price = _as_int(d.get("price"))
    if price is None:
        price = _as_int(item.get("price"))

    attrs: dict[str, str] = {}
    for attr in d.get("attrs") or []:
        name = attr.get("attrName")
        values = [v.get("itemName") for v in (attr.get("attrVals") or []) if v.get("itemName")]
        if name and values:
            attrs[name] = "、".join(values)

    images = d.get("images") or []
    record: dict[str, Any] = {
        "product_id": product_id,
        "url": PRODUCT_URL.format(product_id=product_id),
        "game_id": game_id,
        "game_name": d.get("gameName") or item.get("gameName"),
        "biz_prod": d.get("bizProd") or item.get("bizProd"),
        "price": price,
        "price_yuan": round(price / 100, 2) if price is not None else None,
        "ori_price": _as_int(d.get("oriPrice")) or _as_int(item.get("oriPrice")),
        "deposit_amount": _as_int(d.get("depositAmount")),
        "title": d.get("showTitle") or item.get("showTitle"),
        "product_name": d.get("productName"),
        "product_unique_no": d.get("productUniqueNo") or item.get("productUniqueNo"),
        "status": d.get("status", item.get("status")),
        "trade_status": d.get("tradeStatus"),
        "attrs": attrs,
        "attr_list": item.get("attrNameList") or [],
        "important": d.get("mainImportantKeys") or item.get("important") or [],
        "main_image": d.get("mainImageUrl") or item.get("mainImageUrl"),
        "image_count": len(images),
        "images": images,
        "hot_count": d.get("hotCount", item.get("hotCount")),
        "collect_count": d.get("collectCount", item.get("collectCount")),
        "seller_remark": d.get("sellerRemark"),
        "max_guaranteed_amount": _as_int(d.get("maxGuaranteedAmount")),
        "create_time": d.get("createTime"),
        "shelve_up_time": d.get("shelveUpTime"),
        "crawled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    return record


@dataclass
class CrawlStats:
    pages: int = 0
    queued: int = 0
    written: int = 0
    skipped: int = 0
    failed: int = 0
    started: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        return max(0.001, time.monotonic() - self.started)

    def line(self) -> str:
        rate = self.written / self.elapsed
        return (
            f"pages={self.pages} queued={self.queued} written={self.written} "
            f"skipped={self.skipped} failed={self.failed} rate={rate:.1f}/s"
        )


class Crawler:
    def __init__(
        self,
        api: Pxb7API,
        store: StateStore,
        writer: JsonlWriter,
        *,
        concurrency: int = 4,
        delay: tuple[float, float] = (0.1, 0.3),
        page_size: int = 100,
        biz_prod: int = 1,
        max_pages: int = 0,
        limit: int = 0,
        retry_failed: bool = False,
        restart: bool = False,
        keep_raw_detail: bool = False,
    ) -> None:
        self.api = api
        self.store = store
        self.writer = writer
        self.concurrency = max(1, concurrency)
        self.delay = delay
        self.page_size = page_size
        self.biz_prod = biz_prod
        self.max_pages = max_pages
        self.limit = limit
        self.retry_failed = retry_failed
        self.restart = restart
        self.keep_raw_detail = keep_raw_detail

        self.stats = CrawlStats()
        self._stop = asyncio.Event()
        self._limit_hit = False
        self._waf_error: WafBlocked | None = None

    # ------------------------------------------------------------- primitives
    async def _pause(self) -> None:
        lo, hi = self.delay
        if hi > 0:
            await asyncio.sleep(random.uniform(lo, hi))

    # ----------------------------------------------------------------- driver
    async def run(self, games: list[dict[str, Any]]) -> CrawlStats:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=self.concurrency * 20)

        producer = asyncio.create_task(self._produce(games, queue), name="producer")
        workers = [
            asyncio.create_task(self._consume(queue), name=f"worker-{i}")
            for i in range(self.concurrency)
        ]

        try:
            await producer
        except asyncio.CancelledError:
            self._stop.set()
            raise
        finally:
            for _ in workers:
                await queue.put(None)
            await asyncio.gather(*workers, return_exceptions=True)

        if self._waf_error is not None:
            raise self._waf_error
        log.info("crawl finished: %s", self.stats.line())
        return self.stats

    # --------------------------------------------------------------- producer
    async def _produce(self, games: list[dict[str, Any]], queue: asyncio.Queue) -> None:
        if self.retry_failed:
            rows = self.store.failed_products()
            if rows:
                log.info("re-queueing %d previously failed product(s)", len(rows))
            for row in rows:
                await queue.put(
                    {
                        "item": {
                            "productId": row["product_id"],
                            "gameId": row["game_id"],
                            "gameName": "",
                        },
                        "game": {},
                    }
                )

        for index, game in enumerate(games, 1):
            if self._stop.is_set() or self._limit_hit:
                break
            game_id = str(game.get("gameId") or game.get("game_id"))
            game_name = game.get("gameName") or game.get("game_name") or ""
            self.store.register_game(game_id, game_name)

            row = self.store.get_game(game_id)
            if row and row["state"] == "done" and not self.restart:
                log.info("[%d/%d] %s (%s) already done, skip", index, len(games), game_name, game_id)
                continue

            # Overlap one page on resume: new products shift the listing, so
            # re-reading the last checkpointed page avoids gaps.
            start_page = 1
            if row and not self.restart and row["next_page"] > 1:
                start_page = max(1, int(row["next_page"]) - 1)

            pages_done = int(row["pages_done"]) if row and not self.restart else 0
            page = start_page
            log.info(
                "[%d/%d] %s (%s) from page %d", index, len(games), game_name, game_id, start_page
            )

            while True:
                if self._stop.is_set() or self._limit_hit:
                    self.store.finish_game(game_id, "interrupted")
                    return
                if self.max_pages and pages_done >= self.max_pages:
                    log.info("%s reached --max-pages=%d, stopping this game", game_id, self.max_pages)
                    break

                items = await self.api.search_page(
                    game_id, page, page_size=self.page_size, biz_prod=self.biz_prod
                )
                if not items:
                    self.store.finish_game(game_id, "done")
                    log.info("%s done at page %d", game_id, page)
                    break

                self.stats.pages += 1
                pages_done += 1
                queued_here = 0
                for item in items:
                    product_id = str(item.get("productId"))
                    url = PRODUCT_URL.format(product_id=product_id)
                    price = _as_int(item.get("price"))
                    title = item.get("showTitle")
                    if not self.store.add_pending(
                        product_id,
                        game_id,
                        url,
                        price=price,
                        title=title,
                        retry_failed=self.retry_failed,
                    ):
                        self.stats.skipped += 1
                        continue
                    await queue.put({"item": item, "game": game})
                    self.stats.queued += 1
                    queued_here += 1
                    if self.limit and self.stats.queued >= self.limit:
                        self._limit_hit = True
                        break

                self.store.set_game_progress(game_id, page + 1, pages_done)
                if queued_here:
                    log.info("%s page %d: +%d products", game_id, page, queued_here)
                if self._limit_hit:
                    self.store.finish_game(game_id, "interrupted")
                    return
                page += 1
                await self._pause()

    # --------------------------------------------------------------- consumer
    async def _consume(self, queue: asyncio.Queue) -> None:
        while True:
            entry = await queue.get()
            try:
                if entry is None:
                    return
                if self._stop.is_set():
                    continue
                item = entry["item"]
                product_id = str(item.get("productId"))
                if self.store.is_done(product_id):
                    self.stats.skipped += 1
                    continue
                await self._handle(item)
            except WafBlocked as exc:
                # Keep draining the queue so the producer can finish, but stop
                # doing useful work and surface the error at the end.
                if self._waf_error is None:
                    log.error("WAF blocked: %s", exc)
                    self._waf_error = exc
                self._stop.set()
            except Exception:
                log.exception("worker error")
            finally:
                queue.task_done()

    async def _handle(self, item: dict[str, Any]) -> None:
        product_id = str(item.get("productId"))
        try:
            detail = await self.api.product_detail(product_id)
        except WafBlocked:
            raise
        except Exception as exc:  # noqa: BLE001
            self.stats.failed += 1
            self.store.mark_failed(product_id, str(exc))
            log.warning("product %s failed: %s", product_id, exc)
            return

        record = build_record(item, detail)
        if self.keep_raw_detail:
            record["raw_detail"] = detail
        self.writer.write(record)
        self.store.mark_done(
            product_id,
            price=_as_int(record.get("price")),
            title=record.get("title"),
        )
        self.stats.written += 1
        if self.stats.written % 20 == 0:
            log.info("progress: %s", self.stats.line())
        await self._pause()

    def stop(self) -> None:
        self._stop.set()
