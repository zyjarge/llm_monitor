"""
Telegram alerting for cookie expiry & quota thresholds.

We track consecutive failure count per provider so we don't spam Telegram
on every transient hiccup. After COOKIE_FAILURE_TOLERANCE consecutive
failures, we send an alert and reset the counter. Alerts also include
quota critical/warning thresholds.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

import httpx
from loguru import logger

from scraper.src.config import settings


class TelegramAlerter:
    def __init__(self):
        self._fail_count: dict[str, int] = defaultdict(int)
        self._last_alert_at: dict[str, datetime] = {}

    def record_success(self, provider: str) -> None:
        if self._fail_count[provider] > 0:
            logger.info(f"{provider}: recovered after {self._fail_count[provider]} failures")
        self._fail_count[provider] = 0

    async def record_failure(self, provider: str, error: str) -> None:
        self._fail_count[provider] += 1
        count = self._fail_count[provider]
        logger.warning(f"{provider}: failure #{count}: {error}")

        if count == settings.COOKIE_FAILURE_TOLERANCE:
            await self._send(
                f"🚨 <b>{provider}</b> cookie/认证已失效（连续失败 {count} 次）\n\n"
                f"<b>最近错误</b>: <code>{_escape(error)[:500]}</code>\n\n"
                f"<b>修复步骤</b>:\n"
                f"1. 浏览器登录对应控制台\n"
                f"2. DevTools → Application → Cookies → 复制新 cookie\n"
                f"3. 更新 <code>~/workspace/llm_monitor/.env</code>\n"
                f"4. <code>docker compose restart scraper</code>"
            )

    async def check_quota_thresholds(self, results) -> None:
        for r in results:
            if not r.success:
                continue
            for w in r.windows:
                if w.percent >= settings.ALERT_CRITICAL_THRESHOLD:
                    await self._send(
                        f"🔥 <b>{r.provider} · {w.window}</b> 用量 <b>{w.percent:.1f}%</b>（critical ≥{settings.ALERT_CRITICAL_THRESHOLD}%）"
                    )
                elif w.percent >= settings.ALERT_WARNING_THRESHOLD:
                    await self._send(
                        f"⚠️ <b>{r.provider} · {w.window}</b> 用量 <b>{w.percent:.1f}%</b>（warning ≥{settings.ALERT_WARNING_THRESHOLD}%）"
                    )

    async def _send(self, text: str) -> None:
        token = settings.TELEGRAM_BOT_TOKEN
        chat = settings.TELEGRAM_CHAT_ID
        if not token or not chat:
            logger.debug("Telegram not configured; skip alert")
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": chat,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
                if resp.status_code >= 400:
                    logger.error(f"Telegram alert failed: {resp.status_code} {resp.text[:200]}")
                else:
                    logger.info("Telegram alert sent")
        except Exception as e:
            logger.error(f"Telegram send exception: {e}")


_alerter: TelegramAlerter | None = None


def get_alerter() -> TelegramAlerter:
    global _alerter
    if _alerter is None:
        _alerter = TelegramAlerter()
    return _alerter


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
