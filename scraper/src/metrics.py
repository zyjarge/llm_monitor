"""
Prometheus metric building & push.

We build a fresh CollectorRegistry on each scrape to avoid stale labels
(Prometheus pushgateway doesn't drop old time series automatically).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from loguru import logger
from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

from scraper.src.config import settings
from scraper.src.models import EXPECTED_WINDOWS, ProviderResult


def build_metrics(results: Iterable[ProviderResult]) -> CollectorRegistry:
    """Build a fresh registry of gauges from provider results."""
    registry = CollectorRegistry()

    g_pct = Gauge(
        "llm_quota_percent",
        "Current quota usage percentage (0-100).",
        ["provider", "window"],
        registry=registry,
    )
    g_used = Gauge(
        "llm_quota_used",
        "Quota used (provider-defined units, e.g. dollars or tokens).",
        ["provider", "window"],
        registry=registry,
    )
    g_limit = Gauge(
        "llm_quota_limit",
        "Quota limit (provider-defined units).",
        ["provider", "window"],
        registry=registry,
    )
    g_reset = Gauge(
        "llm_quota_reset_seconds",
        "Seconds until quota window resets.",
        ["provider", "window"],
        registry=registry,
    )
    g_status = Gauge(
        "llm_scraper_status",
        "Scrape status: 1=ok, 0=error.",
        ["provider"],
        registry=registry,
    )
    g_scrape_age = Gauge(
        "llm_scrape_age_seconds",
        "Seconds since last successful scrape.",
        ["provider"],
        registry=registry,
    )

    now = datetime.now(timezone.utc)

    for result in results:
        if result.success:
            g_status.labels(provider=result.provider).set(1)
            age = (now - result.fetched_at).total_seconds()
            g_scrape_age.labels(provider=result.provider).set(max(0.0, age))

            for window, quota in result.windows_dict.items():
                g_pct.labels(provider=result.provider, window=window).set(quota.percent)
                g_used.labels(provider=result.provider, window=window).set(quota.used)
                g_limit.labels(provider=result.provider, window=window).set(quota.limit)
                if quota.reset_in_seconds is not None:
                    g_reset.labels(
                        provider=result.provider, window=window
                    ).set(quota.reset_in_seconds)

            # Emit -1 for missing expected windows so PromQL "absent()" still
            # detects them (vs leaving them empty which looks like scrape never ran)
            expected = EXPECTED_WINDOWS.get(result.provider, ())
            actual = set(result.windows_dict.keys())
            for missing in set(expected) - actual:
                g_pct.labels(provider=result.provider, window=missing).set(-1)
        else:
            g_status.labels(provider=result.provider).set(0)
            g_scrape_age.labels(provider=result.provider).set(0)

    return registry


def push_metrics(results: Iterable[ProviderResult]) -> None:
    """Build metrics from results and push to Pushgateway."""
    registry = build_metrics(results)
    job_name = "llm_monitor"

    try:
        push_to_gateway(
            gateway=f"{_gateway_host()}:{_gateway_port()}",
            job=job_name,
            registry=registry,
        )
        logger.info(f"Pushed metrics to pushgateway (job={job_name})")
    except Exception as e:
        logger.error(f"Failed to push metrics: {e}")


def _gateway_host() -> str:
    url = settings.PUSHGATEWAY_URL
    # strip scheme
    no_scheme = url.split("://", 1)[-1]
    host_port = no_scheme.split("/", 1)[0]
    return host_port.split(":", 1)[0]


def _gateway_port() -> int:
    url = settings.PUSHGATEWAY_URL
    no_scheme = url.split("://", 1)[-1]
    host_port = no_scheme.split("/", 1)[0]
    return int(host_port.split(":", 1)[1])
