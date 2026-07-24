"""
Tests for provider configuration detection (no network, no Playwright).

opencode_go: cookie-file based (Netscape format) — needs cookie file
              containing `auth` cookie name + OPENCODE_GO_WORKSPACE_ID.
kimi_code:   API-key based — needs KIMI_API_KEY to be set.

Both providers also have live data tests (kimi_code only — opencode_go
requires Playwright, exercised in the live integration test).
"""
import pytest

from scraper.src.config import settings
from scraper.src.providers import opencode_go, kimi_code


# ─── opencode_go (cookie) ──────────────────────────────────────────

@pytest.fixture
def cookie_dir(tmp_path, monkeypatch):
    """Point opencode's cookie file at a temp path."""
    monkeypatch.setattr(settings, "OPENCODE_GO_COOKIE_FILE", str(tmp_path / "opencode.txt"))
    return tmp_path


def test_opencode_not_configured_when_workspace_id_empty(monkeypatch, cookie_dir):
    monkeypatch.setattr(settings, "OPENCODE_GO_WORKSPACE_ID", "")
    (cookie_dir / "opencode.txt").write_text("auth=valid")
    assert opencode_go.is_configured() is False


def test_opencode_not_configured_when_file_missing(cookie_dir, monkeypatch):
    monkeypatch.setattr(settings, "OPENCODE_GO_WORKSPACE_ID", "ws-123")
    # no file written
    assert opencode_go.is_configured() is False


def test_opencode_not_configured_when_auth_cookie_missing(cookie_dir, monkeypatch):
    monkeypatch.setattr(settings, "OPENCODE_GO_WORKSPACE_ID", "ws-123")
    (cookie_dir / "opencode.txt").write_text(
        "opencode.ai\tFALSE\t/\tFALSE\t0\ttheme\tlight\n"
        "opencode.ai\tFALSE\t/\tFALSE\t0\toc_locale\tzh\n"
    )
    assert opencode_go.is_configured() is False


def test_opencode_configured_when_auth_cookie_present(cookie_dir, monkeypatch):
    monkeypatch.setattr(settings, "OPENCODE_GO_WORKSPACE_ID", "ws-123")
    (cookie_dir / "opencode.txt").write_text(
        "#HttpOnly_opencode.ai\tFALSE\t/\tTRUE\t1800000000\tauth\tFe26.2**token**\n"
        "opencode.ai\tFALSE\t/\tFALSE\t0\toc_locale\tzh\n"
    )
    assert opencode_go.is_configured() is True


# ─── kimi_code (API key) ───────────────────────────────────────────

def test_kimi_not_configured_when_api_key_empty(monkeypatch):
    monkeypatch.setattr(settings, "KIMI_API_KEY", "")
    assert kimi_code.is_configured() is False


def test_kimi_configured_when_api_key_present(monkeypatch):
    monkeypatch.setattr(settings, "KIMI_API_KEY", "sk-kimi-test-key")
    assert kimi_code.is_configured() is True


def test_kimi_fetch_returns_config_error_when_key_missing(monkeypatch):
    import asyncio
    monkeypatch.setattr(settings, "KIMI_API_KEY", "")
    result = asyncio.run(kimi_code.fetch())
    assert result.success is False
    assert "KIMI_API_KEY" in result.error