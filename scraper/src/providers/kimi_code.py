"""
Kimi Code quota provider — Kimi Code Platform API (+ optional membership API).

Two real upstream sources; no window is derived from another.

1. Kimi Code Platform API (API key — always available)
       GET https://api.kimi.com/coding/v1/usages
   Gives the rolling 5-hour window, the weekly request pool, and — for
   accounts on the Extra Usage (加速包) plan — the booster wallet.

   Response shape A (legacy counters) — the shape we currently receive:
       {
         "usage":  {"limit": "100", "remaining": "100", "resetTime": "..."},
         "limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                     "detail": {"limit": "100", "remaining": "100", ...}}]
       }

   Response shape B (ratio pools) — newer accounts:
       {
         "limits": [...],
         "usages": {
           "limit_5h":          {"used_ratio": 0,    "reset_time": "..."},
           "limit_7d":          {"used_ratio": 0,    "reset_time": "..."},
           "limit_month_total": {"used_ratio": 0.08, "reset_time": "..."}
         }
       }
   `limit_month_total` is the combined monthly pool. `limit_month_code` is
   only the Code slice *inside* it — it is never an allowance of its own and
   must not be reported as the monthly window.

   Both shapes may appear in the same response.

2. Kimi membership API (web session token — optional, gives the console's
   own monthly number)
       POST https://www.kimi.com/apiv2/kimi.gateway.membership.v2.MembershipService/GetSubscriptionStats
       Authorization: Bearer <JWT>
   This is the endpoint the Kimi console itself calls for its 月度余额
   card: `subscriptionBalance.amountUsedRatio` (plus `expireTime` for the
   reset date). It requires a live web token — the Code API key cannot
   authenticate here (the gateway rejects it as a malformed token).

The MONTHLY BALANCE (月度余额) is what the console shows, and it comes only
from the membership API. The Code API has no monthly-balance figure at all:
its `usage`/`limits` are request-count pools and `usages.limit_*` are ratios
of those same pools.

Monthly source precedence (logged on every fetch):
   membership  ->  limit_month_total  ->  (none — do not substitute)

The booster wallet (加速包) is a DIFFERENT quantity and gets its own window
("booster"), never the monthly one:

   booster_wallet.monthlyUsed        = spend so far this period (CNY)
   booster_wallet.monthlyChargeLimit = the monthly cap on that spend (CNY)
   booster_wallet.balance.amountLeft = money still unspent in the pack (CNY)

`monthlyUsed / monthlyChargeLimit` is how much of this month's 加速包 budget
has been burned — a spend ratio, NOT a monthly balance. The field names
("monthly*") refer to the pack's billing period, not to the 月度余额 quota.
Confusing the two would report a money figure as a quota percentage.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

import httpx
from loguru import logger

from scraper.src.config import settings
from scraper.src.models import ProviderResult, WindowQuota


# ─── Window unit table ──────────────────────────────────────────────
# `window.timeUnit` is a protobuf-style enum and has been spelled several
# ways across the API's history. Normalise to minutes instead of
# string-matching one enum+duration pair.
_UNIT_TO_MINUTES = {
    "TIME_UNIT_MINUTE": 1, "MINUTE": 1, "MINUTES": 1,
    "TIME_UNIT_HOUR": 60, "HOUR": 60, "HOURS": 60,
    "TIME_UNIT_DAY": 1440, "DAY": 1440, "DAYS": 1440,
    "TIME_UNIT_WEEK": 10080, "WEEK": 10080, "WEEKS": 10080,
}

FIVE_HOUR_MINUTES = 300


def is_configured() -> bool:
    return bool(settings.KIMI_API_KEY)


# ─── Small parsing helpers ──────────────────────────────────────────
def _num(value) -> float | None:
    """Coerce a JSON number-or-numeric-string to float. None on failure."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _parse_ts(value) -> datetime | None:
    """Parse an RFC3339 timestamp (up to 9 fractional digits) to UTC.

    Python's datetime only holds microseconds, so the fraction is truncated
    to 6 digits. The timezone offset in the string is honoured — we must NOT
    overwrite it with UTC, which would shift the instant by the offset.
    """
    if not isinstance(value, str) or not value:
        return None
    ts = value.strip()
    if ts.endswith(("Z", "z")):
        ts = ts[:-1] + "+00:00"
    # Truncate sub-microsecond precision: .716839300 → .716839
    if "." in ts:
        head, _, tail = ts.partition(".")
        digits = ""
        for ch in tail:
            if ch.isdigit():
                digits += ch
            else:
                break
        rest = tail[len(digits):]
        if digits:
            ts = f"{head}.{digits[:6]}{rest}"
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError as e:
        logger.debug(f"kimi_code: unparseable timestamp {value!r}: {e}")
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _reset_in_seconds(dt: datetime | None) -> int | None:
    """Seconds until `dt` (UTC). None when unknown or already past."""
    if dt is None:
        return None
    return max(0, int((dt - datetime.now(timezone.utc)).total_seconds()))


