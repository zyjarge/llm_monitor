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
  - get_browser()                  → module-level singleton Browser
  - browser_session()              → async context mgr yielding a fresh
                                      BrowserContext (browser is reused)
  - shutdown_browser()             → close the singleton (call on exit)
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from loguru import logger
from playwright.async_api import Browser, BrowserContext, async_playwright

# Persistent profile so cookies survive container restarts
PROFILE_DIR = Path("/tmp/llm-monitor-playwright")

# Module-level singleton: one Chromium process per scraper run.
# Rationale: launching a new browser per fetch leaks chromium helper
# processes (~30 PIDs each); with 5-min ticks that fills the container's
# PID namespace in days. Sharing one Browser across fetches is safe —
# each fetch creates its own BrowserContext, which we close on exit.
_pw = None
_browser: Browser | None = None


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


# ─── Playwright singleton lifecycle ──────────────────────────────────
async def get_browser(headless: bool = True) -> Browser:
    """Return the module-level Browser, launching it on first call.

    Idempotent: safe to call from every fetch — only the first call
    pays the launch cost; subsequent calls return the cached instance.
    """
    global _pw, _browser
    if _browser is not None and _browser.is_connected():
        return _browser
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(
        headless=headless,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    logger.info("playwright singleton browser launched")
    return _browser


async def shutdown_browser() -> None:
    """Close the singleton browser + playwright driver (call on exit)."""
    global _pw, _browser
    if _browser is not None:
        try:
            await _browser.close()
        except Exception as e:
            logger.warning(f"error closing browser: {e}")
        _browser = None
    if _pw is not None:
        try:
            await _pw.stop()
        except Exception as e:
            logger.warning(f"error stopping playwright driver: {e}")
        _pw = None


@asynccontextmanager
async def browser_session(headless: bool = True) -> AsyncIterator[BrowserContext]:
    """Yield a fresh BrowserContext on the singleton Browser.

    The context (and any page the caller opens on it) is closed on exit,
    so even an uncaught exception in the caller's `async with` block will
    not leak Chromium helper processes. The Browser itself stays alive
    for the lifetime of the scraper process.
    """
    browser = await get_browser(headless=headless)
    ctx = await browser.new_context()
    try:
        yield ctx
    finally:
        await ctx.close()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
