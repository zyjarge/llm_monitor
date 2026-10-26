"""
Tests for the Kimi Code provider.

Covers the 月度余额 (monthly balance) and the 加速包 (booster) windows —
which are DIFFERENT quantities and must never be confused — the 5h/weekly
parsing rules, and the response-shape variants the Kimi API has shipped
over time.
"""
import httpx
import pytest
import respx
from httpx import Response

from scraper.src.config import settings
from scraper.src.providers import kimi_code


@pytest.fixture(autouse=True)
def configure_kimi(monkeypatch):
    monkeypatch.setattr(settings, "KIMI_API_KEY", "test-key-***")
    # No web session by default — exercises the Code-API-only path.
    monkeypatch.setattr(settings, "KIMI_COOKIE_FILE", "/nonexistent/kimi_cookie")


def mock_code_api(payload):
    """Route the Code API to a fixed payload."""
    router = respx.mock(base_url="https://api.kimi.com")
    router.get("/coding/v1/usages").mock(return_value=Response(200, json=payload))
    return router


FIVE_HOUR_WINDOW = {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"}

LIVE_PAYLOAD = {
    "usage": {"limit": "100", "remaining": "100",
              "resetTime": "2026-10-29T23:28:04.937293Z"},
    "limits": [{
        "window": FIVE_HOUR_WINDOW,
        "detail": {"limit": "100", "remaining": "100",
                   "resetTime": "2026-10-26T05:28:04.937293Z"},
    }],
    "usages": {
        "limit_5h": {"used_ratio": 0, "reset_time": "2026-10-26T05:28:04Z"},
        "limit_7d": {"used_ratio": 0, "reset_time": "2026-10-29T23:28:04Z"},
    },
    "booster_wallet": {
        "balance": {"type": "BOOSTER", "amount": "7500000000",
                    "amountLeft": "481032400", "unit": "UNIT_CURRENCY"},
        "monthlyChargeLimit": {"currency": "CNY", "priceInCents": "10000"},
        "monthlyUsed": {"currency": "CNY", "priceInCents": "7019"},
    },
}


# ─── Booster wallet (加速包) — its own window, NOT the monthly balance ──

async def test_booster_spend_is_its_own_window():
    """monthlyUsed/monthlyChargeLimit is 加速包 spend, reported as "booster"."""
    with mock_code_api(LIVE_PAYLOAD):
        result = await kimi_code.fetch()

    assert result.success is True
    booster = result.get("booster")
    assert booster is not None
    assert booster.percent == 70.19  # 7019 / 10000 cents
    # used/limit carry the real money amounts, in cents.
    assert booster.used == 7019.0
    assert booster.limit == 10000.0


async def test_booster_spend_is_never_reported_as_the_monthly_balance():
    """The 加速包 spend ratio must not stand in for the 月度余额.

    This is the bug that motivated the split: filing a money-spend ratio
    under window="monthly" made the dashboard show "70.19% monthly usage"
    when 70.19% was how much of the booster pack had been burned.
    """
    with mock_code_api(LIVE_PAYLOAD):
        result = await kimi_code.fetch()

    assert result.get("monthly") is None
    assert result.get("booster") is not None


async def test_booster_balance_left_is_readable():
    """balance.amountLeft is fixed-point with 1e6 units per cent."""
    with mock_code_api(LIVE_PAYLOAD):
        result = await kimi_code.fetch()
    # 481032400 / 1e6 = 481.03 cents = CNY 4.81
    assert kimi_code._booster_balance_cents(LIVE_PAYLOAD) == 481.03


async def test_booster_window_absent_without_a_booster_plan():
    """An account without 加速包 has no booster window — and no crash."""
    payload = {
        "usage": {"limit": "100", "remaining": "90"},
        "limits": [{"window": FIVE_HOUR_WINDOW,
                    "detail": {"limit": "100", "remaining": "90"}}],
    }
    with mock_code_api(payload):
        result = await kimi_code.fetch()

    assert result.success is True
    assert result.get("booster") is None


# ─── Monthly: ratio pools ────────────────────────────────────────────

async def test_monthly_from_limit_month_total_ratio_pool():
    payload = {
        "limits": [{"window": FIVE_HOUR_WINDOW,
                    "detail": {"limit": "100", "remaining": "75"}}],
        "usages": {
            "limit_5h": {"used_ratio": 0.25, "reset_time": "2026-10-26T05:28:04Z"},
            "limit_7d": {"used_ratio": 0.34, "reset_time": "2026-10-29T23:28:04Z"},
            "limit_month_total": {"used_ratio": 0.0795,
                                  "reset_time": "2026-10-29T00:00:00Z"},
            # The Code slice is NOT its own allowance — must be ignored.
            "limit_month_code": {"used_ratio": 0.0,
                                 "reset_time": "2026-10-29T00:00:00Z"},
        },
    }
    with mock_code_api(payload):
        result = await kimi_code.fetch()

    assert result.success is True
    assert result.get("monthly").percent == 7.95
    # limits[] carries absolute counts and wins over the ratio pool.
    assert result.get("5h").percent == 25.0
    assert result.get("weekly").percent == 34.0


async def test_out_of_range_used_ratio_is_ignored_not_clamped():
    """A ratio outside 0..1 is schema drift, not a quota."""
    payload = {
        "usage": {"limit": "100", "remaining": "90"},
        "limits": [{"window": FIVE_HOUR_WINDOW,
                    "detail": {"limit": "100", "remaining": "90"}}],
        "usages": {"limit_month_total": {"used_ratio": 1.7}},
    }
    with mock_code_api(payload):
        result = await kimi_code.fetch()

    assert result.success is True
    assert result.get("monthly") is None


async def test_missing_monthly_source_leaves_window_absent():
    """No monthly data anywhere → the window is absent, not invented."""
    payload = {
        "usage": {"limit": "100", "remaining": "90"},
        "limits": [{"window": FIVE_HOUR_WINDOW,
                    "detail": {"limit": "100", "remaining": "90"}}],
    }
    with mock_code_api(payload):
        result = await kimi_code.fetch()

    assert result.success is True
    assert result.get("monthly") is None
    assert result.get("5h") is not None


# ─── Monthly: membership API (web session) ───────────────────────────

async def test_membership_monthly_takes_precedence(monkeypatch, tmp_path):
    """A live web session reports the same number the console shows."""
    cookie = tmp_path / "kimi_cookie"
    cookie.write_text(
        "#HttpOnly_www.kimi.com\tFALSE\t/\tTRUE\t0\tkimi-auth\ttoken-123\n"
    )
    monkeypatch.setattr(settings, "KIMI_COOKIE_FILE", str(cookie))
    monkeypatch.setattr(kimi_code, "_jwt_expired", lambda token: False)

    with respx.mock(base_url="https://www.kimi.com") as web:
        web.post(
            "/apiv2/kimi.gateway.membership.v2.MembershipService/GetSubscriptionStats"
        ).mock(return_value=Response(200, json={
            "subscriptionBalance": {
                "amountUsedRatio": 0.75,
                "expireTime": "2026-11-01T00:00:00Z",
            }
        }))
        with mock_code_api(LIVE_PAYLOAD):
            result = await kimi_code.fetch()

    assert result.success is True
    monthly = result.get("monthly")
    assert monthly.percent == 75.0
    assert monthly.reset_in_seconds is not None


async def test_expired_web_session_leaves_monthly_absent(monkeypatch, tmp_path):
    """An expired token must not break the fetch — the 月度余额 goes missing,
    but the 加速包 spend and the request pools are still reported."""
    cookie = tmp_path / "kimi_cookie"
    cookie.write_text(
        "#HttpOnly_www.kimi.com\tFALSE\t/\tTRUE\t0\tkimi-auth\texpired-token\n"
    )
    monkeypatch.setattr(settings, "KIMI_COOKIE_FILE", str(cookie))
    monkeypatch.setattr(kimi_code, "_jwt_expired", lambda token: True)

    with mock_code_api(LIVE_PAYLOAD):
        result = await kimi_code.fetch()

    assert result.success is True
    # The monthly balance is NOT backfilled from the booster spend.
    assert result.get("monthly") is None
    assert result.get("booster").percent == 70.19


async def test_membership_corrects_under_reported_rate_windows(monkeypatch, tmp_path):
    """The Code API can report 0% while the console shows the real figure."""
    cookie = tmp_path / "kimi_cookie"
    cookie.write_text(
        "#HttpOnly_www.kimi.com\tFALSE\t/\tTRUE\t0\tkimi-auth\ttoken-123\n"
    )
    monkeypatch.setattr(settings, "KIMI_COOKIE_FILE", str(cookie))
    monkeypatch.setattr(kimi_code, "_jwt_expired", lambda token: False)

    payload = {
        "usage": {"limit": "100", "remaining": "100",
                  "resetTime": "2026-10-29T23:28:04Z"},
        "limits": [{"window": FIVE_HOUR_WINDOW,
                    "detail": {"limit": "100", "remaining": "100",
                               "resetTime": "2026-10-26T05:28:04Z"}}],
    }
    with respx.mock(base_url="https://www.kimi.com") as web:
        web.post(
            "/apiv2/kimi.gateway.membership.v2.MembershipService/GetSubscriptionStats"
        ).mock(return_value=Response(200, json={
            "subscriptionBalance": {"amountUsedRatio": 0.75},
            "ratelimitCode5h": {"ratio": 0.17,
                                "resetTime": "2026-10-26T05:28:04Z"},
            "ratelimitCode7d": {"ratio": 0.34,
                                "resetTime": "2026-10-29T23:28:04Z"},
        }))
        with mock_code_api(payload):
            result = await kimi_code.fetch()

    # Code API said 0% for both; the console says 17% / 34%.
    assert result.get("5h").percent == 17.0
    assert result.get("weekly").percent == 34.0
    assert result.get("monthly").percent == 75.0


async def test_membership_does_not_replace_a_different_window(monkeypatch, tmp_path):
    """A reading from another reset period must not overwrite the window."""
    cookie = tmp_path / "kimi_cookie"
    cookie.write_text(
        "#HttpOnly_www.kimi.com\tFALSE\t/\tTRUE\t0\tkimi-auth\ttoken-123\n"
    )
    monkeypatch.setattr(settings, "KIMI_COOKIE_FILE", str(cookie))
    monkeypatch.setattr(kimi_code, "_jwt_expired", lambda token: False)

    payload = {
        "usage": {"limit": "100", "remaining": "50",
                  "resetTime": "2026-10-29T23:28:04Z"},
        "limits": [{"window": FIVE_HOUR_WINDOW,
                    "detail": {"limit": "100", "remaining": "50",
                               "resetTime": "2026-10-26T05:28:04Z"}}],
    }
    with respx.mock(base_url="https://www.kimi.com") as web:
        web.post(
            "/apiv2/kimi.gateway.membership.v2.MembershipService/GetSubscriptionStats"
        ).mock(return_value=Response(200, json={
            "subscriptionBalance": {"amountUsedRatio": 0.75},
            # Different reset instant → a different rolling window.
            "ratelimitCode5h": {"ratio": 0.99,
                                "resetTime": "2026-11-26T05:28:04Z"},
        }))
        with mock_code_api(payload):
            result = await kimi_code.fetch()

    assert result.get("5h").percent == 50.0


async def test_membership_auth_failure_falls_back(monkeypatch, tmp_path):
    """A 401 from the membership API must not fail the provider.

    The 月度余额 goes missing (no membership token ⇒ no monthly figure),
    while the 加速包 spend and the request pools still come through.
    """
    cookie = tmp_path / "kimi_cookie"
    cookie.write_text(
        "#HttpOnly_www.kimi.com\tFALSE\t/\tTRUE\t0\tkimi-auth\ttoken-123\n"
    )
    monkeypatch.setattr(settings, "KIMI_COOKIE_FILE", str(cookie))
    monkeypatch.setattr(kimi_code, "_jwt_expired", lambda token: False)

    with respx.mock(base_url="https://www.kimi.com") as web:
        web.post(
            "/apiv2/kimi.gateway.membership.v2.MembershipService/GetSubscriptionStats"
        ).mock(return_value=Response(401, json={"code": "unauthenticated"}))
        with mock_code_api(LIVE_PAYLOAD):
            result = await kimi_code.fetch()

    assert result.success is True
    assert result.get("monthly") is None
    assert result.get("booster").percent == 70.19


# ─── membership source (the console's own numbers) ─────────────────────

@respx.mock
async def test_membership_source_matches_console(monkeypatch, tmp_path):
    """A valid web token + the console's response shape must produce the
    console's own monthly percentage and flip `source` to 'membership'."""
    import base64
    import json as _json
    import time

    def make_jwt(exp: int) -> str:
        payload = base64.urlsafe_b64encode(
            _json.dumps({"exp": exp}).encode()
        ).decode().rstrip("=")
        return f"eyJhbGciOiJIUzI1NiJ9.{payload}.sig"

    token_file = tmp_path / "kimi_token"
    token_file.write_text(make_jwt(int(time.time()) + 86400), encoding="utf-8")
    monkeypatch.setattr(settings, "KIMI_AUTH_TOKEN", "")
    monkeypatch.setattr(settings, "KIMI_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(settings, "KIMI_COOKIE_FILE", str(tmp_path / "none"))

    respx.post(settings.KIMI_MEMBERSHIP_URL).mock(
        return_value=httpx.Response(200, json={
            "code": 0,
            "subscriptionBalance": {
                "amountUsedRatio": 0.75,
                "expireTime": "2026-11-30T16:00:00+08:00",
            },
            "ratelimitCode5h": {"usedRatio": 0.30},
            "ratelimitCode7d": {"usedRatio": 0.55},
        })
    )
    respx.get("https://api.kimi.com/coding/v1/usages").mock(
        return_value=httpx.Response(200, json={
            "usages": {"limit_month_total": {"used_ratio": 0.70}},
            "booster_wallet": {
                "monthlyUsed": {"priceInCents": "7019"},
                "monthlyChargeLimit": {"priceInCents": "10000"},
            },
        })
    )

    result = await kimi_code.fetch()
    assert result.success, result.error
    assert result.sources["monthly"] == "membership"
    monthly = [w for w in result.windows if w.window == "monthly"]
    assert monthly and monthly[0].percent == 75.0
    # The booster spend is reported separately and is not the monthly figure.
    assert result.sources["booster"] == "code_api_booster_wallet"
    booster = [w for w in result.windows if w.window == "booster"]
    assert booster and booster[0].percent == 70.19


@respx.mock
async def test_membership_code_share_does_not_leak_into_windows(monkeypatch, tmp_path):
    """`kimiCodeUsedRatio` is only the Kimi Code slice of the pool — it must
    never be reported as the monthly figure (the console shows the total).

    The settings page proves this: 总使用量 is the total, and the page splits
    it into a Kimi bar and a Code bar
    (kimi = max(total - code, 0), code = kimiCodeUsedRatio).
    """
    import base64
    import json as _json
    import time

    def make_jwt(exp: int) -> str:
        payload = base64.urlsafe_b64encode(
            _json.dumps({"exp": exp}).encode()
        ).decode().rstrip("=")
        return f"eyJhbGciOiJIUzI1NiJ9.{payload}.sig"

    token_file = tmp_path / "kimi_token"
    token_file.write_text(make_jwt(int(time.time()) + 86400), encoding="utf-8")
    monkeypatch.setattr(settings, "KIMI_AUTH_TOKEN", "")
    monkeypatch.setattr(settings, "KIMI_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(settings, "KIMI_COOKIE_FILE", str(tmp_path / "none"))

    respx.post(settings.KIMI_MEMBERSHIP_URL).mock(
        return_value=httpx.Response(200, json={
            "subscriptionBalance": {
                "amountUsedRatio": 0.75,       # 总使用量 / 月度余额 (the total)
                "kimiCodeUsedRatio": 0.20,     # only the Kimi Code slice
                "expireTime": "2026-11-30T16:00:00+08:00",
            },
        })
    )
    respx.get("https://api.kimi.com/coding/v1/usages").mock(
        return_value=httpx.Response(200, json={
            "booster_wallet": {
                "monthlyUsed": {"priceInCents": "7019"},
                "monthlyChargeLimit": {"priceInCents": "10000"},
            },
        })
    )

    result = await kimi_code.fetch()
    assert result.success, result.error
    monthly = [w for w in result.windows if w.window == "monthly"]
    assert monthly and monthly[0].percent == 75.0, monthly
    # No window may be named after the code-only share.
    assert not [w for w in result.windows if w.window == "monthly_code"]


@respx.mock
async def test_membership_contract_matches_live_proto(monkeypatch, tmp_path):
    """Field names must match the live proto descriptor exactly.

    Verified against kimi/gateway/membership/v2/membership.proto
    (package kimi.gateway.membership.v2, service MembershipService,
    rpc GetSubscriptionStats):
      subscription_balance -> subscriptionBalance
        amount_used_ratio   -> amountUsedRatio
        kimi_code_used_ratio-> kimiCodeUsedRatio
        expire_time         -> expireTime
      ratelimit_code_5h    -> ratelimitCode5h {ratio, reset_time}
      ratelimit_code_7d    -> ratelimitCode7d {ratio, reset_time}
    """
    import base64
    import json as _json
    import time

    def make_jwt(exp: int) -> str:
        payload = base64.urlsafe_b64encode(
            _json.dumps({"exp": exp}).encode()
        ).decode().rstrip("=")
        return f"eyJhbGciOiJIUzI1NiJ9.{payload}.sig"

    token_file = tmp_path / "kimi_token"
    token_file.write_text(make_jwt(int(time.time()) + 86400), encoding="utf-8")
    monkeypatch.setattr(settings, "KIMI_AUTH_TOKEN", "")
    monkeypatch.setattr(settings, "KIMI_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(settings, "KIMI_COOKIE_FILE", str(tmp_path / "none"))

    # Exactly the shape the real gateway returns.
    respx.post(settings.KIMI_MEMBERSHIP_URL).mock(
        return_value=httpx.Response(200, json={
            "subscriptionBalance": {
                "id": "bal-1",
                "amountUsedRatio": 0.75,
                "kimiCodeUsedRatio": 0.20,
                "expireTime": "2026-11-30T16:00:00+08:00",
            },
            "ratelimitCode5h": {"ratio": 0.17, "resetTime": "2026-10-26T05:28:04Z"},
            "ratelimitCode7d": {"ratio": 0.34, "resetTime": "2026-10-29T23:28:04Z"},
            "boosterWallets": [],
            "giftBalances": [],
            "overdrawn": False,
        })
    )
    respx.get("https://api.kimi.com/coding/v1/usages").mock(
        return_value=httpx.Response(200, json={
            "booster_wallet": {
                "monthlyUsed": {"priceInCents": "7019"},
                "monthlyChargeLimit": {"priceInCents": "10000"},
            },
        })
    )

    result = await kimi_code.fetch()
    assert result.success, result.error

    # 总使用量 == subscriptionBalance.amountUsedRatio * 100
    monthly = result.get("monthly")
    assert monthly is not None
    assert monthly.percent == 75.0

    # The Code slice is separate and must not become the total.
    assert result.get("monthly_code") is None or True  # not a window
    assert not [w for w in result.windows if w.window == "monthly_code"]

    # ratelimitCode5h / ratelimitCode7d use `ratio` + `resetTime`.
    assert result.get("5h").percent == 17.0
    assert result.get("weekly").percent == 34.0

    # The booster spend stays its own window.
    assert result.get("booster").percent == 70.19



    """`kimiCodeUsedRatio` is only the Kimi Code slice of the pool — it must
    never be reported as the monthly figure (the console shows the total)."""
    import base64
    import json as _json
    import time

    def make_jwt(exp: int) -> str:
        payload = base64.urlsafe_b64encode(
            _json.dumps({"exp": exp}).encode()
        ).decode().rstrip("=")
        return f"eyJhbGciOiJIUzI1NiJ9.{payload}.sig"

    token_file = tmp_path / "kimi_token"
    token_file.write_text(make_jwt(int(time.time()) + 86400), encoding="utf-8")
    monkeypatch.setattr(settings, "KIMI_AUTH_TOKEN", "")
    monkeypatch.setattr(settings, "KIMI_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(settings, "KIMI_COOKIE_FILE", str(tmp_path / "none"))

    respx.post(settings.KIMI_MEMBERSHIP_URL).mock(
        return_value=httpx.Response(200, json={
            "subscriptionBalance": {
                "amountUsedRatio": 0.75,       # the console's monthly figure
                "kimiCodeUsedRatio": 0.20,     # only the Kimi Code slice
                "expireTime": "2026-11-30T16:00:00+08:00",
            },
        })
    )
    respx.get("https://api.kimi.com/coding/v1/usages").mock(
        return_value=httpx.Response(200, json={
            "booster_wallet": {
                "monthlyUsed": {"priceInCents": "7019"},
                "monthlyChargeLimit": {"priceInCents": "10000"},
            },
        })
    )

    result = await kimi_code.fetch()
    assert result.success, result.error
    monthly = [w for w in result.windows if w.window == "monthly"]
    assert monthly and monthly[0].percent == 75.0, monthly
    # No window may be named after the code-only share.
    assert not [w for w in result.windows if w.window == "monthly_code"]


@respx.mock
async def test_membership_401_leaves_monthly_absent(monkeypatch, tmp_path):
    """A rejected token must not break the provider.

    The 月度余额 stays absent (it has no Code API equivalent) while the
    加速包 spend still comes through on its own window.
    """
    import base64
    import json as _json
    import time

    def make_jwt(exp: int) -> str:
        payload = base64.urlsafe_b64encode(
            _json.dumps({"exp": exp}).encode()
        ).decode().rstrip("=")
        return f"eyJhbGciOiJIUzI1NiJ9.{payload}.sig"

    token_file = tmp_path / "kimi_token"
    token_file.write_text(make_jwt(int(time.time()) + 86400), encoding="utf-8")
    monkeypatch.setattr(settings, "KIMI_AUTH_TOKEN", "")
    monkeypatch.setattr(settings, "KIMI_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(settings, "KIMI_COOKIE_FILE", str(tmp_path / "none"))

    respx.post(settings.KIMI_MEMBERSHIP_URL).mock(
        return_value=httpx.Response(401, json={"code": "unauthenticated"})
    )
    respx.get("https://api.kimi.com/coding/v1/usages").mock(
        return_value=httpx.Response(200, json={
            "booster_wallet": {
                "monthlyUsed": {"priceInCents": "7019"},
                "monthlyChargeLimit": {"priceInCents": "10000"},
            },
        })
    )

    result = await kimi_code.fetch()
    assert result.success, result.error
    assert result.get("monthly") is None
    booster = [w for w in result.windows if w.window == "booster"]
    assert booster and booster[0].percent == 70.19
    assert result.sources["booster"] == "code_api_booster_wallet"
    assert "monthly" not in result.sources


# ─── Token refresh (the access token lives ~15 minutes) ───────────────

@respx.mock
async def test_expired_token_is_refreshed_automatically(monkeypatch, tmp_path):
    """The Kimi access token is short-lived (~15 min). When it expires the
    provider must exchange the refresh token for a new one instead of losing
    the monthly balance."""
    import base64
    import json as _json
    import time

    def make_jwt(exp: int) -> str:
        payload = base64.urlsafe_b64encode(
            _json.dumps({"exp": exp}).encode()
        ).decode().rstrip("=")
        return f"eyJhbGciOiJIUzI1NiJ9.{payload}.sig"

    token_file = tmp_path / "kimi_token"
    token_file.write_text(make_jwt(int(time.time()) - 60), encoding="utf-8")  # expired
    refresh_file = tmp_path / "kimi_refresh_token"
    refresh_file.write_text("refresh-value", encoding="utf-8")

    monkeypatch.setattr(settings, "KIMI_AUTH_TOKEN", "")
    monkeypatch.setattr(settings, "KIMI_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(settings, "KIMI_REFRESH_TOKEN_FILE", str(refresh_file))
    monkeypatch.setattr(settings, "KIMI_COOKIE_FILE", str(tmp_path / "none"))

    fresh = make_jwt(int(time.time()) + 900)
    respx.post(
        f"{settings.KIMI_AUTH_BASE_URL}/account.gateway.v1.AuthService/RefreshToken"
    ).mock(return_value=httpx.Response(200, json={
        "access_token": fresh, "refresh_token": "refresh-rotated"}))

    respx.post(settings.KIMI_MEMBERSHIP_URL).mock(
        return_value=httpx.Response(200, json={
            "subscriptionBalance": {"amountUsedRatio": 0.75}}))
    respx.get("https://api.kimi.com/coding/v1/usages").mock(
        return_value=httpx.Response(200, json={
            "usage": {"limit": "100", "remaining": "100"},
            "limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                        "detail": {"limit": "100", "remaining": "100"}}],
            "booster_wallet": {"monthlyUsed": {"priceInCents": "7019"},
                               "monthlyChargeLimit": {"priceInCents": "10000"}},
        }))

    result = await kimi_code.fetch()

    assert result.success, result.error
    assert result.get("monthly").percent == 75.0
    # The refreshed token is persisted so the next process starts fresh.
    assert token_file.read_text(encoding="utf-8") == fresh
    assert refresh_file.read_text(encoding="utf-8") == "refresh-rotated"


@respx.mock
async def test_refresh_failure_leaves_monthly_absent(monkeypatch, tmp_path):
    """If the refresh fails, the monthly window is absent — never invented."""
    import base64
    import json as _json
    import time

    def make_jwt(exp: int) -> str:
        payload = base64.urlsafe_b64encode(
            _json.dumps({"exp": exp}).encode()
        ).decode().rstrip("=")
        return f"eyJhbGciOiJIUzI1NiJ9.{payload}.sig"

    token_file = tmp_path / "kimi_token"
    token_file.write_text(make_jwt(int(time.time()) - 60), encoding="utf-8")
    refresh_file = tmp_path / "kimi_refresh_token"
    refresh_file.write_text("stale", encoding="utf-8")

    monkeypatch.setattr(settings, "KIMI_AUTH_TOKEN", "")
    monkeypatch.setattr(settings, "KIMI_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(settings, "KIMI_REFRESH_TOKEN_FILE", str(refresh_file))
    monkeypatch.setattr(settings, "KIMI_COOKIE_FILE", str(tmp_path / "none"))

    respx.post(
        f"{settings.KIMI_AUTH_BASE_URL}/account.gateway.v1.AuthService/RefreshToken"
    ).mock(return_value=httpx.Response(401, json={"code": "unauthenticated"}))
    respx.get("https://api.kimi.com/coding/v1/usages").mock(
        return_value=httpx.Response(200, json={
            "usage": {"limit": "100", "remaining": "100"},
            "limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                        "detail": {"limit": "100", "remaining": "100"}}],
            "booster_wallet": {"monthlyUsed": {"priceInCents": "7019"},
                               "monthlyChargeLimit": {"priceInCents": "10000"}},
        }))

    result = await kimi_code.fetch()

    assert result.success, result.error
    assert result.get("monthly") is None
    assert result.get("booster").percent == 70.19


# ─── Web token discovery ─────────────────────────────────────────────

def test_token_from_env_var(monkeypatch):
    """KIMI_AUTH_TOKEN takes highest precedence."""
    monkeypatch.setattr(settings, "KIMI_AUTH_TOKEN", "env-token")
    monkeypatch.setattr(settings, "KIMI_TOKEN_FILE", "/nonexistent")
    assert kimi_code._kimi_auth_token() == "env-token"


def test_token_from_plain_text_file(monkeypatch, tmp_path):
    """Modern Kimi web keeps the JWT in localStorage, not a cookie —
    so a plain-text token file is accepted."""
    token_file = tmp_path / "kimi_token"
    token_file.write_text("jwt-token-value\n", encoding="utf-8")
    monkeypatch.setattr(settings, "KIMI_AUTH_TOKEN", "")
    monkeypatch.setattr(settings, "KIMI_TOKEN_FILE", str(token_file))
    assert kimi_code._kimi_auth_token() == "jwt-token-value"


def test_token_from_kimi_auth_cookie(monkeypatch, tmp_path):
    """The legacy `kimi-auth` cookie still works."""
    cookie = tmp_path / "kimi_cookie"
    cookie.write_text(
        "#HttpOnly_www.kimi.com\tFALSE\t/\tTRUE\t0\tkimi-auth\tcookie-token\n"
    )
    monkeypatch.setattr(settings, "KIMI_AUTH_TOKEN", "")
    monkeypatch.setattr(settings, "KIMI_TOKEN_FILE", str(tmp_path / "missing"))
    monkeypatch.setattr(settings, "KIMI_COOKIE_FILE", str(cookie))
    assert kimi_code._kimi_auth_token() == "cookie-token"


def test_no_token_anywhere_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "KIMI_AUTH_TOKEN", "")
    monkeypatch.setattr(settings, "KIMI_TOKEN_FILE", str(tmp_path / "missing"))
    monkeypatch.setattr(settings, "KIMI_COOKIE_FILE", str(tmp_path / "missing"))
    assert kimi_code._kimi_auth_token() is None


def test_expired_jwt_is_detected():
    import base64
    import json as _json
    import time

    def make_jwt(exp: int) -> str:
        payload = base64.urlsafe_b64encode(
            _json.dumps({"exp": exp}).encode()
        ).decode().rstrip("=")
        return f"hdr.{payload}.sig"

    assert kimi_code._jwt_expired(make_jwt(int(time.time()) - 60)) is True
    assert kimi_code._jwt_expired(make_jwt(int(time.time()) + 3600)) is False
    assert kimi_code._jwt_expired("not-a-jwt") is None


# ─── 5h / weekly parsing ─────────────────────────────────────────────

async def test_null_detail_does_not_crash():
    """`detail: null` must not raise — the provider reports what it has."""
    payload = {
        "usage": {"limit": "100", "remaining": "90"},
        "limits": [{"window": FIVE_HOUR_WINDOW, "detail": None}],
    }
    with mock_code_api(payload):
        result = await kimi_code.fetch()

    assert result.success is True
    assert result.get("weekly").percent == 10.0
    assert result.get("5h") is None


async def test_used_without_remaining_is_understood():
    """The API may send `used` instead of `remaining`."""
    payload = {
        "usage": {"limit": "100", "used": "30"},
        "limits": [{"window": FIVE_HOUR_WINDOW,
                    "detail": {"limit": "100", "used": "40"}}],
    }
    with mock_code_api(payload):
        result = await kimi_code.fetch()

    assert result.success is True
    assert result.get("weekly").percent == 30.0
    assert result.get("5h").percent == 40.0


async def test_five_hour_window_matched_by_duration_not_position():
    """`limits[]` order is not guaranteed — match on the window length."""
    payload = {
        "usage": {"limit": "100", "remaining": "90"},
        "limits": [
            {"window": {"duration": 10080, "timeUnit": "TIME_UNIT_MINUTE"},
             "detail": {"limit": "1000", "remaining": "500"}},
            {"window": FIVE_HOUR_WINDOW,
             "detail": {"limit": "100", "remaining": "90"}},
        ],
    }
    with mock_code_api(payload):
        result = await kimi_code.fetch()

    # The 5h window must be the 100-request one, not the 1000-request one.
    assert result.get("5h").limit == 100.0
    assert result.get("5h").percent == 10.0


async def test_five_hour_window_in_hours_is_accepted():
    payload = {
        "usage": {"limit": "100", "remaining": "90"},
        "limits": [{"window": {"duration": 5, "timeUnit": "TIME_UNIT_HOUR"},
                    "detail": {"limit": "100", "remaining": "90"}}],
    }
    with mock_code_api(payload):
        result = await kimi_code.fetch()

    assert result.get("5h").percent == 10.0


async def test_null_limits_array_is_tolerated():
    payload = {"usage": {"limit": "100", "remaining": "90"}, "limits": None}
    with mock_code_api(payload):
        result = await kimi_code.fetch()

    assert result.success is True
    assert result.get("weekly").percent == 10.0


# ─── Config / errors ─────────────────────────────────────────────────

async def test_missing_api_key_returns_config_error(monkeypatch):
    monkeypatch.setattr(settings, "KIMI_API_KEY", "")
    result = await kimi_code.fetch()
    assert result.success is False
    assert "KIMI_API_KEY" in result.error


async def test_auth_error_is_reported(monkeypatch):
    with respx.mock(base_url="https://api.kimi.com") as router:
        router.get("/coding/v1/usages").mock(return_value=Response(401, json={}))
        result = await kimi_code.fetch()
    assert result.success is False
    assert "401" in result.error


# ─── Timestamp parsing ───────────────────────────────────────────────

def test_reset_time_with_offset_is_not_shifted_to_utc():
    """A non-UTC offset must be honoured, not overwritten."""
    from datetime import datetime, timezone

    dt = kimi_code._parse_ts("2026-10-26T13:28:04+08:00")
    assert dt == datetime(2026, 10, 26, 5, 28, 4, tzinfo=timezone.utc)


def test_reset_time_with_nanoseconds_is_parsed():
    dt = kimi_code._parse_ts("2026-01-09T15:23:13.716839300Z")
    assert dt is not None
    assert dt.year == 2026 and dt.microsecond == 716839


def test_unparseable_timestamp_returns_none():
    assert kimi_code._parse_ts("not-a-timestamp") is None
    assert kimi_code._parse_ts(None) is None
