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

        # Fire the "cookie expired" alert the first time the counter reaches
        # the tolerance. `==` is deliberate here, not `>=`: without a
        # re-alert path, `>=` would spam Telegram every single tick. The
        # `elif` below re-alerts on a slow cadence instead, so a provider
        # that stays broken for weeks (opencode_go failed 16k+ times) is
        # not permanently silent.
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
        elif count > settings.COOKIE_FAILURE_TOLERANCE:
            # Re-alert on a slow cadence so a long outage isn't silent.
            if count % 100 == 0:
                await self._send(
                    f"⚠️ <b>{provider}</b> 仍然连续失败 <b>{count}</b> 次\n\n"
                    f"<b>最近错误</b>: <code>{_escape(error)[:300]}</code>\n\n"
                    f"如果已经不需要这个 provider，可以在 <code>.env</code> 里设置 "
                    f"<code>{provider.upper()}_ENABLED=false</code> 停掉它。"
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
