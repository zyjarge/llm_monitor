"""
Tests for the Telegram alerter — failure counting + threshold detection.
Uses respx to mock the Telegram HTTP API.
"""
import pytest
import respx
from httpx import Response

from scraper.src.alerts import TelegramAlerter, get_alerter
from scraper.src.config import settings
from scraper.src.models import ProviderResult, WindowQuota
from datetime import datetime, timezone


@pytest.fixture(autouse=True)
def configure_telegram(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "test-bot")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(settings, "COOKIE_FAILURE_TOLERANCE", 3)
    monkeypatch.setattr(settings, "ALERT_WARNING_THRESHOLD", 80)
    monkeypatch.setattr(settings, "ALERT_CRITICAL_THRESHOLD", 95)


async def test_failure_triggers_alert_after_tolerance():
    alerter = TelegramAlerter()
    sent = []
    async def fake_send(text):
        sent.append(text)
    alerter._send = fake_send

    # 2 failures: no alert yet
    await alerter.record_failure("minimaxi", "timeout")
    await alerter.record_failure("minimaxi", "timeout")
    assert sent == []

    # 3rd failure: ALERT
    await alerter.record_failure("minimaxi", "timeout")
    assert len(sent) == 1
    assert "minimaxi" in sent[0]
    assert "连续失败" in sent[0]


async def test_success_resets_failure_counter():
    alerter = TelegramAlerter()
    sent = []

    async def fake_send(text):
        sent.append(text)

    alerter._send = fake_send

    await alerter.record_failure("kimi_code", "err")
    await alerter.record_failure("kimi_code", "err")
    alerter.record_success("kimi_code")
    await alerter.record_failure("kimi_code", "err")
    await alerter.record_failure("kimi_code", "err")
    # counter was reset → 3rd of NEW batch triggers
    await alerter.record_failure("kimi_code", "err")
    assert len(sent) == 1


async def test_quota_threshold_alerts():
    alerter = TelegramAlerter()
    sent = []

    async def fake_send(text):
        sent.append(text)

    alerter._send = fake_send

    results = [
        ProviderResult(
            provider="minimaxi",
            fetched_at=datetime.now(timezone.utc),
            windows=(
                WindowQuota(window="5h", used=85, limit=100),   # 85% warning
                WindowQuota(window="weekly", used=97, limit=100), # 97% critical
                WindowQuota(window="monthly", used=50, limit=100),
            ),
            success=True,
        ),
    ]

    await alerter.check_quota_thresholds(results)

    criticals = [s for s in sent if "🔥" in s]
    warnings = [s for s in sent if "⚠️" in s]
    assert len(criticals) == 1
    assert len(warnings) == 1
    assert "minimaxi" in criticals[0]
    assert "weekly" in criticals[0]


async def test_no_alert_below_threshold():
    alerter = TelegramAlerter()
    sent = []

    async def fake_send(text):
        sent.append(text)

    alerter._send = fake_send

    results = [
        ProviderResult(
            provider="minimaxi",
            fetched_at=datetime.now(timezone.utc),
            windows=(WindowQuota(window="5h", used=50, limit=100),),
            success=True,
        ),
    ]
    await alerter.check_quota_thresholds(results)
    assert sent == []
