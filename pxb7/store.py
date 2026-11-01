"""Append-only JSONL output plus a SQLite checkpoint store.

The JSONL file is the deliverable: one product per line, flushed immediately so
an interrupted run never loses more than the record being written.  SQLite is
only used for resume bookkeeping (which games/pages/products are finished), so
it can be deleted without touching the output.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    game_id       TEXT PRIMARY KEY,
    game_name     TEXT,
    state         TEXT NOT NULL DEFAULT 'pending',
    next_page     INTEGER NOT NULL DEFAULT 1,
    pages_done    INTEGER NOT NULL DEFAULT 0,
    products_done INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS products (
    product_id  TEXT PRIMARY KEY,
    game_id     TEXT,
    url         TEXT,
    state       TEXT NOT NULL DEFAULT 'pending',
    price       INTEGER,
    title       TEXT,
    error       TEXT,
    created_at  TEXT,
    updated_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_products_state ON products (state, game_id);
CREATE INDEX IF NOT EXISTS idx_products_game  ON products (game_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class JsonlWriter:
    """Append one JSON object per line and flush after every record."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")
        self.count = 0

    def write(self, record: dict[str, Any]) -> None:
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()
        self.count += 1

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.flush()
            self._fh.close()

    def __enter__(self) -> JsonlWriter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class StateStore:
    """Checkpoint database: per-game pagination cursor and per-product state."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------------ games
    def register_game(self, game_id: str, game_name: str) -> None:
        self.conn.execute(
            "INSERT INTO games (game_id, game_name, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(game_id) DO UPDATE SET game_name=excluded.game_name",
            (str(game_id), game_name, now_iso()),
        )
        self.conn.commit()

    def get_game(self, game_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM games WHERE game_id = ?", (str(game_id),)).fetchone()
        return dict(row) if row else None

    def set_game_progress(self, game_id: str, next_page: int, pages_done: int) -> None:
        self.conn.execute(
            "UPDATE games SET next_page = ?, pages_done = ?, updated_at = ? WHERE game_id = ?",
            (next_page, pages_done, now_iso(), str(game_id)),
        )
        self.conn.commit()

    def finish_game(self, game_id: str, state: str = "done") -> None:
        self.conn.execute(
            "UPDATE games SET state = ?, updated_at = ? WHERE game_id = ?",
            (state, now_iso(), str(game_id)),
        )
        self.conn.commit()

    # --------------------------------------------------------------- products
    def add_pending(
        self,
        product_id: str,
        game_id: str,
        url: str,
        *,
        price: int | None = None,
        title: str | None = None,
        retry_failed: bool = False,
    ) -> bool:
        """Record a listing hit. Returns True when the product still needs work.

        A product that is already ``done`` is skipped.  A product left
        ``pending`` by an interrupted run is re-queued, which is what makes
        resume work without re-reading the whole listing.
        """
        row = self.conn.execute(
            "SELECT state FROM products WHERE product_id = ?", (str(product_id),)
        ).fetchone()
        if row is not None:
            state = row["state"]
            if state == "done":
                return False
            if state == "failed" and not retry_failed:
                return False
            return True

        ts = now_iso()
        self.conn.execute(
            "INSERT INTO products (product_id, game_id, url, state, price, title, created_at, updated_at) "
            "VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)",
            (str(product_id), str(game_id), url, price, title, ts, ts),
        )
        self.conn.commit()
        return True

    def mark_done(self, product_id: str, *, price: int | None = None, title: str | None = None) -> None:
        self.conn.execute(
            "UPDATE products SET state = 'done', price = COALESCE(?, price), "
            "title = COALESCE(?, title), error = NULL, updated_at = ? WHERE product_id = ?",
            (price, title, now_iso(), str(product_id)),
        )
        self.conn.commit()

    def mark_failed(self, product_id: str, error: str) -> None:
        self.conn.execute(
            "UPDATE products SET state = 'failed', error = ?, updated_at = ? WHERE product_id = ?",
            (error[:500], now_iso(), str(product_id)),
        )
        self.conn.commit()

    def is_done(self, product_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM products WHERE product_id = ? AND state = 'done'", (str(product_id),)
        ).fetchone()
        return row is not None

    def pending_products(self, game_id: str | None = None, limit: int = 0) -> list[dict[str, Any]]:
        sql = "SELECT * FROM products WHERE state = 'pending'"
        args: list[Any] = []
        if game_id is not None:
            sql += " AND game_id = ?"
            args.append(str(game_id))
        sql += " ORDER BY rowid"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def failed_products(self, game_id: str | None = None, limit: int = 0) -> list[dict[str, Any]]:
        sql = "SELECT * FROM products WHERE state = 'failed'"
        args: list[Any] = []
        if game_id is not None:
            sql += " AND game_id = ?"
            args.append(str(game_id))
        sql += " ORDER BY rowid"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    # ------------------------------------------------------------------- meta
    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    # ------------------------------------------------------------------ stats
    def stats(self) -> dict[str, int]:
        def scalar(sql: str) -> int:
            return int(self.conn.execute(sql).fetchone()[0])

        return {
            "games_total": scalar("SELECT COUNT(*) FROM games"),
            "games_done": scalar("SELECT COUNT(*) FROM games WHERE state = 'done'"),
            "products_total": scalar("SELECT COUNT(*) FROM products"),
            "products_done": scalar("SELECT COUNT(*) FROM products WHERE state = 'done'"),
            "products_pending": scalar("SELECT COUNT(*) FROM products WHERE state = 'pending'"),
            "products_failed": scalar("SELECT COUNT(*) FROM products WHERE state = 'failed'"),
        }

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def iter_jsonl(path: str | Path) -> Iterable[dict[str, Any]]:
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)
