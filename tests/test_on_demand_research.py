"""按需问财补取的范围、引用、失败边界和 HTTP 闭环。"""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import httpx
import pytest

from backend.app import main
from backend.app.models import FactRecord, Intent, OrchestrationRequest, DataAcquisitionResult
from backend.app.data_provider.iwencai import IwencaiSkillHubProvider
from backend.app.semantic import StockEvidenceReview
from backend.app.services.evidence_coverage import AGENT_FIELDS
from backend.app.services.research import AutomatedResearchPipeline
from backend.app.services.research_recovery import recover_research
from backend.app.services.on_demand_research import gap_calls, assess_gap_evidence
from backend.app.services.return_expectation import build_return_expectation
from backend.app.services.recommendation_recovery import history_window
from backend.app.services.provider_errors import ProviderCallError


class Provider:
    source_id = "ON_DEMAND_TEST"

    def __init__(self):
        self.now = datetime.now(timezone.utc)
        self.calls = []
        self.no_industry = False

    def fact(self, entity, field, value, *, unit=None, code=None, document=False, period=None):
        return FactRecord(fact_id=f"D-{entity}-{field}-{period or ''}", entity=entity, field=field,
            value=value, unit=unit, entity_code=code, snapshot_time=self.now, quality=.95,
            source_id=self.source_id, period=period or self.now.date().isoformat(),
            source_url="https://example.com/disclosure" if document else None)

    def initial(self):
        return [self.fact(entity, field, 55, code="600001.SH" if agent == "security" else None)
                for entity, agent, omissions in [
                    ("中国宏观经济", "market", {"policy_score", "risk_appetite_score"}),
                    ("制造业", "industry", {"policy_score", "prosperity_score", "crowding_score"}),
                    ("示例科技", "security", {"event_score", "governance_score"})]
                for field in AGENT_FIELDS[agent] if field not in omissions]

    async def get_macro_data(self, query):
        self.calls.append(("macro", query))
        return [f for f in self.initial() if f.entity == "中国宏观经济"]

    async def get_industry_rank(self, query):
        self.calls.append(("industry", query))
        return [f for f in self.initial() if f.entity == "制造业"]

    async def get_financial_metrics(self, query):
        self.calls.append(("financial", query))
        return [f for f in self.initial() if f.entity == "示例科技"]

    async def get_event_data(self, query):
        self.calls.append(("event", query))
        return [self.fact("示例科技", "announcement", "公司披露业绩增长的公告。", code="600001.SH", document=True)]

    async def get_macro_policy(self, entity):
        self.calls.append(("macro_policy", entity))
        return [self.fact(entity, "news", "政策明确支持实体经济发展。", document=True)]

    async def get_market_breadth(self, entity):
        self.calls.append(("breadth", entity))
        return [self.fact(entity, "advancing_count", 3000), self.fact(entity, "market_total_count", 5000)]

    async def get_basic_info(self, query):
        self.calls.append(("basic", query))
        return [self.fact("示例科技", "industry", "制造业", code="600001.SH")]

    async def get_industry_fundamentals(self, industry):
        self.calls.append(("industry_fundamentals", industry))
        return [self.fact(industry, "industry_revenue_growth", 10, unit="percent", code="884001.TI")]

    async def get_industry_policy(self, industry):
        self.calls.append(("industry_policy", industry))
        return [self.fact(industry, "news", "政策支持该行业投资。", document=True)]

    def sessions(self, start, end):
        begin, stop = datetime.fromisoformat(start).date(), datetime.fromisoformat(end).date()
        return [begin + timedelta(days=index) for index in range((stop - begin).days + 1)
                if (begin + timedelta(days=index)).weekday() < 5]

    async def get_market_calendar(self, start, end):
        self.calls.append(("calendar", start))
        sessions = self.sessions(start, end)
        return [*(self.fact("中国A股交易日历", "market_session", 1, period=day.isoformat()) for day in sessions),
                self.fact("中国A股交易日历", "market_session_count", len(sessions), period=f"{start}/{end}")]

    async def get_industry_turnover_history(self, industry, start, end):
        self.calls.append(("industry_history", industry))
        return [self.fact(industry, "industry_turnover_history", index % 10 + 1, unit="percent", period=day.isoformat())
                for index, day in enumerate(self.sessions(start, end))]

    async def get_governance_disclosures(self, query):
        self.calls.append(("governance", query))
        return [self.fact("示例科技", "announcement", "审计为无保留意见；报告明确列出监管状态和按时披露情况。",
                          code="600001.SH", document=True)]

    async def get_stock_disclosure_details(self, query):
        self.calls.append(("disclosure_details", query))
        return await self.get_governance_disclosures(query)

    async def get_quote(self, query):
        self.calls.append(("quote", query))
        return [self.fact("示例科技", "close_price", 100, unit="CNY", code="600001.SH")]

    async def get_institutional_research(self, query):
        self.calls.append(("targets", query))
        return [self.fact("示例科技", "target_price", 120, unit="CNY", code="600001.SH", document=True)]


