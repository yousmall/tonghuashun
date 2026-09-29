"""领域数据概览：真实字段口径、有限只读调用、分栏失败及展示缓存。"""
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.data_provider.iwencai import IwencaiSkillHubProvider
from backend.app.models.schemas import DataOverviewRequest, FactRecord
from backend.app.services.market_overview import fetch_overview
from frontend.research_board import data_period, entity_rows, format_fact


def fact(field="close_price", value=10, *, period="2026-09-24", entity="公司甲", code="600001.SH", raw=None):
    return FactRecord(fact_id="fixture-" + field, entity=entity, entity_code=code, field=field, value=value,
                      period=period, source_field=raw, snapshot_time=datetime.now(timezone.utc), source_id="TEST", quality=.9)


@pytest.mark.asyncio
async def test_provider_keeps_fund_identity_and_per_metric_dates():
    rows = [
        {"基金简称": "基金甲", "基金代码": "510300.SH", "单位净值[20260924]": 1.4, "管理费率": .5},
        {"基金简称": "基金乙", "基金代码": "510500.SH", "单位净值[20260923]": 2.1, "管理费率": .15},
        {"股票简称": "公司甲", "股票代码": "600001.SH", "收盘价[20260924]": 12,
         "净资产收益率[20260630]": 7.5, "报告期": "2025年报"},
    ]
    provider = IwencaiSkillHubProvider("test-only", transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"datas": rows})))
    try:
        facts = await provider.query("只读测试")
    finally:
        await provider.aclose()
    funds = [f for f in facts if f.field == "fund_nav"]
    assert {f.entity for f in funds} == {"基金甲", "基金乙"}
    assert {f.entity_code for f in funds} == {"510300.SH", "510500.SH"}
    assert {f.period for f in funds} == {"2026-09-24", "2026-09-23"}
    assert next(f for f in facts if f.field == "roe").period == "2026-06-30"
    assert next(f for f in facts if f.field == "close_price").source_field == "收盘价[20260924]"


@pytest.mark.asyncio
async def test_provider_keeps_conversion_value_separate_from_conversion_price():
    provider = IwencaiSkillHubProvider("test-only", transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
        "datas": [{"可转债简称": "转债甲", "可转债代码": "111001.SH", "转股价": 33.5,
                   "转股价值[20260924]": 179.4, "债券余额": 5.7e8}]})))
    try:
        facts = await provider.query("只读测试")
    finally:
        await provider.aclose()
    assert {f.field: f.value for f in facts}["conversion_price"] == 33.5
    assert {f.field: f.value for f in facts}["conversion_value"] == 179.4
    assert {f.field: f.value for f in facts}["remaining_size"] == 5.7e8


class Provider:
    def __init__(self, failing=()):
        self.calls = []
        self.failing = failing

    async def query(self, query, **kwargs):
        self.calls.append(("query", query, kwargs))
        return [fact()]

    async def get_macro_data(self, query):
        if "macro" in self.failing:
            raise RuntimeError("internal diagnostic must not reach UI")
        return [fact("pmi", 49.8)]

    async def get_news(self, query):
        if "news" in self.failing:
            raise RuntimeError("unavailable")
        return [fact("news", "测试资讯")]

    async def get_financial_metrics(self, target):
        self.calls.append(("financial", target))
        return [fact("pe_ttm", 20)]

    async def get_basic_info(self, target):
        self.calls.append(("company", target))
        return [fact("industry", "银行")]

    async def get_price_history(self, target, **kwargs):
        self.calls.append(("history", target, kwargs))
        return [fact(period="2026-09-23"), fact(period="2026-09-24", value=11)]


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["market", "industry", "stock", "fund", "convertible"])
async def test_overview_uses_bounded_readonly_jobs(direction):
    provider = Provider()
    result = await fetch_overview(provider, DataOverviewRequest(direction=direction))
    assert result.status == "ok"
    assert 1 <= len(result.sections) <= 4
    query_calls = [call for call in provider.calls if call[0] == "query"]
    assert all(call[2]["limit"] <= 8 for call in query_calls)
    assert all(call[2].get("skill_id", "hithink-market-query") in {"hithink-market-query", "hithink-fund-query", "hithink-astock-selector"} for call in query_calls)
    if direction == "stock":
        assert ("financial", "600519") in provider.calls
        assert ("company", "600519") in provider.calls
        assert ("history", "600519", {"asset_type": "股票", "limit": 30}) in provider.calls


