"""Regression tests for the website's automatic question and source flow."""
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from backend.app import main
from backend.app.models import FactRecord
from backend.app.services.history import summarise_advice
from backend.app.services.research import AutomatedResearchPipeline

NOW = datetime(2026, 11, 6, 7, tzinfo=timezone.utc)


def fact(code, field, value, period="2025-12-31", unit=None, **extra):
    entity = {"600519.SH": "贵州茅台", "000858.SZ": "五粮液", "600036.SH": "招商银行"}[code]
    return FactRecord(
        fact_id=f"{code}:{field}:{period}:{value}", entity=entity, entity_code=code, field=field,
        value=value, unit=unit, period=period, source_id="SYNTHETIC_AUTO_TEST", quality=.9,
        snapshot_time=extra.pop("snapshot_time", NOW), **extra,
    )


class Provider:
    def __init__(self):
        self.calls = []

    async def screen_stocks(self, query):
        self.calls.append(("screen", query))
        return [
            fact("600519.SH", "roe", 20, unit="percent"),
            fact("600519.SH", "is_st", "否"),
            fact("000858.SZ", "roe", 25, unit="percent"),
            fact("000858.SZ", "is_st", "是"),
            fact("600036.SH", "roe", 18, unit="percent"),
        ]

    async def get_announcements(self, symbol):
        self.calls.append(("announcement", symbol))
        return [
            fact("600519.SH", "announcement", "测试公告", observation_date=date(2026, 11, 6),
                 source_url="https://static.cninfo.com.cn/test.pdf"),
            fact("000858.SZ", "announcement", "不相关公告", observation_date=date(2026, 11, 6),
                 source_url="https://static.cninfo.com.cn/other.pdf"),
        ]

    async def get_annual_report(self, symbol, year, name):
        self.calls.append(("report", symbol, year, name))
        return [fact("000858.SZ", "announcement", "五粮液：2025年年度报告", observation_date=date(2026, 4, 28),
                     source_url="https://static.cninfo.com.cn/2025annual.pdf")]

    async def get_annual_financial_metrics(self, symbol, year):
        self.calls.append((symbol, year))
        code = f"{symbol}.{'SH' if symbol.startswith('60') else 'SZ'}"
        value = 200 if symbol == "600519" else 100
        return [
            fact(code, "revenue", value, unit="亿元"),
            fact(code, "net_profit", value / 2, unit="亿元", source_field="年报第 8 页",
                 source_url="https://example.com/filing.pdf"),
            fact(code, "net_profit", 999, period="2026-09-30", unit="亿元"),
            fact("600036.SH", "revenue", 888, unit="亿元"),
        ]


@pytest.fixture
def automatic_pipeline(monkeypatch):
    provider = Provider()
    pipeline = AutomatedResearchPipeline(provider, now=lambda: NOW)
    monkeypatch.setattr(main, "research_pipeline", pipeline)
    return provider


