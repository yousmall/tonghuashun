"""Evidence completeness, scoped metric queries and dependency-aware recovery budgets."""
from datetime import timedelta
import asyncio

import pytest

from backend.app.models import DataAcquisitionResult, Intent
from backend.app.services.research import AutomatedResearchPipeline, DataCall
from backend.app.services.research_requirements import evidence_status, select_calls
from backend.app.services.on_demand_research import gap_calls, select_assessment_documents
from backend.app.services.research_recovery import recover_research
from backend.app.services.history import summarise_advice
from tests.test_on_demand_research import Provider, Assessor, request


def test_http_success_with_only_forecast_profit_is_not_target_price_fulfillment():
    provider = Provider()
    facts = [provider.fact("示例科技", "close_price", 100, unit="CNY", code="600001.SH"),
             provider.fact("示例科技", "forecast_profit", 123, code="600001.SH")]
    call = DataCall("institutional_research", "get_institutional_research", ("600001",), "test")
    status = evidence_status(call, facts, provider.now)
    assert status["status"] == "partial" and status["missing_fields"] == ["target_price"]
    assert status["reason_codes"] == ["REQUIRED_FIELD_MISSING"]


@pytest.mark.parametrize("updates", [{"unit": None}, {"period": "REC-unknown"}, {"source_url": None},
    {"value": float("nan")}, {"value": True}, {"entity": "其他公司", "entity_code": "600002.SH"}])
def test_target_price_requires_metadata_and_exact_security(updates):
    provider = Provider()
    fact = provider.fact("示例科技", "target_price", 120, unit="CNY", code="600001.SH", document=True)
    call = DataCall("targets", "get_institutional_research", ("600001",), "test")
    assert evidence_status(call, [fact.model_copy(update=updates)], provider.now)["status"] == "partial"
    assert evidence_status(call, [fact], provider.now)["status"] == "complete"


def test_count_pair_needs_one_scope_date_and_integer_counts():
    provider = Provider()
    call = DataCall("breadth", "get_market_breadth", ("中国宏观经济",), "test",
                    required_fields=("advancing_count", "market_total_count"), expected_entity="中国宏观经济")
    a = provider.fact("中国宏观经济", "advancing_count", 100)
    b = provider.fact("中国宏观经济", "market_total_count", 500)
    assert evidence_status(call, [a, b], provider.now)["status"] == "complete"
    missing = evidence_status(call, [a], provider.now)
    assert missing["missing_fields"] == ["market_total_count"] and missing["invalid_fields"] == []
    for changed in [b.model_copy(update={"entity": "其他市场"}), b.model_copy(update={"period": "2000-01-01"}),
                    b.model_copy(update={"value": 0}), b.model_copy(update={"value": 500.5})]:
        assert evidence_status(call, [a, changed], provider.now)["status"] == "partial"


def test_priority_budget_keeps_history_and_calendar_together():
    pipeline = AutomatedResearchPipeline(Provider())
    calls = [DataCall("low", "get_quote", ("low",), "low", priority=10),
        DataCall("history", "get_industry_turnover_history", ("制造业", "start", "end"), "hist", priority=70, bundle="history"),
        DataCall("calendar", "get_market_calendar", ("start", "end"), "cal", priority=70, bundle="history"),
        DataCall("scope", "get_basic_info", ("600001",), "scope", priority=100)]
    selected, deferred = select_calls(pipeline, calls, 3)
    assert [c.label for c in selected] == ["scope", "history", "calendar"]
    assert [c.label for c in deferred] == ["low"]
    selected, deferred = select_calls(pipeline, calls, 2)
    assert not any(c.label in {"history", "calendar"} for c in selected)
    assert {c.label for c in deferred} == {"history", "calendar"}


def test_valid_quote_is_reused_while_only_missing_targets_are_queried():
    provider = Provider()
    req = request(provider)
    req.facts.extend([provider.fact("示例科技", "industry", "制造业", code="600001.SH"),
                      provider.fact("示例科技", "close_price", 100, unit="CNY", code="600001.SH")])
    calls = gap_calls(AutomatedResearchPipeline(provider), req, Intent.SECURITY_RESEARCH, "示例科技", DataAcquisitionResult())
    assert not any(c.label.startswith("return_quote:") for c in calls)
    assert any(c.label.startswith("return_targets:") for c in calls)


