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
    """Realistic MiniMax /coding_plan/remains response."""
    return {
        "code": 0,
        "data": {
            "plan": {"name": "Plus", "level": 1},
            "models": [
                {
                    "model": "MiniMax-Text-01",
                    "reset_at": 1900000000,
                    "end_at":   1900000000,
                    "five_hour_count": 20,
                    "five_hour_limit": 100,
                    "weekly_count": 30,
                    "weekly_limit": 200,
                },
                {
                    "model": "M-2.7",
                    "reset_at": 1900000000,
                    "end_at":   1900000000,
                    "five_hour_count": 5,
                    "five_hour_limit": 50,
                    "weekly_count": 15,
                    "weekly_limit": 100,
                },
            ],
        },
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

    # Monthly = weekly * 4.345
    assert monthly.used == pytest.approx(45.0 * 4.345)
    assert monthly.limit == pytest.approx(300.0 * 4.345)


async def test_minimaxi_handles_api_error_code():
    with respx.mock(base_url="https://www.minimaxi.com") as respx_router:
        respx_router.get("/v1/api/openplatform/coding_plan/remains").mock(
            return_value=Response(200, json={"code": 1004, "msg": "token invalid"})
        )
        result = await minimaxi.fetch()
    assert result.success is False
    assert "1004" in result.error or "token" in result.error.lower()


async def test_minimaxi_handles_http_error():
    with respx.mock(base_url="https://www.minimaxi.com") as respx_router:
        respx_router.get("/v1/api/openplatform/coding_plan/remains").mock(
            return_value=Response(401, json={"code": 401, "msg": "Unauthorized"})
        )
        result = await minimaxi.fetch()
    assert result.success is False
    assert "401" in result.error


async def test_minimaxi_handles_empty_models(sample_payload):
    payload = {"code": 0, "data": {"plan": {}, "models": []}}
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
