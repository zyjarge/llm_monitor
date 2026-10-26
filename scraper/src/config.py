"""
Configuration loaded from .env via pydantic-settings.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ─── Providers ───────────────────────────────────────────────
    # Per-provider kill switch. Set to false to stop polling a provider
    # entirely: no fetch, no browser launch, no alert noise. Use this
    # when a provider's upstream breaks (e.g. opencode_go's dashboard
    # selectors went stale) or you temporarily don't care about it.
    MINIMAXI_ENABLED: bool = True
    OPENCODE_GO_ENABLED: bool = True
    KIMI_CODE_ENABLED: bool = True

    MINIMAX_API_KEY: str = ""
    MINIMAX_API_URL: str = "https://www.minimaxi.com/v1/api/openplatform/coding_plan/remains"

    OPENCODE_GO_WORKSPACE_ID: str = ""
    OPENCODE_GO_COOKIE_FILE: str = "cookie/opencode_cookie"

    KIMI_API_URL: str = "https://api.kimi.com/coding/v1/usages"
    KIMI_API_KEY: str = ""
    # Web session cookie (Netscape format) — supplies the `kimi-auth` JWT
    # used by the membership API, which reports the same monthly pool
    # percentage the Kimi console shows. Optional: without it the monthly
    # window falls back to the Code API's booster_wallet data.
    KIMI_COOKIE_FILE: str = "cookie/kimi_cookie"
    # The Kimi web JWT. Modern Kimi web stores it in localStorage under
    # `access_token` rather than in a `kimi-auth` cookie, so a Cookie-Editor
    # export no longer contains it. Either paste it here, or drop it in
    # KIMI_TOKEN_FILE as plain text (one line, no quotes).
    KIMI_AUTH_TOKEN: str = ""
    KIMI_TOKEN_FILE: str = "cookie/kimi_token"
    # The access token lives ~15 minutes; the refresh token from the same
    # login response is long-lived and is exchanged automatically.
    KIMI_REFRESH_TOKEN_FILE: str = "cookie/kimi_refresh_token"
    # Kimi membership gateway (Connect RPC). Rejects Code API keys — needs
    # the `kimi-auth` web session token.
    # The auth service that issues/refreshes web tokens.
    KIMI_AUTH_BASE_URL: str = "https://auth.kimi.com/api"
    KIMI_MEMBERSHIP_URL: str = (
        "https://www.kimi.com/apiv2/kimi.gateway.membership.v2."
        "MembershipService/GetSubscriptionStats"
    )

    # ─── Notifications ───────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    # ─── Runtime ──────────────────────────────────────────────────
    PUSHGATEWAY_URL: str = "http://pushgateway:9091"
    SCRAPE_INTERVAL: int = 300
    HISTORY_RETENTION_DAYS: int = 30

    ALERT_WARNING_THRESHOLD: int = 80
    ALERT_CRITICAL_THRESHOLD: int = 95
    COOKIE_FAILURE_TOLERANCE: int = 3

    # ─── Derived ──────────────────────────────────────────────────
    # Project root: parent of scraper/ package
    PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
    DATA_DIR: Path = PROJECT_ROOT / "data"
    LOG_DIR: Path = PROJECT_ROOT / "logs"
    HEARTBEAT_FILE: Path = Path("/tmp/scraper.heartbeat")

    def model_post_init(self, __context) -> None:
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.LOG_DIR.mkdir(parents=True, exist_ok=True)

    # ─── Helpers ──────────────────────────────────────────────────
    @property
    def sqlite_path(self) -> Path:
        return self.DATA_DIR / "history.sqlite"

    def cookie_path(self, name: str) -> Path:
        """Resolve a cookie file path to absolute. Falls back to PROJECT_ROOT/<rel>."""
        v = getattr(self, name)
        p = Path(v)
        if not p.is_absolute():
            p = self.PROJECT_ROOT / p
        return p


settings = Settings()