@pytest.mark.asyncio
async def test_focused_m2_and_late_industry_calls_share_one_budget():
    class Focused(Provider):
        async def get_macro_data(self, query):
            self.calls.append(("macro", query))
            assert query == "中国最新M2同比增长率"
            return [self.fact("中国宏观经济", "m2_growth", 8, unit="percent")]
        async def get_industry_flow(self, industry):
            return [self.fact(industry, "capital_flow", 100, unit="CNY", code="884001.TI"),
                    self.fact(industry, "turnover_value", 1000, unit="CNY", code="884001.TI")]
    provider = Focused()
    req = request(provider)
    req.facts = [f for f in req.facts if f.entity != "制造业" and f.field != "liquidity_score"]
    req.facts.append(provider.fact("示例科技", "close_price", 100, unit="CNY", code="600001.SH"))
    output, audit = await recover_research(AutomatedResearchPipeline(provider), req, Intent.SECURITY_RESEARCH,
        None, DataAcquisitionResult(), target="示例科技", semantic=Assessor())
    assert any(f.field == "liquidity_score" for f in output.facts)
    assert {"industry_history", "trading_calendar"} <= set(audit.recovery_capabilities)
    assert "return_quote:0" not in audit.recovery_capabilities
    assert len(audit.recovery_capabilities) <= 12 and audit.recovery_rounds == 1
    assert audit.recovery_evidence["macro_liquidity"]["status"] == "complete"
    assert audit.recovery_gap_metrics["missing_after"] < audit.recovery_gap_metrics["missing_before"]
    compact = summarise_advice({"data_acquisition": audit.model_dump(mode="json")})
    assert compact["data_acquisition"]["recovery_evidence"] == audit.recovery_evidence


def test_document_selection_deduplicates_records_and_uses_publication_date():
    provider = Provider()
    title = provider.fact("示例科技", "announcement", "年度报告", document=True)
    summary = provider.fact("示例科技", "announcement_summary", "年度报告审计意见、监管情况与信息披露。", document=True)
    old = provider.fact("示例科技", "announcement", "旧监管资料", document=True, period="2000-01-01")
    old.source_url = "https://example.com/old"
    selected = select_assessment_documents([title, summary, old], [{"entity": "示例科技", "dimension": "governance"}])
    assert title not in selected and summary in selected
    assert selected[0] is summary


def test_stale_quote_does_not_suppress_refresh():
    provider = Provider()
    req = request(provider)
    req.facts.append(provider.fact("示例科技", "close_price", 100, unit="CNY").model_copy(
        update={"snapshot_time": provider.now - timedelta(hours=1)}))
    calls = gap_calls(AutomatedResearchPipeline(provider), req, Intent.SECURITY_RESEARCH, "示例科技", DataAcquisitionResult())
    assert any(c.label.startswith("return_quote:") for c in calls)


@pytest.mark.asyncio
async def test_shared_recovery_deadline_retains_fast_sources_and_skips_model():
    class SlowScope(Provider):
        async def get_basic_info(self, query):
            await asyncio.sleep(.2)
            return await super().get_basic_info(query)
    provider, assessor = SlowScope(), Assessor()
    pipeline = AutomatedResearchPipeline(provider, call_timeout_seconds=1, recovery_timeout_seconds=.03)
    try:
        output, audit = await recover_research(pipeline, request(provider), Intent.SECURITY_RESEARCH,
            None, DataAcquisitionResult(), target="示例科技", semantic=assessor)
        assert audit.recovery_errors["security_scope:0"]["code"] == "RECOVERY_TIMEOUT"
        assert any(f.field == "news" for f in output.facts)
        assert assessor.calls == 0 and audit.assessment_status == "unavailable"
        assert audit.recovery_rounds == 1
    finally:
        await pipeline.cache.aclose()


def test_pdf_pages_remain_independent_evidence_after_record_deduplication():
    provider = Provider()
    pages = [provider.fact("示例科技", "announcement_excerpt", text, document=True).model_copy(
        update={"fact_id": f"page-{i}", "source_field": f"PDF第{i}页"})
        for i, text in enumerate(["审计意见", "监管状态", "披露情况"], 1)]
    selected = select_assessment_documents(pages, [{"entity": "示例科技", "dimension": "governance"}])
    assert {f.fact_id for f in selected} == {"page-1", "page-2", "page-3"}