class Assessor:
    def __init__(self, mode="valid"):
        self.mode, self.calls = mode, 0

    async def assess_stock_evidence(self, request, facts, targets, conditions):
        self.calls += 1
        assessments = []
        for target in targets:
            dimension = target["dimension"]
            docs = [f for f in facts if f.entity == target["entity"]]
            if not docs:
                continue
            doc = next((f for f in docs if "审计" in str(f.value)), docs[0]) if dimension == "governance" else docs[0]
            criteria = {"policy": ["policy"], "event": ["event"],
                        "governance": ["audit_opinion", "regulatory_status", "disclosure_status"]}[dimension]
            labels = {"policy": "supportive", "event": "favorable", "audit_opinion": "unqualified",
                      "regulatory_status": "explicitly_clear", "disclosure_status": "timely"}
            items = [{"criterion": criterion, "label": labels[criterion], "evidence_id": doc.fact_id,
                      "quote": str(doc.value)} for criterion in criteria]
            if self.mode == "bogus_id":
                items[0]["evidence_id"] = "not-in-source"
            elif self.mode == "bogus_quote":
                items[0]["quote"] = "原文不存在的判断"
            elif self.mode == "wrong_entity":
                items[0]["evidence_id"] = next((f.fact_id for f in facts if f.entity != target["entity"]), "absent")
            elif self.mode == "incomplete_governance" and dimension == "governance":
                items = items[:1]
            assessments.append({**target, "complete": self.mode != "incomplete", "items": items})
        return StockEvidenceReview(confidence=.9, assessments=assessments)


def request(provider):
    return OrchestrationRequest(query="请研究示例科技这只个股", profile={"confirmed": True, "risk_level": "R3"},
                                facts=provider.initial())


@pytest.mark.asyncio
async def test_all_screenshot_gaps_are_fetched_assessed_and_reproducible():
    provider, assessor = Provider(), Assessor()
    pipeline = AutomatedResearchPipeline(provider, now=lambda: provider.now)
    output, audit = await recover_research(pipeline, request(provider), Intent.SECURITY_RESEARCH, None,
                                         DataAcquisitionResult(), target="示例科技", semantic=assessor)
    fields = {(f.entity, f.field): f for f in output.facts}
    assert fields["中国宏观经济", "risk_appetite_score"].value == 60
    assert fields["制造业", "prosperity_score"].value == 70
    assert ("制造业", "crowding_score") in fields
    for entity, dimension in [("中国宏观经济", "policy"), ("制造业", "policy"),
                              ("示例科技", "event"), ("示例科技", "governance")]:
        assert fields[entity, dimension + "_score"].derived_from
    assert audit.missing_fields_by_agent == {} and audit.assessment_status == "completed"
    assert assessor.calls == 1 and len(audit.recovery_capabilities) <= 12
    forecast = build_return_expectation(output.facts, [f.fact_id for f in output.facts], output.profile, "PASS")
    assert forecast.status == "scenario" and forecast.scenarios[0].price_return == pytest.approx(.2)
    before = len(provider.calls)
    await recover_research(pipeline, output, Intent.SECURITY_RESEARCH, None, audit, semantic=assessor)
    assert len(provider.calls) == before


@pytest.mark.asyncio
async def test_industry_is_resolved_before_targeted_queries_in_the_same_round():
    provider = Provider()
    req = request(provider)
    req.facts = [f for f in req.facts if f.entity != "制造业"]
    output, audit = await recover_research(AutomatedResearchPipeline(provider), req, Intent.SECURITY_RESEARCH,
                                         None, DataAcquisitionResult(), target="示例科技", semantic=Assessor())
    # 初始行业排名已返回行业评分时可直接定位；基本信息仍按明确标的查询。
    # 未解析行业时，第一批基本信息查询或行业排名先确定范围；不会跨行业拼接评分。
    assert any(name in {"basic", "industry"} for name, _ in provider.calls)
    assert any(name == "industry_fundamentals" and query == "制造业" for name, query in provider.calls)
    assert audit.recovery_rounds == 1
    assert any(f.entity == "制造业" and f.field == "prosperity_score" for f in output.facts)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["bogus_id", "bogus_quote", "wrong_entity", "incomplete"])
