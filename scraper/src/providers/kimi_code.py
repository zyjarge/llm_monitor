"""
Kimi Code quota provider — uses the official Kimi Code Platform API.

Auth:      Bearer API key (from https://www.kimi.com/code → API Keys).
Endpoint:  GET https://api.kimi.com/coding/v1/usages
Same quota data as the Kimi Code CLI `/usage` command — 5h rate window
plus weekly request quota.

Response shape (verified from public docs + multiple OSS consumers):

    {
      "usage": {
        "limit": "2048",         # weekly quota (requests)
        "used":  "214",
        "remaining": "1834",
        "resetTime": "2026-01-09T15:23:13.716839300Z"
      },
      "limits": [{
        "window": {
          "duration": 300,       # 5 hours = 300 minutes
          "timeUnit": "TIME_UNIT_MINUTE"
        },
        "detail": {
          "limit": "200",        # 5h rate limit (requests)
          "used": "139",
          "remaining": "61",
          "resetTime": "2026-01-06T13:33:02.717479433Z"
        }
      }],
      "user": {"membership": {"level": "LEVEL_INTERMEDIATE"}}
    }

Note: this is the Kimi Code Platform quota (api.kimi.com/coding), which
matches the CLI `/usage` output. The Web console `/code/console` page
shows Kimi Open Platform quota (api.moonshot.cn) — a separate system.
See: https://github.com/MoonshotAI/kimi-cli/issues/2150
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx
from loguru import logger

from scraper.src.config import settings
from scraper.src.models import ProviderResult, WindowQuota


# Sentinel error returned when the API key is missing/invalid so the
# caller can distinguish a config error from a real network failure.
class KimiConfigError(Exception):
    pass


def is_configured() -> bool:
    return bool(settings.KIMI_API_KEY)


def _reset_in_seconds(reset_time: str | None) -> int | None:
    """Convert ISO resetTime → seconds until reset (UTC)."""
    if not reset_time:
        return None
    try:
        # Trim nanoseconds beyond microseconds (Python datetime supports 6).
        ts = reset_time.rstrip("Z")
        if "." in ts:
            base, frac = ts.split(".", 1)
            # Truncate fractional to 6 digits
            ts = f"{base}.{frac[:6]}"
        reset_dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        return max(0, int((reset_dt - datetime.now(timezone.utc)).total_seconds()))
    except (ValueError, TypeError) as e:
        logger.debug(f"kimi_code: could not parse resetTime {reset_time!r}: {e}")
        return None


def _pct(used: int | float | None, limit: int | float | None) -> tuple[float, float, float] | None:
    """Compute (used, limit, used_pct) from raw API fields. Returns None if
    either field is missing/non-positive (avoid divide-by-zero on stale data)."""
    if used is None or limit is None:
        return None
    try:
        used_f = float(used)
        limit_f = float(limit)
    except (TypeError, ValueError):
        return None
    if limit_f <= 0:
        return None
    pct = (used_f / limit_f) * 100
    return (used_f, limit_f, round(pct, 2))


def _used_remaining(detail_or_usage: dict) -> tuple[float, float] | None:
    """The Kimi API only returns `limit` and `remaining` — not `used`.
    `used = limit - remaining`. Both are strings in the response.
    Returns (used, limit) or None if either is missing/non-numeric.
    """
    limit = detail_or_usage.get("limit")
    remaining = detail_or_usage.get("remaining")
    if limit is None or remaining is None:
        return None
    try:
        limit_f = float(limit)
        remaining_f = float(remaining)
    except (TypeError, ValueError):
        return None
    if limit_f <= 0:
        return None
    used_f = max(0.0, limit_f - remaining_f)
    return (used_f, limit_f)


async def fetch() -> ProviderResult:
    now = datetime.now(timezone.utc)
    if not settings.KIMI_API_KEY:
        return ProviderResult(
            provider="kimi_code",
            fetched_at=now,
            windows=(),
            success=False,
            error="KIMI_API_KEY not set in .env",
        )

    url = settings.KIMI_API_URL

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {settings.KIMI_API_KEY}",
                    "Accept": "application/json",
                },
            )

        if resp.status_code == 401 or resp.status_code == 403:
            return ProviderResult(
                provider="kimi_code",
                fetched_at=now,
                windows=(),
                success=False,
                error=f"auth {resp.status_code} — check KIMI_API_KEY",
            )

        resp.raise_for_status()
        payload = resp.json()

        # ── 5h window: limits[0].detail (typically the first entry) ─────
        # Filter to entries with TIME_UNIT_MINUTE + duration=300; fall back
        # to the first entry otherwise.
        five_h: tuple[float, float, float] | None = None
        five_h_reset = None
        for lim in payload.get("limits", []) or []:
            window = lim.get("window", {}) or {}
            if window.get("timeUnit") == "TIME_UNIT_MINUTE" and window.get("duration") == 300:
                ur = _used_remaining(lim.get("detail", {}) or {})
                if ur:
                    used_f, limit_f = ur
                    five_h = (used_f, limit_f, round(used_f / limit_f * 100, 2))
                five_h_reset = _reset_in_seconds(lim.get("detail", {}).get("resetTime"))
                break
        if five_h is None and payload.get("limits"):
            # fallback: first entry
            ur = _used_remaining(payload["limits"][0].get("detail", {}) or {})
            if ur:
                used_f, limit_f = ur
                five_h = (used_f, limit_f, round(used_f / limit_f * 100, 2))
            five_h_reset = _reset_in_seconds(payload["limits"][0].get("detail", {}).get("resetTime"))

        # ── Weekly: usage ────────────────────────────────────────────
        ur = _used_remaining(payload.get("usage") or {})
        weekly: tuple[float, float, float] | None = None
        if ur:
            used_f, limit_f = ur
            weekly = (used_f, limit_f, round(used_f / limit_f * 100, 2))
        weekly_reset = _reset_in_seconds((payload.get("usage") or {}).get("resetTime"))

        windows: list[WindowQuota] = []
        if five_h is not None:
            windows.append(WindowQuota(
                window="5h", used=five_h[0], limit=five_h[1], reset_in_seconds=five_h_reset
            ))
        if weekly is not None:
            windows.append(WindowQuota(
                window="weekly", used=weekly[0], limit=weekly[1], reset_in_seconds=weekly_reset
            ))
            # Monthly = weekly × 4.345 (≈ weeks per month); same percent by construction.
            # Monthly reset time is None — the upstream API doesn't expose
            # a real monthly window, so the dashboard shows blank (noValue='')
            # rather than a misleading synthetic value.
            windows.append(WindowQuota(
                window="monthly", used=weekly[0] * 4.345, limit=weekly[1] * 4.345,
                reset_in_seconds=None
            ))

        if not windows:
            return ProviderResult(
                provider="kimi_code",
                fetched_at=now,
                windows=(),
                success=False,
                error="no limits/usage data in response — API may have changed",
            )

        return ProviderResult(
            provider="kimi_code",
            fetched_at=now,
            windows=tuple(windows),
            success=True,
        )

    except httpx.HTTPStatusError as e:
        return ProviderResult(
            provider="kimi_code",
            fetched_at=now,
            windows=(),
            success=False,
            error=f"http {e.response.status_code}",
        )
    except Exception as e:
        logger.exception("kimi_code fetch failed")
        return ProviderResult(
            provider="kimi_code",
            fetched_at=now,
            windows=(),
            success=False,
            error=str(e),
        )