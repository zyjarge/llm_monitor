"""
Configuration loaded from .env via pydantic-settings.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ─── Providers ───────────────────────────────────────────────
    MINIMAX_API_KEY: str = ""
    MINIMAX_API_URL: str = "https://www.minimaxi.com/v1/api/openplatform/coding_plan/remains"

    OPENCODE_GO_WORKSPACE_ID: str = ""
    OPENCODE_GO_COOKIE_FILE: str = "cookie/opencode_cookie"

    KIMI_API_URL: str = "https://api.kimi.com/coding/v1/usages"
    KIMI_API_KEY: str = ""

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
