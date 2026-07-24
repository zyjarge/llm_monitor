"""
Shared data models for provider output.
"""
from __future__ import annotations

from dataclasses import dataclass
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
    "kimi_code":    ("5h", "weekly", "monthly"),  # monthly derived from weekly (see kimi_code.py)
}
