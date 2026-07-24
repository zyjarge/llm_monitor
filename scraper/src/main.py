"""
Scraper entry point: runs an async loop that scrapes all enabled providers,
stores history, pushes metrics, fires alerts.
"""
from __future__ import annotations

import asyncio
import signal
import traceback
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from scraper.src.alerts import get_alerter
from scraper.src.config import settings
from scraper.src.metrics import push_metrics
from scraper.src.models import ProviderResult
from scraper.src.providers import kimi_code, minimaxi, opencode_go
from scraper.src.storage import get_store


# ─── Logging setup ─────────────────────────────────────────────────
logger.remove()
logger.add(
    settings.LOG_DIR / "scraper.log",
    rotation="10 MB",
    retention="7 days",
    level="INFO",
)
logger.add(lambda msg: print(msg, end=""), level="INFO")


# ─── Provider registry (drives main loop) ──────────────────────────
# kimi_code re-enabled: uses Kimi Code Platform API
# (api.kimi.com/coding/v1/usages, Bearer auth). See providers/kimi_code.py.
PROVIDERS = [
    ("minimaxi",    minimaxi.is_configured,    minimaxi.fetch),
    ("opencode_go", opencode_go.is_configured, opencode_go.fetch),
    ("kimi_code",   kimi_code.is_configured,   kimi_code.fetch),
]


# ─── Main scrape tick ──────────────────────────────────────────────
async def tick(scheduler: AsyncIOScheduler) -> list[ProviderResult]:
    """One scrape pass across all providers."""
    logger.info("─" * 60)
    logger.info("scrape tick starting")

    tasks = []
    for name, configured, fetch_fn in PROVIDERS:
        if not configured():
            logger.warning(f"{name}: not configured (missing env), skipping")
            tasks.append(_skip(name))
            continue
        tasks.append(_run_one(name, fetch_fn))

    results: list[ProviderResult] = await asyncio.gather(*tasks, return_exceptions=False)

    # ── Push to Prometheus ───────────────────────────────────────
    try:
        push_metrics(results)
    except Exception as e:
        logger.error(f"metrics push failed: {e}")

    # ── Persist history (best-effort) ────────────────────────────
    try:
        store = await get_store()
        for r in results:
            if r.success:
                await store.record(r)
    except Exception as e:
        logger.error(f"history persist failed: {e}")

    # ── Telegram alerts (thresholds + cookie expiry) ─────────────
    # Quota threshold alerts are now handled by Grafana (alert rules),
    # so we skip the scraper's daily threshold check to avoid duplicate
    # notifications every scrape cycle.
    # Cookie failure alerting is still active (via record_success/failure).
    # try:
    #     alerter = get_alerter()
    #     await alerter.check_quota_thresholds(results)
    # except Exception as e:
    #     logger.error(f"alert check failed: {e}")

    # ── Heartbeat file (for docker healthcheck) ──────────────────
    settings.HEARTBEAT_FILE.touch()

    ok = sum(1 for r in results if r.success)
    logger.info(f"scrape tick done: {ok}/{len(results)} providers ok")
    return results


async def _run_one(name: str, fetch_fn) -> ProviderResult:
    alerter = get_alerter()
    try:
        result = await fetch_fn()
        if result.success:
            alerter.record_success(name)
        else:
            await alerter.record_failure(name, result.error or "unknown error")
        return result
    except Exception as e:
        tb = traceback.format_exc(limit=3)
        logger.error(f"{name}: exception: {e}\n{tb}")
        await alerter.record_failure(name, str(e))
        return ProviderResult(
            provider=name,
            fetched_at=datetime.now(timezone.utc),
            windows=(),
            success=False,
            error=str(e),
        )


async def _skip(name: str) -> ProviderResult:
    return ProviderResult(
        provider=name,
        fetched_at=datetime.now(timezone.utc),
        windows=(),
        success=False,
        error="not configured",
    )


# ─── Daily maintenance ─────────────────────────────────────────────
async def daily_prune() -> None:
    store = await get_store()
    deleted = await store.prune()
    logger.info(f"pruned {deleted} old history rows")


# ─── Lifecycle ─────────────────────────────────────────────────────
async def main() -> None:
    logger.info("llm-monitor starting up")

    # initialize sqlite once
    await get_store()

    scheduler = AsyncIOScheduler()

    # Run first tick immediately, then every SCRAPE_INTERVAL seconds.
    # Pass the coroutine directly (don't wrap in lambda) so APScheduler
    # dispatches it on its own event loop — avoids the
    # "no running event loop" RuntimeError from a worker thread.
    scheduler.add_job(
        tick,
        "interval",
        seconds=settings.SCRAPE_INTERVAL,
        id="scrape",
        next_run_time=datetime.now(timezone.utc),
        max_instances=1,
        coalesce=True,
        kwargs={"scheduler": scheduler},
    )
    scheduler.add_job(
        daily_prune,
        "cron",
        hour=3,
        minute=0,
        id="prune",
    )

    scheduler.start()
    logger.info(f"scheduler started, interval={settings.SCRAPE_INTERVAL}s")

    # Graceful shutdown
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    try:
        await stop_event.wait()
    finally:
        scheduler.shutdown(wait=False)
        logger.info("llm-monitor stopped")


if __name__ == "__main__":
    asyncio.run(main())
