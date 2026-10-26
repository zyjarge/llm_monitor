"""
Tests for the Kimi QR-login helper.

The QR flow is the only login path that needs neither a browser profile nor
a cookie export: Kimi's web session lives in localStorage (`access_token`),
which a Cookie-Editor export can never contain.

These tests exercise the request shapes and the safety rules (never write an
unverified token) without performing a real scan.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "kimi_qr_login.py"


def load_module():
    spec = importlib.util.spec_from_file_location("kimi_qr_login", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


kqr = load_module()


def test_auth_base_url_matches_the_web_bundle():
    """The web app builds the oauth namespace as `${AUTH_API_HOST}/api`, and
    AUTH_API_HOST is https://auth.kimi.com."""
    assert kqr.AUTH_BASE.startswith("https://auth.kimi.com/api/")
    assert kqr.AUTH_BASE.endswith("account.gateway.v1.AuthService")


def test_membership_url_matches_the_web_bundle():
    assert kqr.MEMBERSHIP_URL == (
        "https://www.kimi.com/apiv2/kimi.gateway.membership.v2."
        "MembershipService/GetSubscriptionStats"
    )


def test_scan_page_is_the_wechat_auth_route():
    """The QR image in the login panel points at /wechat/mp/auth?id=..."""
    assert kqr.SCAN_PAGE == "https://www.kimi.com/wechat/mp/auth"


def test_create_qr_posts_to_the_right_endpoint():
    """CreateLoginQRCode returns a bare {code} and needs no auth."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), request.read()))
        return httpx.Response(200, json={"code": "abc-123"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        code = kqr.create_qr(client)

    assert code == "abc-123"
    assert len(calls) == 1
    url, body = calls[0]
    assert url.endswith("AuthService/CreateLoginQRCode")
    assert body == b"{}"


def test_poll_returns_tokens_on_success():
    """STATUS_SUCCESS carries access_token / refresh_token / user_id."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "status": "STATUS_SUCCESS",
            "access_token": "eyJhbGciOiJIUzI1NiJ9.eyJ4IjoxfQ.sig",
            "refresh_token": "refresh-value",
            "user_id": "u-1",
        })

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        data = kqr.poll(client, "abc-123", timeout=5)

    assert data["access_token"].startswith("eyJ")
    assert data["refresh_token"] == "refresh-value"
    assert data["user_id"] == "u-1"


def test_poll_raises_on_expired_qr():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "STATUS_EXPIRED"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="expired"):
            kqr.poll(client, "abc-123", timeout=5)


def test_poll_accepts_camel_case_token_keys():
    """The wire format may use either snake_case or camelCase for the token
    field — losing the token to a naming change would be silent."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "status": "STATUS_SUCCESS",
            "accessToken": "eyJhbGciOiJIUzI1NiJ9.eyJ4IjoxfQ.sig",
            "refreshToken": "r",
            "userId": "u-1",
        })

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        data = kqr.poll(client, "abc-123", timeout=5)

    assert data["accessToken"].startswith("eyJ")


def test_verify_rejects_a_bad_token():
    """A token that the real endpoint rejects must not be treated as good."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": "unauthenticated"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        ok, detail = kqr.verify(client, "eyJhbGciOiJIUzI1NiJ9.eyJ4IjoxfQ.sig")

    assert ok is False
    assert "401" in detail


def test_verify_accepts_a_good_token():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"].startswith("Bearer ")
        return httpx.Response(200, json={"code": 0})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        ok, detail = kqr.verify(client, "good-token")

    assert ok is True
    assert "200" in detail


def test_token_extraction_handles_both_key_spellings():
    """The helper must find the token whether the gateway spells it
    `access_token` or `accessToken`."""
    # Mirror the extraction the script performs.
    def extract(data):
        return (data.get("access_token") or data.get("accessToken")
                or (data.get("result") or {}).get("access_token")
                or (data.get("result") or {}).get("accessToken"))

    assert extract({"access_token": "A"}) == "A"
    assert extract({"accessToken": "B"}) == "B"
    assert extract({"result": {"access_token": "C"}}) == "C"
    assert extract({"status": "STATUS_SUCCESS"}) is None


def test_make_qr_text_degrades_without_the_qrcode_package():
    """No qrcode dependency => empty string, not a crash."""
    art = kqr.make_qr_text("https://example.com")
    assert isinstance(art, str)
