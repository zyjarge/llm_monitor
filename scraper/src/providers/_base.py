"""
Shared Playwright helpers for cookie-based scraping.

Cookies are stored as Netscape-format files (the format exported by Chrome
extensions like Cookie-Editor and supported by curl/browser DevTools).

Format reference: http://curl.haxx.se/rfc/cookie_spec.html
Each non-comment line is TAB-delimited with 7 columns:
    domain  include_subdomains  path  secure  expires  name  value
- Lines beginning with '#' are comments (and '#HttpOnly_' is a marker,
  NOT a comment — strip that prefix and treat the rest as a normal line).

Helpers:
  - read_cookie_file(path)         → dict[name,value]
  - cookies_for_playwright(d, host)→ list of Playwright cookie dicts
  - browser_session()              → async Chromium session
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from loguru import logger
from playwright.async_api import Browser, async_playwright

# Persistent profile so cookies survive container restarts
PROFILE_DIR = Path("/tmp/llm-monitor-playwright")


# ─── Cookie parsing (Netscape format only) ───────────────────────────
def cookie_dict_from_netscape(text: str) -> dict[str, str]:
    """Parse Netscape-format cookie file text into {name: value} dict.

    Lines beginning with '#' are treated as comments EXCEPT '#HttpOnly_<rest>'
    which has the '#HttpOnly_' prefix stripped and is parsed as a normal cookie.
    Each remaining line must be TAB-delimited with 7 columns:
        domain  include_subdomains  path  secure  expires  name  value

    Names are case-preserved; duplicates keep the LAST occurrence.
    Invalid lines are skipped silently. Empty / blank input → {}.
    """
    out: dict[str, str] = {}
    if not text:
        return out
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line.strip():
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            # tolerate the rare comma-separated variant from some exporters
            parts = line.split(",")
            if len(parts) < 7:
                logger.debug(f"skipping malformed cookie line: {raw_line!r}")
                continue
        # columns: domain, include_subdomains, path, secure, expires, name, value
        name = parts[5].strip()
        value = parts[6].strip()
        if name:
            out[name] = value
    return out


def cookies_for_playwright(
    cookies: dict[str, str], host: str
) -> list[dict]:
    """Convert parsed cookies to Playwright's expected format.

    `host` is the target site (e.g. 'kimi.com' / 'opencode.ai'). Per-cookie
    domain from the file overrides this default so cross-domain cookies
    (like .kimi.com subdomains) attach correctly.
    """
    out: list[dict] = []
    for name, value in cookies.items():
        out.append({
            "name": name,
            "value": value,
            # Use leading-dot for cross-domain cookies; browser handles both
            "domain": host,
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "Lax",
        })
    return out


def read_cookie_file(path: Path | str) -> dict[str, str]:
    """Read a Netscape-format cookie file. Returns {} if file missing."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        logger.warning(f"read_cookie_file({p}): {e}")
        return {}
    return cookie_dict_from_netscape(text)


# ─── Playwright session ──────────────────────────────────────────────
@asynccontextmanager
async def browser_session(headless: bool = True) -> AsyncIterator[Browser]:
    """Yields a Chromium browser with a persistent profile."""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            yield browser
        finally:
            await browser.close()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
