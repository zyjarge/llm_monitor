"""
Tests for metrics building, including failure paths and missing windows.
"""
from datetime import datetime, timezone

from prometheus_client import generate_latest

from scraper.src.metrics import build_metrics
from scraper.src.models import ProviderResult, WindowQuota


def test_metrics_builds_for_success_and_failure():
    success = ProviderResult(
        provider="minimaxi",
        fetched_at=datetime.now(timezone.utc),
        windows=(
            WindowQuota(window="5h", used=25, limit=100),
            WindowQuota(window="weekly", used=45, limit=200),
            WindowQuota(window="monthly", used=150, limit=800),
        ),
        success=True,
    )
    failure = ProviderResult(
        provider="kimi_code",
        fetched_at=datetime.now(timezone.utc),
        windows=(),
        success=False,
        error="cookie expired",
    )

    registry = build_metrics([success, failure])
    text = generate_latest(registry).decode()

    # success: status=1
    assert 'llm_scraper_status{provider="minimaxi"} 1.0' in text
    assert 'llm_quota_percent{provider="minimaxi",window="5h"} 25.0' in text
    assert 'llm_quota_percent{provider="minimaxi",window="weekly"} 22.5' in text
    assert 'llm_quota_percent{provider="minimaxi",window="monthly"} 18.75' in text

    # failure: status=0
    assert 'llm_scraper_status{provider="kimi_code"} 0.0' in text


def test_missing_windows_get_sentinel_value():
    """A provider that only returned weekly should still emit monthly=-1."""
    result = ProviderResult(
        provider="opencode_go",
        fetched_at=datetime.now(timezone.utc),
        windows=(WindowQuota(window="weekly", used=50, limit=100),),
        success=True,
    )
    registry = build_metrics([result])
    text = generate_latest(registry).decode()
    assert 'llm_quota_percent{provider="opencode_go",window="5h"} -1.0' in text
    assert 'llm_quota_percent{provider="opencode_go",window="monthly"} -1.0' in text


def test_reset_seconds_emitted_when_present():
    result = ProviderResult(
        provider="minimaxi",
        fetched_at=datetime.now(timezone.utc),
        windows=(
            WindowQuota(window="5h", used=10, limit=100, reset_in_seconds=4200),
            WindowQuota(window="weekly", used=20, limit=200, reset_in_seconds=None),
        ),
        success=True,
    )
    registry = build_metrics([result])
    text = generate_latest(registry).decode()
    # 5h has reset
    assert 'llm_quota_reset_seconds{provider="minimaxi",window="5h"} 4200.0' in text
    # weekly doesn't → no metric line at all
    assert 'llm_quota_reset_seconds{provider="minimaxi",window="weekly"}' not in text