@pytest.mark.asyncio
async def test_overview_retains_successful_sections_when_macro_fails():
    result = await fetch_overview(Provider(failing={"macro"}), DataOverviewRequest(direction="market"))
    assert result.status == "partial"
    sections = {section.key: section for section in result.sections}
    assert sections["overview"].facts
    assert sections["news"].facts
    assert sections["macro"].status == "unavailable"
    assert not sections["macro"].facts
    assert "internal diagnostic" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_non_tabular_metadata_is_not_successful_market_data():
    class ScalarProvider(Provider):
        async def query(self, query, **kwargs):
            return [fact("provider_response", "2495")]
    result = await fetch_overview(ScalarProvider(), DataOverviewRequest(direction="fund"))
    assert result.status == "empty"
    assert not result.sections[0].facts


@pytest.mark.asyncio
async def test_missing_provider_is_unavailable_without_fake_numbers():
    result = await fetch_overview(None, DataOverviewRequest(direction="stock"))
    assert result.status == "unavailable"
    assert all(section.status == "unavailable" and not section.facts for section in result.sections)


def test_overview_http_route_and_direction_allowlist(monkeypatch):
    import backend.app.main as main
    monkeypatch.setattr(main, "data_provider", Provider())
    from backend.app.services.overview_cache import OverviewCache
    monkeypatch.setattr(main, "overview_cache", OverviewCache())
    main.app.dependency_overrides[main.customer_user] = lambda: {"id": 1, "role": "user"}
    client = TestClient(main.app)
    response = client.post("/api/v1/data/overview", json={"direction": "stock", "target": "600036"})
    assert response.status_code == 200
    assert response.json()["target"] == "600036"
    assert response.json()["status"] == "ok"
    assert client.post("/api/v1/data/overview", json={"direction": "execute_trade"}).status_code == 422
    assert client.post("/api/v1/data/overview", json={"direction": "stock", "target": "a"*61}).status_code == 422
    main.app.dependency_overrides.pop(main.customer_user, None)


def test_board_display_preserves_percent_units_dates_and_missing_data():
    assert format_fact(fact("fee_rate", .5).model_dump()) == "0.50%"
    assert format_fact(fact("roe", 17.9543).model_dump()) == "17.95%"
    assert format_fact(fact("listing_date", "20010827").model_dump()) == "2001-08-27"
    assert format_fact(None) == "—"
    assert data_period({"period": None, "snapshot_time": "2026-09-26"}) == "日期未提供"
    assert format_fact(fact("fund_size", 6.17e8).model_dump()) == "6.17 亿元"


def test_board_groups_by_code_and_does_not_hide_same_period_conflicts():
    facts = [fact(value=10).model_dump(), fact(value=15).model_dump(),
             fact(value=20, entity="同名公司", code="600002.SH").model_dump(),
             fact("roe", 7, period="2026-03-31").model_dump(), fact("roe", 8, period="2026-06-30").model_dump()]
    rows = entity_rows(facts)
    assert len(rows) == 2
    assert "close_price" in rows[0]["conflicts"]
    assert "close_price" not in rows[0]["fields"]
    assert rows[0]["fields"]["roe"]["value"] == 8
    assert rows[1]["fields"]["close_price"]["value"] == 20


def test_provider_rounded_latest_percentage_does_not_hide_dated_nav_change():
    rows = entity_rows([fact("nav_change", -1.6838, period="20260924").model_dump(),
                        fact("nav_change", -1.68381, period="2026-09-24").model_dump()])
    assert not rows[0]["conflicts"]
    assert format_fact(rows[0]["fields"]["nav_change"]) == "-1.68%"
