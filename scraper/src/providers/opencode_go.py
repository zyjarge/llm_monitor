"""
OpenCode Go quota provider — dashboard scraping with cookie auth.

URL pattern: https://opencode.ai/workspace/{workspace_id}/go

Auth: workspace ID + browser cookies loaded from `cookie/opencode_cookie.txt`.
The cookie file is a raw `Cookie:` header (e.g. "auth=Fe26.2...; oc_locale=zh").
We extract the 'auth' cookie specifically (others like oc_locale / cf_clearance /
analytics are also injected so the server-side fingerprint looks consistent).

The dashboard renders three cards: Rolling (5h), Weekly, Monthly.
⚠️ Dashboard DOM is owned by OpenCode and may change — see SELECTORS below.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from loguru import logger
from playwright.async_api import Browser

from scraper.src.config import settings
from scraper.src.models import ProviderResult, WindowQuota
from scraper.src.providers._base import (
    browser_session,
    cookies_for_playwright,
    now_utc,
    read_cookie_file,
)

# ─── Selectors (verified live 2026-07-23) ────────────────────────────
# OpenCode's quota cards are wrapped in <div class='_root_xxx'> siblings
# inside a <section>. Each card contains a <span> label like "滚动用量"
# and a sibling <span> with the percentage like "0%".
# Strategy: find the label span, walk up to the card container
# (2 levels up — grand-parent of the span), then read the full card text.
SELECTORS = {
    "label_span":  "span:has-text('{}')",
    "card_xpath":  "xpath=ancestor::div[1]/ancestor::div[1]",
    "percent":     "text=/^\\d+(?:\\.\\d+)?%$/",
}

# Window labels to try (Chinese — verified live 2026-07-23).
# Card text format: "<label> <pct>%<reset info>" e.g. "滚动用量 0% 重置于 5 小时 0 分钟".
# To detect a card, we look for the label substring; the % number lives on the
# same line. Don't add English variants here — opencode's UI is i18n'd and the
# text is server-rendered based on Accept-Language.
WINDOW_LABELS = {
    "5h":      "滚动用量",
    "weekly":  "每周用量",
    "monthly": "每月用量",
}

# Cookie name(s) that prove the user is authenticated. If none present,
# we assume the cookie file is stale and report not-configured.
REQUIRED_COOKIE_NAMES = ("auth",)


def is_configured() -> bool:
    if not settings.OPENCODE_GO_WORKSPACE_ID:
        return False
    cookies = read_cookie_file(settings.cookie_path("OPENCODE_GO_COOKIE_FILE"))
    return any(name in cookies for name in REQUIRED_COOKIE_NAMES)


async def fetch() -> ProviderResult:
    if not settings.OPENCODE_GO_WORKSPACE_ID:
        return ProviderResult(
            provider="opencode_go",
            fetched_at=now_utc(),
            windows=(),
            success=False,
            error="OPENCODE_GO_WORKSPACE_ID not set in .env",
        )

    cookies = read_cookie_file(settings.cookie_path("OPENCODE_GO_COOKIE_FILE"))
    if not any(name in cookies for name in REQUIRED_COOKIE_NAMES):
        return ProviderResult(
            provider="opencode_go",
            fetched_at=now_utc(),
            windows=(),
            success=False,
            error=(
                f"cookie file {settings.OPENCODE_GO_COOKIE_FILE} missing or has no "
                f"{'/'.join(REQUIRED_COOKIE_NAMES)} cookie — please refresh"
            ),
        )

    url = f"https://opencode.ai/workspace/{settings.OPENCODE_GO_WORKSPACE_ID}/go"

    try:
        async with browser_session() as ctx:
            await ctx.add_cookies(
                cookies_for_playwright(cookies, host="opencode.ai")
            )
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
                await page.wait_for_timeout(2000)  # SPA hydrate

                windows: list[WindowQuota] = []
                # Reset-time text is rendered in <span data-slot="reset-time">
                # in the same order as the percentage cards (5h, weekly, monthly).
                reset_texts = await page.locator('span[data-slot="reset-time"]').all_inner_texts()

                for win_idx, (win_name, label) in enumerate(WINDOW_LABELS.items()):
                    pct = await _parse_card(page, label)
                    if pct is None:
                        continue
                    # Parse reset-time text if available for this window
                    reset_secs = None
                    if win_idx < len(reset_texts):
                        reset_secs = _parse_chinese_duration(reset_texts[win_idx])
                    windows.append(
                        WindowQuota(
                            window=win_name,
                            used=pct,
                            limit=100,
                            reset_in_seconds=reset_secs,
                        )
                    )
            finally:
                # Close the page explicitly so chromium helper PIDs are
                # released immediately rather than waiting for ctx.close()
                # in the outer async-with. Defense in depth — the outer
                # ctx.close() still runs even if this raises.
                try:
                    await page.close()
                except Exception:
                    pass

            if not windows:
                return ProviderResult(
                    provider="opencode_go",
                    fetched_at=now_utc(),
                    windows=(),
                    success=False,
                    error=(
                        f"dashboard rendered but no quota cards found at {url}; "
                        "SELECTORS may be stale"
                    ),
                )

            return ProviderResult(
                provider="opencode_go",
                fetched_at=now_utc(),
                windows=tuple(windows),
                success=True,
            )

    except Exception as e:
        logger.exception("opencode_go fetch failed")
        return ProviderResult(
            provider="opencode_go",
            fetched_at=now_utc(),
            windows=(),
            success=False,
            error=str(e),
        )


async def _parse_card(page, label: str) -> float | None:
    """Return the percentage for a given card label, or None if not found.

    The label lives in a <span> like <span>滚动用量</span>. Walk up 2 divs
    to the card container (verified DOM: label span → wrapper div → card div),
    then read all inner text and extract the first `N%` pattern.
    """
    loc = page.locator(SELECTORS["label_span"].format(label)).first
    if await loc.count() == 0:
        return None
    card = loc.locator(SELECTORS["card_xpath"])
    if await card.count() == 0:
        return None
    text = await card.inner_text()
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    return float(m.group(1)) if m else None


# ─── Chinese duration parsing ───────────────────────────────────────
# The opencode dashboard renders reset times in Chinese:
#   "重置于 3 小时 29 分钟"  (5h window)
#   "重置于 2 天 21 小时"      (weekly)
#   "重置于 20 天 5 小时"      (monthly)
_CN_UNITS = {
    "天":   86400,
    "小时": 3600,
    "分钟": 60,
    "秒":   1,
}
_CN_DURATION_RE = re.compile(r"(\d+)\s*(天|小时|分钟|秒)")


def _parse_chinese_duration(text: str) -> int | None:
    """Convert '重置于 2 天 21 小时' to total seconds. Returns None
    if no Chinese duration tokens are found.
    """
    if not text:
        return None
    total = 0
    matched = False
    for value, unit in _CN_DURATION_RE.findall(text):
        total += int(value) * _CN_UNITS[unit]
        matched = True
    return total if matched else None
