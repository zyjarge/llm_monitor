"""
Tests for the provider kill switch and the tick loop's skip behaviour.

A provider whose *_ENABLED flag is off must be skipped entirely: no
fetch, no browser launch, no Telegram failure alerts.
"""
from datetime import datetime, timezone

from scraper.src import main as main_mod
from scraper.src.config import settings
from scraper.src.models import ProviderResult


class FakeScheduler:
    """tick() only passes this through; nothing else touches it."""


def _enabled_flags():
    return {name: enabled for name, enabled, _, _ in main_mod.PROVIDERS}


async def test_disabled_provider_is_skipped_without_fetching(monkeypatch):
    """A disabled provider must not be fetched at all."""
    called = []

    async def fake_fetch():
        called.append("fetched")
        return ProviderResult(
            provider="opencode_go",
            fetched_at=datetime.now(timezone.utc),
            windows=(),
            success=False,
            error="should not happen",
        )

    # Replace the registry with one where opencode_go is disabled and its
    # fetch would blow up if it were called.
    monkeypatch.setattr(settings, "OPENCODE_GO_ENABLED", False)
    monkeypatch.setattr(
        main_mod,
        "PROVIDERS",
        [
            ("opencode_go", main_mod._enabled_opencode_go,
             lambda: True, fake_fetch),
        ],
    )

    results = await main_mod.tick(FakeScheduler())

    assert called == [], "disabled provider must not be fetched"
    assert len(results) == 1
    assert results[0].provider == "opencode_go"
    assert results[0].success is False
    assert "disabled" in (results[0].error or "")


async def test_disabled_provider_does_not_alert(monkeypatch):
    """Skipping a disabled provider must not fire a Telegram failure."""
    sent = []

    class FakeAlerter:
        def record_success(self, provider):
            sent.append(("success", provider))

        async def record_failure(self, provider, error):
            sent.append(("failure", provider))

    monkeypatch.setattr(settings, "OPENCODE_GO_ENABLED", False)
    monkeypatch.setattr(main_mod, "get_alerter", lambda: FakeAlerter())
    monkeypatch.setattr(
        main_mod,
        "PROVIDERS",
        [
            ("opencode_go", main_mod._enabled_opencode_go,
             lambda: True, None),
        ],
    )

    await main_mod.tick(FakeScheduler())

    assert sent == [], f"disabled provider must not alert, got {sent}"


async def test_enabled_provider_is_fetched(monkeypatch):
    """The switch defaults to on — an enabled provider still runs."""
    called = []

    async def fake_fetch():
        called.append("fetched")
        return ProviderResult(
            provider="kimi_code",
            fetched_at=datetime.now(timezone.utc),
            windows=(),
            success=True,
        )

    class FakeAlerter:
        def record_success(self, provider):
            pass

        async def record_failure(self, provider, error):
            pass

    monkeypatch.setattr(settings, "KIMI_CODE_ENABLED", True)
    monkeypatch.setattr(main_mod, "get_alerter", lambda: FakeAlerter())
    monkeypatch.setattr(
        main_mod,
        "PROVIDERS",
        [
            ("kimi_code", main_mod._enabled_kimi_code, lambda: True, fake_fetch),
        ],
    )

    results = await main_mod.tick(FakeScheduler())

    assert called == ["fetched"]
    assert results[0].success is True


def test_enable_flags_default_to_true():
    """All three providers ship enabled by default.

    Assert the *declared* default, not the live value: the local .env may
    legitimately turn one off.
    """
    from scraper.src.config import Settings

    defaults = {
        name: field.default
        for name, field in Settings.model_fields.items()
        if name.endswith("_ENABLED")
    }
    assert defaults == {
        "MINIMAXI_ENABLED": True,
        "OPENCODE_GO_ENABLED": True,
        "KIMI_CODE_ENABLED": True,
    }
    assert set(_enabled_flags()) == {"minimaxi", "opencode_go", "kimi_code"}


def test_enabled_flag_reads_from_config(monkeypatch):
    """Flipping the config value flips the switch (no code change needed)."""
    monkeypatch.setattr(settings, "OPENCODE_GO_ENABLED", False)
    assert main_mod._enabled_opencode_go() is False
    monkeypatch.setattr(settings, "OPENCODE_GO_ENABLED", True)
    assert main_mod._enabled_opencode_go() is True
