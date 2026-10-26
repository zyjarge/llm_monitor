"""
Shared data models for provider output.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class WindowQuota:
    """A single quota window (e.g. 5h, weekly, monthly)."""
    window: str                       # "5h" | "weekly" | "monthly"
    used: float                       # absolute usage (dollars or tokens depending on provider)
    limit: float                      # absolute limit for the window
    reset_in_seconds: int | None = None  # seconds until window resets

    @property
    def percent(self) -> float:
        if self.limit <= 0:
            return 0.0
        return round(self.used / self.limit * 100, 2)


@dataclass(frozen=True)
class ProviderResult:
    """Result of a single provider scrape."""
    provider: str                                    # "minimaxi" | "opencode_go" | "kimi_code"
    fetched_at: datetime
    windows: tuple[WindowQuota, ...]                 # 1..N windows
    success: bool = True
    error: str | None = None
    # Which upstream each window came from, when a provider has more than
    # one candidate source (kimi_code: membership API / Code API pools /
    # booster wallet). Keyed by window name, because different windows can
    # come from different upstreams — kimi_code's monthly balance and its
    # booster spend are separate quantities from separate endpoints.
    # Surfaced as a metric so the dashboard can explain a number that
    # differs from the vendor console.
    sources: dict[str, str] = field(default_factory=dict)

    def get(self, window: str) -> WindowQuota | None:
        for w in self.windows:
            if w.window == window:
                return w
        return None

    @property
    def windows_dict(self) -> dict[str, WindowQuota]:
        return {w.window: w for w in self.windows}


# Provider -> expected windows (used as contract)
EXPECTED_WINDOWS: dict[str, tuple[str, ...]] = {
    "minimaxi":     ("5h", "weekly", "monthly"),
    "opencode_go":  ("5h", "weekly", "monthly"),
    # "monthly" is the 月度余额 (monthly balance) and needs the membership
    # API; "booster" is the 加速包 spend — a different quantity, reported
    # separately so the two are never confused.
    "kimi_code":    ("5h", "weekly", "monthly", "booster"),
}
