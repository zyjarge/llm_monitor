"""
minimaxi.com (中国区) quota provider — uses official API.

Endpoint: https://www.minimaxi.com/v1/api/openplatform/coding_plan/remains
Auth:     Bearer token (subscription key)

The API exposes 5-hour and weekly rolling windows natively. Monthly is NOT
exposed, so we compute it from weekly: monthly_used = weekly_used * 4.345
(months have ~4.345 weeks), monthly_limit = weekly_limit * 4.345,
percentage is identical by construction but kept as separate metric so
alerting can fire on either threshold independently.

Response shape (verified live 2026-07-23):
    {
      "base_resp": {"status_code": 0, "status_msg": "success"},
      "model_remains": [
        {
          "model_name": "general",
          "current_interval_total_count": N,
          "current_interval_usage_count": N,
          "current_interval_status": 1,        // 1=active window, 3=idle?
          "current_interval_remaining_percent": 88,
          "current_weekly_total_count": N,
          "current_weekly_usage_count": N,
          "current_weekly_status": 3,
          "current_weekly_remaining_percent": 100,
          "start_time": 1784822400000,           // ms epoch
          "end_time":   1784840400000,           // ms epoch
          "remains_time": 12607927,              // seconds until reset
          "weekly_start_time": 1784476800000,
          "weekly_end_time":   1785081600000,
          "weekly_remains_time": 253807927
        },
        ...
      ]
    }

We aggregate ALL models into a single set of windows, since MiniMax's Token
Plan uses a unified quota pool (per official docs).
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
from loguru import logger

from scraper.src.config import settings
from scraper.src.models import ProviderResult, WindowQuota


def is_configured() -> bool:
    return bool(settings.MINIMAX_API_KEY)


async def fetch() -> ProviderResult:
    now = datetime.now(timezone.utc)
    url = settings.MINIMAX_API_URL

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {settings.MINIMAX_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            payload = resp.json()

        if payload.get("base_resp", {}).get("status_code") not in (0, None):
            return ProviderResult(
                provider="minimaxi",
                fetched_at=now,
                windows=(),
                success=False,
                error=f"api status_code={payload.get('base_resp', {}).get('status_code')} msg={payload.get('base_resp', {}).get('status_msg')}",
            )

        models = payload.get("model_remains") or []

        if not models:
            return ProviderResult(
                provider="minimaxi",
                fetched_at=now,
                windows=(),
                success=False,
                error="no models in response",
            )

        # Aggregate across all models in the plan's unified quota pool.
        # The API returns both raw counts (interval_total - interval_usage)
        # and a convenience "remaining_percent". When `total_count` is 0
        # (minimaxi's API bug for some users / non-activated subscriptions)
        # we fall back to `remaining_percent` so the dashboard isn't stuck
        # at 0% — `used_percent = 100 - remaining_percent`.
        interval_used = interval_limit = 0
        interval_used_pct = interval_total_pct = 0
        weekly_used = weekly_limit = 0
        weekly_used_pct = weekly_total_pct = 0
        # Track reset times per-window. The minimaxi API fields are
        # oddly-named: `end_time`/`remains_time` describe the 5h window and
        # `weekly_end_time`/`weekly_remains_time` describe the weekly window.
        interval_reset_at: int | None = None  # unix seconds (5h window)
        weekly_reset_at: int | None = None    # unix seconds (weekly)

        for m in models:
            # Interval (5h) window
            iu = float(m.get("current_interval_usage_count", 0) or 0)
            it = float(m.get("current_interval_total_count", 0) or 0)
            irp = float(m.get("current_interval_remaining_percent", 0) or 0)
            interval_used += iu
            interval_limit += it
            if it > 0:
                interval_total_pct += (iu / it) * 100
            else:
                # No raw count — use remaining_percent as proxy
                interval_total_pct += (100 - irp)

            # Weekly window
            wu = float(m.get("current_weekly_usage_count", 0) or 0)
            wt = float(m.get("current_weekly_total_count", 0) or 0)
            wrp = float(m.get("current_weekly_remaining_percent", 0) or 0)
            weekly_used += wu
            weekly_limit += wt
            if wt > 0:
                weekly_total_pct += (wu / wt) * 100
            else:
                weekly_total_pct += (100 - wrp)

            # 5h window reset. The minimaxi API exposes `end_time` (ms epoch
            # of the current 5h window's end) and `remains_time` (a seconds
            # field whose semantics are inconsistent across calls — values of
            # ~17 million seconds have been observed, far larger than the
            # 5h window itself). Use `end_time` for reliability.
            end_ms = m.get("end_time")
            if end_ms:
                cand = int(end_ms) // 1000
                if interval_reset_at is None or cand < interval_reset_at:
                    interval_reset_at = cand

            # Weekly window reset — same pattern: use `weekly_end_time` (ms).
            weekly_end_ms = m.get("weekly_end_time")
            if weekly_end_ms:
                cand_w = int(weekly_end_ms) // 1000
                if weekly_reset_at is None or cand_w < weekly_reset_at:
                    weekly_reset_at = cand_w

        # Round percentages for cleaner gauges
        interval_total_pct = round(interval_total_pct, 2)
        weekly_total_pct = round(weekly_total_pct, 2)
        # Monthly = weekly * 4.345; same percent by construction
        monthly_total_pct = round(weekly_total_pct * 4.345, 2)
        # When using raw counts, percent = used/limit. When falling back to
        # remaining_percent, limit==0 but percent is meaningful — set limit
        # to 100 so the WindowQuota.percent property still computes correctly.
        if interval_limit == 0 and interval_total_pct > 0:
            interval_used = interval_total_pct
            interval_limit = 100
        if weekly_limit == 0 and weekly_total_pct > 0:
            weekly_used = weekly_total_pct
            weekly_limit = 100

        def secs_until(reset_at):
            if reset_at is None:
                return None
            return max(0, int(reset_at - now.timestamp()))

        interval_reset_sec = secs_until(interval_reset_at)
        weekly_reset_sec = secs_until(weekly_reset_at)
        # Monthly window is a derived metric (weekly * 4.345). The reset
        # time is a placeholder — the upstream API doesn't expose a real
        # monthly window, so we set it to None to signal "no real value"
        # in metrics. The dashboard's noValue='' config will display it
        # as blank rather than "No data".
        monthly_reset_sec = None

        windows = [
            WindowQuota(window="5h",      used=interval_used, limit=interval_limit, reset_in_seconds=interval_reset_sec),
            WindowQuota(window="weekly",  used=weekly_used,    limit=weekly_limit,    reset_in_seconds=weekly_reset_sec),
            WindowQuota(window="monthly", used=weekly_used,    limit=weekly_limit,    reset_in_seconds=monthly_reset_sec),
        ]

        return ProviderResult(
            provider="minimaxi",
            fetched_at=now,
            windows=tuple(windows),
            success=True,
        )

    except httpx.HTTPStatusError as e:
        return ProviderResult(
            provider="minimaxi",
            fetched_at=now,
            windows=(),
            success=False,
            error=f"http {e.response.status_code}",
        )
    except Exception as e:
        logger.exception("minimaxi fetch failed")
        return ProviderResult(
            provider="minimaxi",
            fetched_at=now,
            windows=(),
            success=False,
            error=str(e),
        )
