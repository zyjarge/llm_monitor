"""
Tests for core data models.
"""
from datetime import datetime, timezone

import pytest

from scraper.src.models import WindowQuota, ProviderResult, EXPECTED_WINDOWS


def test_window_quota_percent():
    w = WindowQuota(window="5h", used=25, limit=100)
    assert w.percent == 25.0


def test_window_quota_percent_rounding():
    w = WindowQuota(window="weekly", used=1, limit=3)
    assert w.percent == 33.33


def test_window_quota_zero_limit_returns_zero():
    w = WindowQuota(window="5h", used=10, limit=0)
    assert w.percent == 0.0


def test_provider_result_get_window():
    result = ProviderResult(
        provider="test",
        fetched_at=datetime.now(timezone.utc),
        windows=(
            WindowQuota(window="5h", used=10, limit=100),
            WindowQuota(window="weekly", used=30, limit=100),
        ),
    )
    assert result.get("5h").percent == 10.0
    assert result.get("weekly").percent == 30.0
    assert result.get("monthly") is None


def test_provider_result_windows_dict():
    result = ProviderResult(
        provider="test",
        fetched_at=datetime.now(timezone.utc),
        windows=(
            WindowQuota(window="5h", used=10, limit=100),
            WindowQuota(window="weekly", used=30, limit=100),
        ),
    )
    d = result.windows_dict
    assert set(d.keys()) == {"5h", "weekly"}


def test_expected_windows_contract():
    """All providers must declare (5h, weekly, monthly).

    A provider may declare extra windows beyond that core set — kimi_code
    adds "booster" for the 加速包 spend, which is a separate quantity from
    the monthly balance.
    """
    for provider, windows in EXPECTED_WINDOWS.items():
        missing = {"5h", "weekly", "monthly"} - set(windows)
        assert not missing, f"{provider} missing windows: {missing}"


def test_kimi_code_declares_booster_separately_from_monthly():
    """The 加速包 spend must be its own window, never the monthly one."""
    windows = EXPECTED_WINDOWS["kimi_code"]
    assert "booster" in windows
    assert "monthly" in windows
    assert "booster" != "monthly"
