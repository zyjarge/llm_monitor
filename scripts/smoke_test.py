"""
Smoke test: verify provider contract, metric building, and history storage
without needing real cookies or a running Pushgateway.

Run: uv run python scripts/smoke_test.py
"""
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scraper.src.metrics import build_metrics
from scraper.src.models import ProviderResult, WindowQuota
from scraper.src.storage import HistoryStore


def print_metrics(registry) -> None:
    """Walk Prometheus registry and print exposition format."""
    from prometheus_client import generate_latest
    text = generate_latest(registry).decode()
    print("\n─── Prometheus exposition ───")
    print(text)


async def main() -> None:
    print("─── 1. ProviderResult contract ───")

    five_h = WindowQuota(window="5h", used=12.5, limit=100, reset_in_seconds=4200)
    weekly = WindowQuota(window="weekly", used=45.0, limit=200, reset_in_seconds=86400)
    monthly = WindowQuota(window="monthly", used=195.0, limit=870, reset_in_seconds=None)

    result = ProviderResult(
        provider="minimaxi",
        fetched_at=datetime.now(timezone.utc),
        windows=(five_h, weekly, monthly),
        success=True,
    )

    print(f"  minimaxi 5h      percent = {result.get('5h').percent}%")
    print(f"  minimaxi weekly  percent = {result.get('weekly').percent}%")
    print(f"  minimaxi monthly percent = {result.get('monthly').percent}%")

    # failure case
    fail = ProviderResult(
        provider="opencode_go",
        fetched_at=datetime.now(timezone.utc),
        windows=(),
        success=False,
        error="cookie expired",
    )
    print(f"  opencode_go ok={fail.success} err={fail.error}")

    print("\n─── 2. Build metrics ───")
    registry = build_metrics([result, fail])
    print_metrics(registry)

    print("\n─── 3. History store round-trip ───")
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tf:
        tmp_path = tf.name
    try:
        store = HistoryStore(path=Path(tmp_path))
        await store.init()
        await store.record(result)
        rows = await store.recent("minimaxi", "5h", limit=5)
        print(f"  recorded 3 rows, retrieved {len(rows)} (window=5h)")
        for row in rows:
            print(f"    ts={row['ts']} percent={row['percent']}")

        # Prune (nothing old to delete)
        deleted = await store.prune()
        print(f"  pruned {deleted} old rows")
    finally:
        os.unlink(tmp_path)

    print("\n✅ Smoke test passed")


if __name__ == "__main__":
    asyncio.run(main())
