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
    """All providers should declare (5h, weekly, monthly)."""
    for provider, windows in EXPECTED_WINDOWS.items():
        assert set(windows) == {"5h", "weekly", "monthly"}, f"{provider} missing windows"