def _window_minutes(window) -> int | None:
    """Convert a `window` block to its length in minutes."""
    if not isinstance(window, dict):
        return None
    duration = _num(window.get("duration"))
    if duration is None or duration <= 0:
        return None
    unit = window.get("timeUnit") or window.get("time_unit") or ""
    factor = _UNIT_TO_MINUTES.get(str(unit).upper())
    if factor is None:
        return None
    return int(duration * factor)


def _usage_counts(block) -> tuple[float, float] | None:
    """Extract (used, limit) from a usage block.

    The API may send `used`, `remaining`, both, or neither alongside
    `limit`. Derive whichever is missing; return None when the limit is
    unusable or both counters are absent.
    """
    if not isinstance(block, dict):
        return None
    limit = _num(block.get("limit"))
    if limit is None or limit <= 0:
        return None
    used = _num(block.get("used"))
    remaining = _num(block.get("remaining"))
    if used is None and remaining is not None:
        used = limit - remaining
    if used is None:
        return None
    return (max(0.0, min(used, limit)), limit)


def _ratio_percent(block) -> float | None:
    """Extract a 0-100 percentage from a ratio-pool block.

    `used_ratio` is a fraction of the pool (0.0-1.0). A value outside that
    range is schema drift, not a quota — treat it as missing rather than
    clamping it into a plausible-looking number.
    """
    if not isinstance(block, dict):
        return None
    ratio = _num(block.get("used_ratio"))
    if ratio is None or ratio < 0.0 or ratio > 1.0:
        return None
    return round(ratio * 100, 2)


def _ratio_reset(block) -> datetime | None:
    if not isinstance(block, dict):
        return None
    for key in ("reset_time", "resetTime", "reset_at", "resetAt"):
        dt = _parse_ts(block.get(key))
        if dt is not None:
            return dt
    return None


def _block_reset(block) -> datetime | None:
    if not isinstance(block, dict):
        return None
    for key in ("resetTime", "reset_time", "resetAt", "reset_at"):
        dt = _parse_ts(block.get(key))
        if dt is not None:
            return dt
    return None


def _percent_window(name: str, pct: float, reset_dt: datetime | None) -> WindowQuota:
    """Build a window from a percentage (ratio pools carry no absolute counts).

    `used`/`limit` are expressed in percentage points (used=pct, limit=100)
    so `WindowQuota.percent` stays correct; the dashboard renders
    `llm_quota_percent` for this window.
    """
    return WindowQuota(
        window=name,
        used=pct,
        limit=100.0,
        reset_in_seconds=_reset_in_seconds(reset_dt),
    )


# ─── Membership API (optional, needs a live web session) ────────────
def _kimi_auth_token() -> str | None:
    """Get the Kimi web JWT, in order of precedence:

    1. `KIMI_AUTH_TOKEN` in .env — the token pasted directly
    2. `cookie/kimi_token` — the token as a plain-text file
    3. the `kimi-auth` cookie in the Netscape cookie export

    Modern Kimi web keeps this JWT in localStorage under `access_token`,
    so a Cookie-Editor export no longer contains it; only the older
    `kimi-auth` cookie is exported. Accept all three so the user can
    supply it however is easiest.
    """
    token = (settings.KIMI_AUTH_TOKEN or "").strip()
    if token:
        return token

    try:
        token_path = settings.cookie_path("KIMI_TOKEN_FILE")
        if token_path.exists():
            token = token_path.read_text(encoding="utf-8").strip()
            if token:
                return token
    except Exception as e:
        logger.debug(f"kimi_code: could not read token file: {e}")

    try:
        # Imported lazily: _base pulls in Playwright, which the Kimi
        # provider itself does not need.
        from scraper.src.providers._base import read_cookie_file
    except Exception as e:  # pragma: no cover - playwright missing
        logger.debug(f"kimi_code: cookie reader unavailable: {e}")
        return None
    try:
        cookies = read_cookie_file(settings.cookie_path("KIMI_COOKIE_FILE"))
    except Exception as e:
        logger.debug(f"kimi_code: could not read cookie file: {e}")
        return None
    return cookies.get("kimi-auth") or None


