"""
SQLite history store.

Writes a row per (provider, window, scrape) and prunes old data daily.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import aiosqlite

from scraper.src.config import settings
from scraper.src.models import ProviderResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS quota_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,  -- ISO 8601 UTC
    provider        TEXT    NOT NULL,
    window          TEXT    NOT NULL,
    used            REAL    NOT NULL,
    limit_value     REAL    NOT NULL,
    percent         REAL    NOT NULL,
    reset_in_sec    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_hist_provider_ts
    ON quota_history (provider, ts);
CREATE INDEX IF NOT EXISTS idx_hist_ts
    ON quota_history (ts);
"""


class HistoryStore:
    def __init__(self, path=None):
        self.path = path or settings.sqlite_path

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def record(self, result: ProviderResult) -> None:
        if not result.success:
            return
        ts = result.fetched_at.astimezone(timezone.utc).isoformat()
        async with aiosqlite.connect(self.path) as db:
            await db.executemany(
                """INSERT INTO quota_history
                   (ts, provider, window, used, limit_value, percent, reset_in_sec)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        ts,
                        result.provider,
                        w.window,
                        w.used,
                        w.limit,
                        w.percent,
                        w.reset_in_seconds,
                    )
                    for w in result.windows
                ],
            )
            await db.commit()

    async def prune(self) -> int:
        """Drop rows older than HISTORY_RETENTION_DAYS. Returns rows deleted."""
        cutoff = datetime.now(timezone.utc).timestamp() - (
            settings.HISTORY_RETENTION_DAYS * 86400
        )
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "DELETE FROM quota_history WHERE ts < ?", (cutoff_iso,)
            )
            await db.commit()
            return cur.rowcount

    async def recent(self, provider: str, window: str, limit: int = 100):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT ts, used, limit_value, percent, reset_in_sec
                   FROM quota_history
                   WHERE provider = ? AND window = ?
                   ORDER BY ts DESC LIMIT ?""",
                (provider, window, limit),
            )
            return await cur.fetchall()


_store: HistoryStore | None = None


async def get_store() -> HistoryStore:
    global _store
    if _store is None:
        _store = HistoryStore()
        await _store.init()
    return _store


def run_sync(coro):
    """Helper for synchronous contexts (e.g. APScheduler jobs)."""
    return asyncio.run(coro)