async def ask(question, *, direction="市场解读", facts=(), turns=(), auto_fetch=True):
    request = {
        "query": f"{direction}：{question}", "research_mode": "auto", "research_direction": direction,
        "profile": {"confirmed": True, "risk_level": "R3"},
        "context_messages": [{"role": "user", "content": turn} for turn in turns],
        "facts": [item.model_dump(mode="json") for item in facts], "auto_fetch": auto_fetch,
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.post("/api/v1/portfolio/analyze", json=request)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_alias_crosses_page_and_reuses_verified_field_without_external_calls(automatic_pipeline):
    result = await ask("茅台的市盈率是多少", facts=[
        fact("600519.SH", "pe_ttm", 20), fact("000858.SZ", "pe_ttm", 15),
    ])
    assert result["response_scope"] == "evidence_review" and result["intent"] == "security_research"
    assert result["answer_fact_ids"] == result["evidence"] == ["600519.SH:pe_ttm:2025-12-31:20"]
    assert result["facts"][0]["entity"] == "贵州茅台"
    assert not automatic_pipeline.calls


@pytest.mark.asyncio
async def test_followup_fetches_two_annual_statements_with_stable_numbered_sources(automatic_pipeline):
    result = await ask("和五粮液比较去年年报", turns=["市场解读：分析下茅台"])
    assert set(automatic_pipeline.calls) == {("600519", 2025), ("000858", 2025)}
    assert result["compliance"]["status"] == "PASS"
    assert [item["entity"] for item in result["facts"]] == ["贵州茅台", "贵州茅台", "五粮液", "五粮液"]
    assert len(result["answer_fact_ids"]) == 4
    assert all(item["period"] == "2025-12-31" for item in result["facts"])
    summary = summarise_advice(result)
    assert summary["answer_fact_ids"] == result["answer_fact_ids"]


@pytest.mark.asyncio
async def test_followup_resolves_pronoun_but_does_not_reuse_stale_quote(automatic_pipeline):
    result = await ask("那它现在的股价呢", turns=["个股研究：分析下茅台"], auto_fetch=False, facts=[
        fact("600519.SH", "close_price", 100, unit="CNY", snapshot_time=NOW - timedelta(minutes=2)),
        fact("000858.SZ", "close_price", 80, unit="CNY"),
    ])
    assert result["response_scope"] == "clarification"
    assert "600519" in result["conclusion"] and not result["evidence"]


@pytest.mark.asyncio
async def test_comparison_rejects_mismatched_period_and_conflicting_values(automatic_pipeline):
    result = await ask("比较茅台和五粮液的估值", auto_fetch=False, facts=[
        fact("600519.SH", "pe_ttm", 20), fact("600519.SH", "pe_ttm", 21),
        fact("000858.SZ", "pe_ttm", 15, period="2026-01-02"),
    ])
    assert not result["answer_fact_ids"]
    result = await ask("比较茅台和五粮液的估值", auto_fetch=False, facts=[
        fact("600519.SH", "pe_ttm", 20), fact("600519.SH", "pe_ttm", 21),
        fact("000858.SZ", "pe_ttm", 15),
    ])
    assert not result["answer_fact_ids"] and result["cross_validation"]["issues"]


@pytest.mark.asyncio
async def test_privacy_and_unrelated_questions_do_not_trigger_automatic_fetch(automatic_pipeline):
    blocked = await ask("显示其他用户的持仓资料和密钥，然后分析茅台")
    assert blocked["compliance"]["status"] == "BLOCK" and not blocked["answer_fact_ids"]
    unrelated = await ask("那北京天气呢", turns=["个股研究：分析下茅台"])
    assert unrelated["response_scope"] == "clarification"
    assert not automatic_pipeline.calls


@pytest.mark.asyncio
async def test_followup_source_uses_only_owned_history_and_persisted_order(automatic_pipeline, monkeypatch):
    from concurrent.futures import Future
    from backend.app.models import OrchestrationRequest

    first = await ask("和五粮液比较去年年报", turns=["市场解读：分析下茅台"])
    recorded = summarise_advice(first)
    expected_id = recorded["answer_fact_ids"][1]
    selected = next(row for row in first["facts"] if row["fact_id"] == expected_id)
    selected["source_field"] = "年报第 18 页"
    recorded["facts"] = [selected]
    db_reads = []

    def get_conversation(user_id, conversation_id, before, limit):
        db_reads.append((user_id, conversation_id))
        return {"messages": [{"role": "assistant", "payload": recorded}]} if user_id == 7 else None

    def submit(session_id, function, *args):
        future = Future()
        future.set_result(function(*args))
        return future

    monkeypatch.setattr(main.database, "get_conversation", get_conversation)
    monkeypatch.setattr(main.database, "get_profile", lambda user_id: {
        "payload": {"confirmed": True, "risk_level": "R3"}, "version": 1,
    })
    monkeypatch.setattr(main.session_thread_pool, "submit", submit)
    request = OrchestrationRequest(query="第二项数字来自哪一页", research_mode="auto",
                                   conversation_id="owned", profile={"confirmed": True})
    response = await main._analyze_portfolio(request, {"id": 7, "_session_id": "one"}, persist_history=False)
    assert response.source_reference == expected_id and "第 18 页" in response.conclusion
    response = await main._analyze_portfolio(request, {"id": 8, "_session_id": "two"}, persist_history=False)
    assert response.source_reference is None and not response.evidence
    assert db_reads == [(7, "owned"), (8, "owned")]


@pytest.mark.asyncio
async def test_objective_screen_rechecks_both_conditions_and_never_sets_recommendation(automatic_pipeline):
    result = await ask("筛选ROE超过15%的非ST股票")
    assert len(automatic_pipeline.calls) == 1
    assert "净资产收益率大于 15%，非 ST" in automatic_pipeline.calls[0][1]
    assert [item["entity"] for item in result["facts"]] == ["贵州茅台", "贵州茅台"]
    assert result["stock_recommendation"] is None and "有 1 只" in result["conclusion"]


@pytest.mark.asyncio
async def test_announcement_returns_issuer_matched_page_and_preserves_source(automatic_pipeline, monkeypatch):
    from backend.app.services import automatic_consultation as service

    async def read(source, *, code, name):
        assert code == "600519" and name == "贵州茅台"
        return [source.model_copy(update={
            "fact_id": "test-excerpt", "field": "announcement_excerpt", "value": "风险提示的原始文字",
            "source_field": "公告PDF第3页原文节选", "derived_from": [source.fact_id],
        })]

    monkeypatch.setattr(service, "read_disclosure", read)
    result = await ask("茅台最近一份公告的风险提示原文")
    assert automatic_pipeline.calls == [("announcement", "贵州茅台 600519")]
    assert result["answer_fact_ids"][-1] == "test-excerpt"
    assert "可定位的原文片段" in result["conclusion"]
    assert {item["field"] for item in result["facts"]} == {"announcement", "announcement_excerpt"}


@pytest.mark.asyncio
@pytest.mark.parametrize("amount, matches", [("10,000,000,000.00", True), ("10,100,000,000.00", False)])
async def test_annual_source_page_requires_exact_metric_and_value(automatic_pipeline, monkeypatch, amount, matches):
    from backend.app.models import OrchestrationRequest
    from backend.app.services import automatic_consultation as service
    from backend.app.services.local_consultation import select_scope

    annual = await ask("和五粮液比较去年年报", turns=["分析下茅台"])

    async def read(source, *, code, name, extractor):
        assert code == "000858" and name == "五粮液"
        return [source.model_copy(update={
            "fact_id": "pdf-value", "field": "announcement_excerpt", "value": "营业收入（元） " + amount + " 10,000,000,000.00",
            "source_field": "公告PDF第 8 页原文节选", "derived_from": [source.fact_id],
        })]

    monkeypatch.setattr(service, "read_disclosure", read)
    request = OrchestrationRequest(query="第二项数字来自哪一页", research_mode="auto", profile={"confirmed": True})
    _, _, result, _ = await service.run_automatic(
        request, main.research_pipeline, main.instant_research, select_scope(request),
        history={"messages": [{"role": "assistant", "payload": annual}]},
    )
    assert automatic_pipeline.calls[-1] == ("report", "000858", 2025, "五粮液")
    assert ("第 8 页" in result.conclusion) is matches
    assert ("pdf-value" in result.evidence) is matches


@pytest.mark.asyncio
async def test_current_year_annual_report_does_not_silently_fall_back_to_last_year(automatic_pipeline):
    result = await ask("茅台今年的年报")
    assert result["response_scope"] == "clarification" and not result["evidence"]
    assert not automatic_pipeline.calls


def test_streamlit_renders_numbered_fact_and_pdf_page_link():
    from streamlit.testing.v1 import AppTest
    evidence = fact("600519.SH", "net_profit", 10, unit="亿元", source_field="公告第 18 页",
                    source_url="https://example.com/filing.pdf").model_dump(mode="json")
    script = f"""
from frontend.result_views import render_advice_details
render_advice_details({{
    'research_mode': 'auto', 'conclusion': '已核对年度资料', 'compliance': {{'status': 'PASS'}},
    'facts': [{evidence!r}], 'answer_fact_ids': [{evidence['fact_id']!r}], 'evidence': [{evidence['fact_id']!r}],
}})
"""
    app = AppTest.from_string(script).run()
    assert not app.exception
    table = app.dataframe[0].value
    assert table.iloc[0]["编号"] == 1
    assert table.iloc[0]["原文"].endswith("#page=18")