def _jwt_expired(token: str) -> bool | None:
    """True/False if the JWT's exp is readable, None if it can't be parsed."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = _num(claims.get("exp"))
        if exp is None:
            return None
        return exp <= datetime.now(timezone.utc).timestamp()
    except Exception:
        return None


async def _refresh_access_token() -> str | None:
    """Exchange the refresh token for a fresh access token.

    Kimi's access token is short-lived (~15 minutes), but the refresh token
    from the same login response is long-lived. Without this the monthly
    balance would silently disappear a quarter of an hour after login.

    Writes the new access token back to the token file so the next process
    starts fresh. Returns the new token, or None on failure.
    """
    refresh = ""
    try:
        refresh_path = settings.cookie_path("KIMI_REFRESH_TOKEN_FILE")
        if refresh_path.exists():
            refresh = refresh_path.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.debug(f"kimi_code: could not read refresh token: {e}")

    if not refresh:
        return None

    # The web app calls `refreshToken({refreshToken})` with the credential in
    # the request body. Some gateway builds also want it as a bearer header,
    # so try the body form first and fall back to the header form.
    url = f"{settings.KIMI_AUTH_BASE_URL}/account.gateway.v1.AuthService/RefreshToken"
    base_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://www.kimi.com",
        "Referer": "https://www.kimi.com/",
        "x-msh-platform": "web",
    }
    attempts = [
        ({"refresh_token": refresh}, {}),
        ({"refreshToken": refresh}, {}),
        ({"refresh_token": refresh}, {"Authorization": f"Bearer {refresh}"}),
    ]
    resp = None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            for body, extra in attempts:
                resp = await client.post(
                    url, json=body,
                    headers={**base_headers, **extra},
                )
                if resp.status_code == 200:
                    break
    except Exception as e:
        logger.warning(f"kimi_code: token refresh request failed: {e}")
        return None

    if resp.status_code != 200:
        logger.warning(
            f"kimi_code: token refresh failed ({resp.status_code}) — "
            "re-run `python scripts/kimi_qr_login.py` to log in again"
        )
        return None

    try:
        data = resp.json()
    except Exception:
        return None

    token = (data.get("access_token") or data.get("accessToken")
             or (data.get("result") or {}).get("access_token")
             or (data.get("result") or {}).get("accessToken"))
    if not token:
        return None

    new_refresh = (data.get("refresh_token") or data.get("refreshToken"))
    try:
        token_path = settings.cookie_path("KIMI_TOKEN_FILE")
        token_path.write_text(token, encoding="utf-8")
        token_path.chmod(0o600)
        if new_refresh:
            refresh_path = settings.cookie_path("KIMI_REFRESH_TOKEN_FILE")
            refresh_path.write_text(new_refresh, encoding="utf-8")
            refresh_path.chmod(0o600)
    except Exception as e:
        logger.debug(f"kimi_code: could not persist refreshed token: {e}")

    logger.info("kimi_code: refreshed the Kimi web token")
    return token


async def _fetch_membership_stats() -> dict | None:
    """Fetch the console's own quota view via the membership gateway.

    Returns a dict with:
      "monthly": (percent, reset_dt)          — the console's 月度用量
      "5h":      (percent, reset_dt)          — console's 5 小时用量
      "weekly":  (percent, reset_dt)          — console's 7 天用量

    Returns None when unavailable. Never raises — the membership source is
    an enrichment; the Code API alone still yields a monthly number.
    """
    token = _kimi_auth_token()
    if not token:
        logger.info(
            "kimi_code: no Kimi web token found — the 月度余额 (monthly balance) "
            "is unavailable; only the 加速包 spend and the request pools can be "
            "read. To match the console exactly, set KIMI_AUTH_TOKEN in .env "
            "(see README: Kimi 月度额度)"
        )
        return None

    expired = _jwt_expired(token)
    if expired:
        # The access token is short-lived; the refresh token keeps the
        # session alive. Try to renew before giving up.
        refreshed = await _refresh_access_token()
        if refreshed:
            token = refreshed
        else:
            logger.info(
                "kimi_code: the Kimi web token has expired and could not be "
                "refreshed — run `python scripts/kimi_qr_login.py` to log in again "
                "(see README: Kimi 月度额度)"
            )
            return None

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                settings.KIMI_MEMBERSHIP_URL,
                json={},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
        if resp.status_code in (401, 403):
            logger.warning(
                f"kimi_code: membership API auth failed ({resp.status_code}) — the "
                "Kimi web token was rejected. Refresh it with "
                "`python scripts/fetch_kimi_token.py` (see README: Kimi 月度额度)"
            )
            return None
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"kimi_code: membership API request failed: {e}")
        return None

    if not isinstance(data, dict):
        return None

    out: dict = {}

    # `subscriptionBalance.amountUsedRatio` is the TOTAL monthly pool usage
    # across all of Kimi. This is the number the Kimi console shows as
    # 「月度余额」 and the one the settings page renders as 「总使用量」
    # (subscription.quota.total).
    #
    # The page splits it into two bars: Kimi and Code. The companion
    # `kimiCodeUsedRatio` is only the Code slice of the total, and the Kimi
    # slice is `max(total - code, 0)` — so the total is NOT the Code slice.
    # The Code share is surfaced separately and never substituted.
    #
    # Field names verified against the live proto descriptor
    # kimi/gateway/membership/v2/membership.proto (package
    # kimi.gateway.membership.v2, service MembershipService, rpc
    # GetSubscriptionStats):
    #   subscription_balance -> subscriptionBalance
    #     amount_used_ratio  -> amountUsedRatio
    #     kimi_code_used_ratio -> kimiCodeUsedRatio
    #     expire_time        -> expireTime
    #   ratelimit_code_5h   -> ratelimitCode5h  {ratio, reset_time}
    #   ratelimit_code_7d   -> ratelimitCode7d  {ratio, reset_time}
    balance = data.get("subscriptionBalance") or {}
    ratio = _num(balance.get("amountUsedRatio"))
    if ratio is not None and 0.0 <= ratio <= 1.0:
        out["monthly"] = (round(ratio * 100, 2), _parse_ts(balance.get("expireTime")))

    code_ratio = _num(balance.get("kimiCodeUsedRatio"))
    if code_ratio is not None and 0.0 <= code_ratio <= 1.0:
        out["monthly_code"] = (round(code_ratio * 100, 2),
                               _parse_ts(balance.get("expireTime")))

    # The Code-side rate-limit windows. The `ratelimitCode*` keys are the
    # Kimi Code slices; the plain `ratelimit*` keys are the rest of Kimi.
    for key, window in (("ratelimitCode5h", "5h"), ("ratelimitCode7d", "weekly")):
        block = data.get(key) or {}
        if not isinstance(block, dict):
            continue
        r = _num(block.get("ratio"))
        if r is None or not (0.0 <= r <= 1.0):
            continue
        out[window] = (round(r * 100, 2), _parse_ts(block.get("resetTime")))

    return out or None


def _same_window(a: datetime | None, b: datetime | None, tolerance: int = 2) -> bool:
    """True when two reset instants describe the same rolling window."""
    if a is None or b is None:
        return False
    return abs((a - b).total_seconds()) <= tolerance


def _reconcile(name: str, current: WindowQuota | None, current_reset: datetime | None,
               other) -> tuple[WindowQuota | None, datetime | None]:
    """Merge a membership-API reading into a Code-API window.

    The Code API is known to report zeroed ratios during real usage while
    the console shows the true figure for the same window. When both
    sources describe the same rolling window (reset instants within 2s),
    the more-exhausted reading wins. A reading from a different period is
    never substituted.

    Returns (window, reset_dt).
    """
    if not other:
        return current, current_reset
    pct, reset_dt = other
    if current is None:
        return _percent_window(name, pct, reset_dt), reset_dt
    # Same window? Only then may the two readings be compared.
    if not _same_window(current_reset, reset_dt):
        return current, current_reset
    if pct > current.percent:
        logger.info(
            f"kimi_code: {name} usage raised {current.percent}% → {pct}% "
            "from the membership API"
        )
        return _percent_window(name, pct, reset_dt), reset_dt
    return current, current_reset


# ─── Monthly window assembly ────────────────────────────────────────
def _booster_window(payload) -> WindowQuota | None:
    """The booster wallet (加速包) as its own window — NOT the monthly quota.

    `booster_wallet.monthlyUsed` is the money spent on the 加速包 so far this
    period and `monthlyChargeLimit` is the cap on that spend. Their ratio is
    "how much of this month's 加速包 budget is gone", which is a spend ratio
    over a money cap — it is NOT the 月度余额 quota percentage the console
    shows. Reporting it as the monthly window would mislabel a money figure
    as a quota.

    `used`/`limit` are carried in cents (the API's own unit) so the
    dashboard can show the real amounts, not just the ratio.
    """
    wallet = payload.get("booster_wallet") or {}
    if not isinstance(wallet, dict):
        return None
    used = _num((wallet.get("monthlyUsed") or {}).get("priceInCents"))
    limit = _num((wallet.get("monthlyChargeLimit") or {}).get("priceInCents"))
    if used is None or limit is None or limit <= 0:
        return None
    return WindowQuota(
        window="booster",
        used=used,
        limit=limit,
        reset_in_seconds=None,
    )


def _booster_balance_cents(payload) -> float | None:
    """Money still unspent in the 加速包 (balance.amountLeft), in cents.

    `balance.amount`/`amountLeft` are fixed-point with 1e6 units per cent.
    """
    wallet = payload.get("booster_wallet") or {}
    if not isinstance(wallet, dict):
        return None
    balance = wallet.get("balance") or {}
    if not isinstance(balance, dict):
        return None
    left = _num(balance.get("amountLeft"))
    if left is None:
        return None
    return round(left / 1_000_000, 2)


def _monthly_window(payload, membership) -> tuple[WindowQuota | None, str]:
    """Pick the real MONTHLY BALANCE (月度余额). Returns (window, source name).

    Precedence:
      1. membership API — `subscriptionBalance.amountUsedRatio`, the exact
         number the Kimi console displays.
      2. `usages.limit_month_total` — the Code API's monthly pool ratio.

    There is deliberately NO booster-wallet fallback: the 加速包 spend ratio
    is a different quantity and must never stand in for the monthly balance.
    When neither source has a monthly figure the window is simply absent —
    an honest gap beats a mislabelled number.
    """
    if membership:
        monthly = membership.get("monthly")
        if monthly:
            pct, reset_dt = monthly
            return _percent_window("monthly", pct, reset_dt), "membership"

    pools = payload.get("usages") or {}
    pool = pools.get("limit_month_total") if isinstance(pools, dict) else None
    pct = _ratio_percent(pool)
    if pct is not None:
        return _percent_window("monthly", pct, _ratio_reset(pool)), "limit_month_total"

    return None, "none"


# ─── Fetch ──────────────────────────────────────────────────────────
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

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                settings.KIMI_API_URL,
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

    try:
        windows: list[WindowQuota] = []

        # Which upstream each window came from, surfaced as a metric so the
        # dashboard can explain a number that differs from the vendor console.
        sources: dict[str, str] = {}

        # ── 5h window ────────────────────────────────────────────
        # Match on the window's own length, never on array position —
        # `limits[]` order is not guaranteed and has changed upstream.
        five_h: WindowQuota | None = None
        five_h_reset: datetime | None = None
        for entry in payload.get("limits") or []:
            if not isinstance(entry, dict):
                continue
            if _window_minutes(entry.get("window")) != FIVE_HOUR_MINUTES:
                continue
            detail = entry.get("detail") or {}
            five_h_reset = _block_reset(detail)
            counts = _usage_counts(detail)
            if counts:
                used, limit = counts
                five_h = WindowQuota(
                    window="5h",
                    used=used,
                    limit=limit,
                    reset_in_seconds=_reset_in_seconds(five_h_reset),
                )
            break

        # ── weekly pool ──────────────────────────────────────────
        # Shape A puts it in the top-level `usage` block; shape B exposes
        # it only as the `limit_7d` ratio pool.
        weekly: WindowQuota | None = None
        weekly_reset: datetime | None = None
        usage_block = payload.get("usage") or {}
        weekly_reset = _block_reset(usage_block)
        counts = _usage_counts(usage_block)
        if counts:
            used, limit = counts
            weekly = WindowQuota(
                window="weekly",
                used=used,
                limit=limit,
                reset_in_seconds=_reset_in_seconds(weekly_reset),
            )

        pools = payload.get("usages") or {}
        if not isinstance(pools, dict):
            pools = {}

        if five_h is None:
            pct = _ratio_percent(pools.get("limit_5h"))
            if pct is not None:
                five_h_reset = _ratio_reset(pools.get("limit_5h"))
                five_h = _percent_window("5h", pct, five_h_reset)

        if five_h is not None:
            sources["5h"] = "code_api"

        if weekly is None:
            pct = _ratio_percent(pools.get("limit_7d"))
            if pct is not None:
                weekly_reset = _ratio_reset(pools.get("limit_7d"))
                weekly = _percent_window("weekly", pct, weekly_reset)

        if weekly is not None:
            sources["weekly"] = "code_api"

        # ── membership API (console's own view) ───────────────────
        # Supplies the monthly figure the console shows, and can also
        # correct the 5h/weekly windows when the Code API under-reports
        # them (it is known to report zeroed ratios during real usage).
        membership = await _fetch_membership_stats()

        if membership:
            five_h, five_h_reset = _reconcile(
                "5h", five_h, five_h_reset, membership.get("5h")
            )
            weekly, weekly_reset = _reconcile(
                "weekly", weekly, weekly_reset, membership.get("weekly")
            )
            # When the membership API supplies (or raises) a window, that
            # becomes its active source.
            if membership.get("5h") and five_h is not None:
                sources["5h"] = "membership_api"
            if membership.get("weekly") and weekly is not None:
                sources["weekly"] = "membership_api"

        if five_h is not None:
            windows.append(five_h)
        if weekly is not None:
            windows.append(weekly)

        # ── monthly balance (月度余额) ───────────────────────────
        monthly, monthly_source = _monthly_window(payload, membership)
        if monthly is not None:
            windows.append(monthly)
            sources["monthly"] = monthly_source
            logger.info(
                f"kimi_code: monthly balance {monthly.percent}% "
                f"(source={_monthly_source_label(monthly_source)})"
            )
        else:
            logger.warning(
                "kimi_code: no monthly balance available — the 月度余额 comes only "
                "from the membership API (console session). Add a Kimi web token to "
                "see it; see README: Kimi 月度额度"
            )

        # ── booster wallet (加速包) ───────────────────────────────
        # A separate quantity: money spent on the 加速包 this period against
        # its monthly cap. Never reported as the monthly balance.
        booster = _booster_window(payload)
        if booster is not None:
            windows.append(booster)
            sources["booster"] = "code_api_booster_wallet"
            balance_left = _booster_balance_cents(payload)
            extra = (
                f", CNY {balance_left / 100:.2f} left in pack"
                if balance_left is not None else ""
            )
            logger.info(
                f"kimi_code: booster spend CNY {booster.used / 100:.2f} of "
                f"CNY {booster.limit / 100:.2f} ({booster.percent}%){extra}"
            )

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
            sources=sources,
        )

    except Exception as e:
        logger.exception("kimi_code parsing failed")
        return ProviderResult(
            provider="kimi_code",
            fetched_at=now,
            windows=(),
            success=False,
            error=str(e),
        )


def _monthly_source_label(source: str) -> str:
    """Human-readable label for the monthly window's upstream source."""
    return {
        "membership": "membership API (console)",
        "limit_month_total": "usages.limit_month_total",
        "none": "none",
    }.get(source, source)