async def test_invalid_model_assessment_never_generates_qualitative_scores(mode):
    provider = Provider()
    output, audit = await recover_research(AutomatedResearchPipeline(provider), request(provider),
        Intent.SECURITY_RESEARCH, None, DataAcquisitionResult(), semantic=Assessor(mode))
    assert audit.assessment_status == "partial"
    assert not any(f.field in {"policy_score", "event_score", "governance_score"} for f in output.facts)


@pytest.mark.asyncio
async def test_partial_governance_keeps_gap_and_preserves_other_assessments():
    provider = Provider()
    output, audit = await recover_research(AutomatedResearchPipeline(provider), request(provider),
        Intent.SECURITY_RESEARCH, None, DataAcquisitionResult(), semantic=Assessor("incomplete_governance"))
    assert any(f.field == "event_score" for f in output.facts)
    assert not any(f.field == "governance_score" for f in output.facts)
    assert audit.assessment_status == "partial" and "governance_score" in audit.missing_fields_by_agent["security"]


@pytest.mark.asyncio
async def test_model_outage_preserves_raw_sources_and_does_not_make_neutral_scores():
    provider = Provider()
    class Unavailable:
        async def assess_stock_evidence(self, *args):
            raise RuntimeError("private model failure")
    output, audit = await recover_research(AutomatedResearchPipeline(provider), request(provider),
        Intent.SECURITY_RESEARCH, None, DataAcquisitionResult(), semantic=Unavailable())
    assert audit.assessment_status == "unavailable"
    assert any(f.field == "announcement" for f in output.facts)
    assert not any(f.field == "governance_score" for f in output.facts)
    assert "private" not in audit.model_dump_json()


@pytest.mark.asyncio
async def test_opt_out_never_calls_provider_or_assessor():
    provider, assessor = Provider(), Assessor()
    req = request(provider).model_copy(update={"auto_fetch": False})
    output, audit = await recover_research(AutomatedResearchPipeline(provider), req, Intent.SECURITY_RESEARCH,
                                         None, DataAcquisitionResult(), semantic=assessor)
    assert output is req and audit.recovery_rounds == 0
    assert not provider.calls and assessor.calls == 0


@pytest.mark.parametrize("code", ["AUTHENTICATION_REJECTED", "CAPABILITY_FORBIDDEN", "PROVIDER_QUOTA_EXHAUSTED"])
def test_terminal_capability_failures_do_not_retry_through_aliases(code):
    provider = Provider()
    audit = DataAcquisitionResult(capability_errors={
        capability: {"code": code, "retryable": False} for capability in ("quote", "industry", "news", "event", "institutional_research")})
    calls = gap_calls(AutomatedResearchPipeline(provider), request(provider), Intent.SECURITY_RESEARCH, "示例科技", audit)
    assert not any(call.method in {"get_quote", "get_industry_fundamentals", "get_industry_policy",
        "get_macro_policy", "get_market_breadth", "get_industry_turnover_history", "get_governance_disclosures",
        "get_stock_disclosure_details", "get_institutional_research"} for call in calls)


def test_unrelated_industry_ranking_cannot_replace_company_industry_metadata():
    provider = Provider()
    from backend.app.services.on_demand_research import scopes
    pipe = AutomatedResearchPipeline(provider)
    req = request(provider)
    assert scopes(pipe, req, Intent.SECURITY_RESEARCH, "示例科技")[1] is None
    req.facts.append(provider.fact("示例科技", "industry", "制造业", code="600001.SH"))
    assert scopes(pipe, req, Intent.SECURITY_RESEARCH, "示例科技")[1] == "制造业"


@pytest.mark.asyncio
async def test_exact_code_documents_are_assessed_for_returned_security_name():
    provider = Provider()
    class CodeDocuments(Provider):
        async def get_governance_disclosures(self, query):
            return [provider.fact("600001", "announcement",
                "审计为无保留意见；报告明确列出监管状态和按时披露情况。", document=True)]
        async def get_stock_disclosure_details(self, query):
            return await self.get_governance_disclosures(query)
    req = request(provider).model_copy(update={"query": "请研究600001"})
    output, audit = await recover_research(AutomatedResearchPipeline(CodeDocuments()), req,
        Intent.SECURITY_RESEARCH, None, DataAcquisitionResult(), target="600001", semantic=Assessor())
    source = next(f for f in output.facts if f.fact_id == "D-600001-announcement-")
    assert source.entity == "示例科技" and source.source_url and source.period
    governance = next(f for f in output.facts if f.entity == "示例科技" and f.field == "governance_score")
    assert governance.derived_from
    assert "governance_score" not in audit.missing_fields_by_agent.get("security", [])


