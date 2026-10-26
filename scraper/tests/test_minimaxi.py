"""
Tests for the MiniMax (minimaxi.com) provider using respx to mock httpx.
"""
import pytest
import respx
from httpx import Response

from scraper.src.providers import minimaxi
from scraper.src.config import settings


@pytest.fixture(autouse=True)
def configure_minimaxi(monkeypatch):
    monkeypatch.setattr(settings, "MINIMAX_API_KEY", "test-key-***")


@pytest.fixture
def sample_payload():
    """Realistic MiniMax /coding_plan/remains response (verified live)."""
    return {
        "base_resp": {"status_code": 0, "status_msg": "success"},
        "model_remains": [
            {
                "model_name": "general",
                "current_interval_total_count": 100,
                "current_interval_usage_count": 20,
                "current_interval_remaining_percent": 80,
                "current_weekly_total_count": 200,
                "current_weekly_usage_count": 30,
                "current_weekly_remaining_percent": 85,
                "start_time": 1784822400000,
                "end_time": 1784840400000,
                "remains_time": 12607927,
                "weekly_start_time": 1784476800000,
                "weekly_end_time": 1785081600000,
                "weekly_remains_time": 253807927,
            },
            {
                "model_name": "video",
                "current_interval_total_count": 50,
                "current_interval_usage_count": 5,
                "current_interval_remaining_percent": 90,
                "current_weekly_total_count": 100,
                "current_weekly_usage_count": 15,
                "current_weekly_remaining_percent": 85,
                "start_time": 1784822400000,
                "end_time": 1784840400000,
                "remains_time": 12607927,
                "weekly_start_time": 1784476800000,
                "weekly_end_time": 1785081600000,
                "weekly_remains_time": 253807927,
            },
        ],
    }


async def test_minimaxi_fetches_and_aggregates(sample_payload):
    with respx.mock(base_url="https://www.minimaxi.com") as respx_router:
        respx_router.get("/v1/api/openplatform/coding_plan/remains").mock(
            return_value=Response(200, json=sample_payload)
        )
        result = await minimaxi.fetch()

    assert result.success is True
    assert result.provider == "minimaxi"

    five_h = result.get("5h")
    weekly = result.get("weekly")
    monthly = result.get("monthly")

    assert five_h.used == 25.0           # 20 + 5
    assert five_h.limit == 150.0         # 100 + 50
    assert five_h.percent == 16.67       # 25/150 rounded

    assert weekly.used == 45.0           # 30 + 15
    assert weekly.limit == 300.0         # 200 + 100
    assert weekly.percent == 15.0

    # monthly mirrors the weekly pool for minimaxi (same used/limit).
    assert monthly.used == 45.0
    assert monthly.limit == 300.0
    assert monthly.percent == 15.0


async def test_minimaxi_handles_api_error_code():
    with respx.mock(base_url="https://www.minimaxi.com") as respx_router:
        respx_router.get("/v1/api/openplatform/coding_plan/remains").mock(
            return_value=Response(200, json={
                "base_resp": {"status_code": 1004, "status_msg": "token invalid"}
            })
        )
        result = await minimaxi.fetch()
    assert result.success is False
    assert "1004" in result.error or "token" in result.error.lower()


async def test_minimaxi_handles_http_error():
    with respx.mock(base_url="https://www.minimaxi.com") as respx_router:
        respx_router.get("/v1/api/openplatform/coding_plan/remains").mock(
            return_value=Response(401, json={"base_resp": {"status_code": 401}})
        )
        result = await minimaxi.fetch()
    assert result.success is False
    assert "401" in result.error


async def test_minimaxi_handles_empty_models(sample_payload):
    payload = {"base_resp": {"status_code": 0, "status_msg": "success"},
               "model_remains": []}
    with respx.mock(base_url="https://www.minimaxi.com") as respx_router:
        respx_router.get("/v1/api/openplatform/coding_plan/remains").mock(
            return_value=Response(200, json=payload)
        )
        result = await minimaxi.fetch()
    assert result.success is False
    assert "no models" in result.error


def test_minimaxi_is_configured(monkeypatch):
    monkeypatch.setattr(settings, "MINIMAX_API_KEY", "")
    assert minimaxi.is_configured() is False
    monkeypatch.setattr(settings, "MINIMAX_API_KEY", "valid")
    assert minimaxi.is_configured() is True
