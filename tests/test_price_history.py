"""Historical chart data keeps the vendor observation date and fails closed on gaps."""

from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

import backend.app.main as main_module
from backend.app.data_provider.iwencai import IwencaiSkillHubProvider, _history_facts
from backend.app.models import FactRecord


@pytest.mark.asyncio
async def test_history_provider_extracts_dated_stock_prices_from_live_shape() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": [{
            "股票代码": "600519.SH", "股票简称": "贵州茅台",
            "收盘价[20260921]": 1252.57, "收盘价[20260922]": 1253.80,
            "最新涨跌幅": 1.2, "收盘价[20260923]": "--",
        }]})

    provider = IwencaiSkillHubProvider(
        "test-secret", base_url="https://iwencai.example", max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    try:
        facts = await provider.get_price_history("600519", "股票")
    finally:
        await provider.aclose()
    assert [fact.period for fact in facts] == ["2026-09-21", "2026-09-22"]
    assert [fact.value for fact in facts] == [1252.57, 1253.80]
    assert {fact.field for fact in facts} == {"close_price"}
    assert calls[0].headers["x-claw-skill-id"] == "hithink-market-query"
    assert "test-secret" not in repr(facts)


@pytest.mark.asyncio
async def test_history_provider_uses_fund_skill_and_excludes_forward_filled_future_date() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": [{
            "基金代码": "005827.OF", "最新净值日期": "20260922",
            "单位净值[20260921]": 1.20, "单位净值[20260922]": 1.21,
            "单位净值[20260923]": 1.21,
        }]})

    provider = IwencaiSkillHubProvider(
        "test-secret", base_url="https://iwencai.example", max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    try:
        facts = await provider.get_price_history("005827", "基金")
    finally:
        await provider.aclose()
    assert [fact.period for fact in facts] == ["2026-09-21", "2026-09-22"]
    assert {fact.field for fact in facts} == {"fund_nav"}
    assert calls[0].headers["x-claw-skill-id"] == "hithink-fund-query"


def test_history_parser_rejects_undated_values_other_securities_and_conflicts() -> None:
    payload = {"data": [
        {"股票代码": "600519.SH", "最新价": 99},
        {"股票代码": "000001.SZ", "收盘价[20260921]": 1},
        {"股票代码": "600519.SH", "收盘价[20260921]": 10,
         "收盘价[20260922]": 11, "收盘价[20260923]": 12},
        {"股票代码": "600519.SH", "收盘价[20260922]": 15},
    ]}
    facts = _history_facts(
        payload, entity_hint="600519", source_id="IWENCAI_SKILLHUB",
        metric="close_price", limit=30,
    )
    assert [(fact.period, fact.value) for fact in facts] == [
        ("2026-09-21", 10.0), ("2026-09-23", 12.0),
    ]


def test_history_api_requires_login_and_returns_only_chart_fields(monkeypatch) -> None:
    client = TestClient(main_module.app)
    unauthenticated = client.post(
        "/api/v1/data/price-history",
        json={"target": "600519", "asset_type": "股票"},
    )
    assert unauthenticated.status_code == 401

    class Provider:
        async def get_price_history(self, target: str, asset_type: str, *, limit: int):
            assert (target, asset_type, limit) == ("600519", "股票", 30)
            return [
                FactRecord(fact_id=f"private-{day}", entity=target, field="close_price",
                           value=10 + day, period=f"2026-09-{day:02d}",
                           snapshot_time=datetime.now(timezone.utc),
                           source_id="IWENCAI_SKILLHUB", quality=0.9)
                for day in (21, 22)
            ]

    monkeypatch.setattr(main_module, "data_provider", Provider())
    main_module.app.dependency_overrides[main_module.authenticated_user] = lambda: {"id": 1}
    try:
        response = client.post(
            "/api/v1/data/price-history",
            json={"target": "600519", "asset_type": "股票"},
        )
        invalid = client.post(
            "/api/v1/data/price-history",
            json={"target": "600519", "asset_type": "行业"},
        )
    finally:
        main_module.app.dependency_overrides.pop(main_module.authenticated_user, None)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert [point["date"] for point in body["points"]] == ["2026-09-21", "2026-09-22"]
    assert "fact_id" not in str(body)
    assert invalid.status_code == 422


def test_history_api_does_not_turn_missing_series_into_a_chart(monkeypatch) -> None:
    client = TestClient(main_module.app)

    class EmptyProvider:
        async def get_price_history(self, *_args, **_kwargs):
            return []

    monkeypatch.setattr(main_module, "data_provider", EmptyProvider())
    main_module.app.dependency_overrides[main_module.authenticated_user] = lambda: {"id": 1}
    try:
        response = client.post(
            "/api/v1/data/price-history",
            json={"target": "600519", "asset_type": "股票"},
        )
        monkeypatch.setattr(main_module, "data_provider", None)
        unavailable = client.post(
            "/api/v1/data/price-history",
            json={"target": "600519", "asset_type": "股票"},
        )
    finally:
        main_module.app.dependency_overrides.pop(main_module.authenticated_user, None)
    assert response.status_code == 200
    assert response.json()["status"] == "empty"
    assert response.json()["points"] == []
    assert unavailable.status_code == 200
    assert unavailable.json()["status"] == "unavailable"
    assert unavailable.json()["points"] == []