def test_code_alias_alignment_preserves_unknown_names_and_conflicting_codes():
    from backend.app.services.on_demand_research import align_security_documents
    provider = Provider()
    docs = [provider.fact("600002", "announcement", "另一股票", document=True),
            provider.fact("600001", "news", "代码冲突", code="600002.SH", document=True),
            provider.fact("模糊名称", "announcement", "未知对象", document=True),
            provider.fact("600001.SH", "research_report", "同一证券", document=True)]
    req = request(provider).model_copy(update={"query": "研究600001", "facts": [*docs, *provider.initial()]})
    output = align_security_documents(AutomatedResearchPipeline(provider), req, Intent.SECURITY_RESEARCH, "600001")
    assert [f.entity for f in output.facts[:4]] == ["600002", "600001", "模糊名称", "示例科技"]
    assert [f.fact_id for f in output.facts] == [f.fact_id for f in req.facts]


@pytest.mark.asyncio
async def test_real_gateway_currency_and_report_date_allow_forecast_without_losing_record_identity():
    provider = IwencaiSkillHubProvider("test-only")
    today = datetime.now(timezone.utc).date()
    try:
        raw = {"datas": [{"证券简称": "示例科技", "证券代码": "600001.SH", "最新价": 100,
                          "单位": "元", "交易日期": today.isoformat()},
                         {"证券简称": "示例科技", "证券代码": "600001.SH", "目标价": 120,
                          "报告期": "2026Q4",
                          "目标价币种": "人民币", "研报发布日期": today.isoformat(),
                          "研报链接": "https://example.com/report"}]}
        facts = provider._normalize(raw, entity_hint="600001")
        target = next(f for f in facts if f.field == "target_price")
        assert target.unit == "CNY" and target.observation_date == today and target.period.startswith("REC-")
        forecast = build_return_expectation(facts, [f.fact_id for f in facts], request(Provider()).profile, "PASS")
        assert forecast.status == "scenario" and forecast.scenarios[0].price_return == pytest.approx(.2)
    finally:
        await provider.aclose()


@pytest.mark.parametrize("record", [{"目标价": 120, "单位": "元", "目标价币种": "美元"}, {"目标价": 120}])
def test_conflicting_or_missing_currency_does_not_get_guessed(record):
    provider = IwencaiSkillHubProvider("test-only")
    facts = provider._normalize({"datas": [record]}, entity_hint="示例科技")
    target = next(f for f in facts if f.field == "target_price")
    assert target.unit in {None, "conflicting"}


def test_http_analysis_automatically_recovers_and_verifies_return_panel(monkeypatch):
    provider, assessor = Provider(), Assessor()
    monkeypatch.setattr(main, "research_pipeline", AutomatedResearchPipeline(provider))
    monkeypatch.setattr(main.coordinator.semantic, "assess_stock_evidence", assessor.assess_stock_evidence)
    response = TestClient(main.app).post("/api/v1/portfolio/analyze", json=request(provider).model_dump(mode="json"))
    assert response.status_code == 200
    body = response.json()
    assert body["data_acquisition"]["assessment_status"] == "completed"
    assert body["data_acquisition"]["missing_fields_by_agent"] == {}
    assert body["return_expectation"]["status"] == "scenario"
    assert set(body["return_expectation"]["scenarios"][0]["evidence"]) <= set(body["evidence"])
    assert body["compliance"]["status"] == "PASS"


@pytest.mark.asyncio
async def test_unauthorized_live_shape_stops_without_exposing_credentials():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(401, json={"message": "invalid token", "token": "PRIVATE_TEST_SECRET"})
    provider = IwencaiSkillHubProvider("test-only", transport=httpx.MockTransport(handler), max_retries=0)
    try:
        with pytest.raises(ProviderCallError):
            await provider.get_quote("600001")
        req = request(Provider())
        audit = DataAcquisitionResult(capability_errors={"quote": {"code": "AUTHENTICATION_REJECTED", "retryable": False}})
        output, audit = await recover_research(AutomatedResearchPipeline(provider), req, Intent.SECURITY_RESEARCH,
                                             None, audit, target="示例科技", semantic=Assessor())
        assert output is req and audit.recovery_rounds == 0 and len(calls) == 1
        assert "PRIVATE_TEST_SECRET" not in audit.model_dump_json()
    finally:
        await provider.aclose()
