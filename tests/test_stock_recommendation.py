"""Recommendation dispatch, profile matching and adversarial evidence boundaries."""
import asyncio
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest
import httpx

from backend.app import main
from backend.app.agents.coordinator import CoordinatorAgent, basic_compliance_check, verify_facts
from backend.app.agents.rule_agents import make_rule_agents
from backend.app.models import FactRecord, Intent, OrchestrationRequest
from backend.app.data_provider.iwencai import IwencaiSkillHubProvider
from backend.app.services.provider_errors import ProviderCallError
from backend.app.semantic import RequestUnderstanding, SemanticService
from backend.app.services.evidence_coverage import AGENT_FIELDS
from backend.app.services.research import AutomatedResearchPipeline
from backend.app.services.stock_recommendation import StockRecommendationService
from backend.app.services.history import summarise_advice, used_fact_ids_of


def request(**profile_changes):
    profile = {"confirmed": True, "risk_level": "R3", "horizon_months": 24,
               "max_drawdown": .20, "liquidity_need": "中", "preferred_product_types": ["权益类"]}
    return OrchestrationRequest(query="推荐几支符合我的投资偏好的股票", research_direction="个股研究",
                                profile={**profile, **profile_changes})


def understanding(**changes):
    return RequestUnderstanding.model_validate({"intent": "security_research", "confidence": .95,
        "risk_rules": [], "reason": "按确认画像挑选股票", "action": "recommend_stocks",
        "recommendation_count": 2, **changes})


class Model:
    def __init__(self, *, action="recommend_stocks", bogus=False, constraint_result="yes", final_block=False):
        self.action, self.bogus, self.constraint_result, self.final_block = action, bogus, constraint_result, final_block
        self.calls = []

    async def complete_json(self, *, system, payload):
        title = payload["required_schema"]["title"]
        self.calls.append((title, payload))
        if title == "RequestUnderstanding":
            return understanding(action=self.action).model_dump(mode="json")
        if title == "OutputReview":
            is_final = any("建议关注以下股票" in r.get("opinion", "") for r in payload.get("results", []))
            return {"confidence": .95, "risk_rules": ["NO_RETURN_PROMISE"] if self.final_block and is_final else [],
                    "conflicting_agents": [], "reason": "测试风险检查"}
        facts = payload["authorized_facts"]
        assessments = []
        labels = {"policy": "supportive", "event": "neutral", "audit_opinion": "unqualified",
                  "regulatory_status": "explicitly_clear", "disclosure_status": "timely"}
        for target in payload["targets"]:
            document = next((f for f in facts if f["entity"] == target["entity"] and f["field"] in {"news", "announcement"}), None)
            if document is None:
                continue
            criteria = {"policy": ["policy"], "event": ["event"],
                        "governance": ["audit_opinion", "regulatory_status", "disclosure_status"]}[target["dimension"]]
            assessments.append({**target, "complete": True, "items": [
                {"criterion": criterion, "label": labels[criterion], "evidence_id": "invented" if self.bogus else document["fact_id"],
                 "quote": document["value"]} for criterion in criteria]})
        candidate = next((f for f in facts if f["entity"] == payload["candidate_entity"]), None)
        matches = [{"condition_index": i, "result": self.constraint_result,
                    "evidence_id": candidate["fact_id"] if candidate else None,
                    "quote": str(candidate["value"]) if candidate else None} for i in range(len(payload["conditions"]))]
        return {"confidence": .95, "assessments": assessments, "matches": matches}


class Provider:
    source_id = "IWENCAI_TEST"

    def __init__(self, *, qualitative=False, mismatch=False, drawdown=None, fail_symbol=None, delay=0):
        self.qualitative, self.mismatch = qualitative, mismatch
        self.drawdown = drawdown or {}
        self.fail_symbol, self.delay = fail_symbol, delay
        self.calls = []
        self.clock = datetime.now(timezone.utc)

    def fact(self, entity, field, value, *, code=None, unit=None, document=False):
        return FactRecord(fact_id=f"TEST-{entity}-{field}", entity=entity, field=field, value=value,
            entity_code=code, unit=unit, snapshot_time=self.clock, source_id=self.source_id, quality=.95,
            period="2026Q3", source_url="https://example.com/disclosure" if document else None)

    def scores(self, entity, agent, code=None):
        return [self.fact(entity, f, (70 if f == "fundamental_score" and code == "600002" else
                     65 if f == "fundamental_score" else 75 if f == "governance_score" else 55), code=code)
                for f in AGENT_FIELDS[agent]
                if not (self.qualitative and f in {"policy_score", "event_score", "governance_score"})]

    async def screen_stocks(self, query):
        self.calls.append(("screen", query))
        return [self.fact("示例" + code, "pe_ttm", 20, code=code) for code in ("600001", "600002")]

    async def get_basic_info(self, code):
        self.calls.append(("basic", code))
        return [self.fact("示例" + code, "industry", "制造业", code=code)]

    async def get_macro_data(self, query):
        self.calls.append(("macro", query))
        return self.scores("中国宏观经济", "market")

    async def get_industry_rank(self, industry):
        self.calls.append(("industry", industry))
        return self.scores(industry, "industry")

    async def get_news(self, entity):
        self.calls.append(("news", entity))
        return [self.fact(entity, "news", "政策明确支持行业发展。", document=True)]

    async def get_quote(self, code):
        self.calls.append(("quote", code))
        if self.delay:
            await asyncio.sleep(self.delay)
        if code == self.fail_symbol:
            raise RuntimeError("secret provider body")
        returned = "600099" if self.mismatch else code
        return [self.fact("示例" + returned, "close_price", 20, code=returned),
                self.fact("示例" + returned, "technical_score", 55, code=returned)]

    async def get_financial_metrics(self, code):
        self.calls.append(("financial", code))
        return [f for f in self.scores("示例" + code, "security", code) if f.field != "technical_score"]

    async def get_event_data(self, code):
        self.calls.append(("event", code))
        return [self.fact("示例" + code, "announcement",
                         "中性事件；无保留审计意见；监管资料明确无处罚；及时披露。", code=code, document=True)]

    async def get_announcements(self, code):
        self.calls.append(("announcement", code))
        return await self.get_event_data(code)

    async def get_stock_risk_metrics(self, code):
        self.calls.append(("risk", code))
        return [self.fact("示例" + code, field, value, code=code, unit=unit) for field, value, unit in (
            ("max_drawdown_1y", self.drawdown.get(code, 10), "percent"),
            ("avg_turnover_20d", 100_000_000, "CNY"), ("is_st", False, None), ("trading_status", "正常交易", None))]


def service(provider, model=None, **options):
    semantic = SemanticService(model or Model())
    coordinator = CoordinatorAgent(make_rule_agents(), verify_facts, basic_compliance_check, semantic=semantic)
    return StockRecommendationService(AutomatedResearchPipeline(provider), coordinator, **options)


@pytest.mark.asyncio
async def test_profile_directed_screening_fans_out_and_ranks_verified_stocks():
    provider = Provider()
    prepared, audit, advice = await service(provider).run(request(), understanding())
    assert advice.intent is Intent.SECURITY_RESEARCH
    assert advice.compliance.status == "PASS", advice.model_dump()
    recommendations = advice.stock_recommendation.recommendations
    assert [r.symbol for r in recommendations] == ["600002", "600001"]
    assert "600002" in advice.conclusion and "600001" in advice.conclusion
    query = next(q for method, q in provider.calls if method == "screen")
    assert "小于40" in query and "大于8%" in query and "最近三年归母净利润均为正" in query
    criteria = advice.stock_recommendation.screening_conditions
    assert criteria["max_drawdown"] == .20 and criteria["min_avg_turnover_20d_cny"] == 20_000_000
    assert criteria["risk_level"] == "R3" and criteria["horizon_months"] == 24
    assert all(("quote", r.symbol) in provider.calls and ("financial", r.symbol) in provider.calls for r in recommendations)
    assert sum(method == "macro" for method, _ in provider.calls) == 1
    assert sum(method == "industry" for method, _ in provider.calls) == 1
    assert set(advice.evidence) <= {f.fact_id for f in prepared.facts}
    assert audit.provider == "IWENCAI_TEST" and audit.fetched_fact_count > 0


@pytest.mark.asyncio
async def test_evidence_assessments_generate_traceable_missing_dimensions():
    prepared, _, advice = await service(Provider(qualitative=True)).run(request(), understanding())
    assert len(advice.stock_recommendation.recommendations) == 2, advice.model_dump()
    assessments = [f for f in prepared.facts if f.source_id == "DERIVED_STOCK_ASSESSMENT_V1"]
    assert {f.field for f in assessments} >= {"policy_assessment", "event_assessment", "governance_assessment"}
    assert all(f.derived_from and f.derivation_rule for f in assessments)


@pytest.mark.asyncio
async def test_bogus_assessment_references_never_complete_missing_evidence():
    _, _, advice = await service(Provider(qualitative=True), Model(bogus=True)).run(request(), understanding())
    assert advice.compliance.status == "REVIEW"
    assert not advice.stock_recommendation.recommendations


@pytest.mark.asyncio
async def test_excess_drawdown_excludes_one_stock_without_discarding_verified_peer():
    _, _, advice = await service(Provider(drawdown={"600001": 35})).run(request(), understanding())
    assert [r.symbol for r in advice.stock_recommendation.recommendations] == ["600002"]
    assert next(c for c in advice.stock_recommendation.candidates if c.symbol == "600001").status == "excluded"


@pytest.mark.asyncio
async def test_negative_drawdown_representation_uses_absolute_loss_magnitude():
    _, _, advice = await service(Provider(drawdown={"600001": -35, "600002": -10})).run(request(), understanding())
    assert [r.symbol for r in advice.stock_recommendation.recommendations] == ["600002"]


@pytest.mark.asyncio
async def test_history_preserves_recommendations_and_excluded_candidate_evidence():
    prepared, _, advice = await service(Provider(drawdown={"600001": 35})).run(request(), understanding())
    payload = advice.model_copy(update={"facts": prepared.facts}).model_dump(mode="json")
    used = used_fact_ids_of(payload)
    summary = summarise_advice(payload, used)
    assert summary["stock_recommendation"]["recommendations"][0]["symbol"] == "600002"
    assert "TEST-示例600001-max_drawdown_1y" in used
    assert not summary["missing_evidence_ids"]


@pytest.mark.asyncio
async def test_conflicting_risk_metrics_exclude_only_the_affected_stock():
    class ConflictingProvider(Provider):
        async def get_stock_risk_metrics(self, code):
            facts = await super().get_stock_risk_metrics(code)
            if code == "600001":
                facts.append(facts[0].model_copy(update={"fact_id": "CONFLICT-DD", "value": 15}))
            return facts
    _, _, advice = await service(ConflictingProvider()).run(request(), understanding())
    assert [r.symbol for r in advice.stock_recommendation.recommendations] == ["600002"]


@pytest.mark.asyncio
async def test_basic_information_permission_failure_is_not_retried_or_hidden():
    class DeniedProvider(Provider):
        async def get_basic_info(self, code):
            self.calls.append(("basic", code))
            raise ProviderCallError("private credential context", code="AUTHENTICATION_REJECTED", status_code=401)
    provider = DeniedProvider()
    _, audit, advice = await service(provider).run(request(), understanding())
    assert sum(method == "basic" for method, _ in provider.calls) == 2
    assert not advice.stock_recommendation.recommendations
    assert all(error["retryable"] is False for error in audit.capability_errors.values())
    assert all("未获授权" in entry.reasons[0] for entry in advice.stock_recommendation.candidates)
    assert "private credential" not in audit.model_dump_json()


def test_risk_adapter_normalizes_percent_currency_and_industry_fields():
    provider = IwencaiSkillHubProvider("test-only")
    facts = provider._normalize({"datas": [{"股票代码": "600001.SH", "股票简称": "示例公司",
        "近一年最大回撤(%)": -12.5, "近20日平均成交额(万元)": 5000,
        "是否ST": False, "交易状态": "正常交易", "所属行业": "制造业"}]}, entity_hint="600001")
    by_field = {f.field: f for f in facts}
    assert by_field["max_drawdown_1y"].normalized_value == -12.5
    assert by_field["avg_turnover_20d"].unit == "万元"
    assert by_field["industry"].value == "制造业"
    assert all(f.entity_code == "600001.SH" for f in facts)


@pytest.mark.asyncio
async def test_stock_risk_metrics_uses_fixed_symbol_query_with_market_skill():
    captured = []
    def handler(req):
        captured.append(req)
        return httpx.Response(200, json={"datas": [{"股票代码": "600001", "近一年最大回撤(%)": 10}]})
    provider = IwencaiSkillHubProvider("test-only", transport=httpx.MockTransport(handler))
    facts = await provider.get_stock_risk_metrics("600001")
    assert facts and facts[0].field == "max_drawdown_1y"
    assert "600001" in captured[0].content.decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["no", "unknown"])
async def test_hard_profile_constraints_require_positive_evidence(result):
    provider = Provider()
    _, _, advice = await service(provider, Model(constraint_result=result)).run(
        request(constraints=["只研究制造业"]), understanding())
    assert "只研究制造业" in provider.calls[0][1]
    assert not advice.stock_recommendation.recommendations


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unconfirmed", "incomplete", "product", "opt_out", "provider", "risk", "direction", "low_confidence"])
async def test_no_external_reads_before_required_gates(kind):
    provider = Provider()
    req, understood = request(), understanding()
    if kind == "unconfirmed": req.profile.confirmed = False
    if kind == "incomplete": req.profile.horizon_months = None
    if kind == "product": req.profile.preferred_product_types = ["固定收益类"]
    if kind == "opt_out": req.auto_fetch = False
    if kind == "risk": understood.risk_rules = ["NO_RETURN_PROMISE"]
    if kind == "direction": req.research_direction = "基金筛选"
    if kind == "low_confidence": understood.confidence = .2
    _, _, advice = await service(None if kind == "provider" else provider).run(req, understood)
    assert not provider.calls
    assert advice.compliance.status != "PASS"


@pytest.mark.asyncio
async def test_mismatched_security_quote_cannot_be_used_for_recommendation():
    _, _, advice = await service(Provider(mismatch=True)).run(request(), understanding())
    assert not advice.stock_recommendation.recommendations


@pytest.mark.asyncio
async def test_industry_scores_cannot_substitute_for_missing_stock_research():
    class IndustryOnlyProvider(Provider):
        async def get_quote(self, code):
            return [self.fact("示例" + code, "close_price", 20, code=code)]
        async def get_financial_metrics(self, code):
            return []
        async def get_industry_rank(self, industry):
            return self.scores(industry, "industry") + self.scores(industry, "security")
    _, _, advice = await service(IndustryOnlyProvider()).run(request(), understanding())
    assert not advice.stock_recommendation.recommendations


@pytest.mark.asyncio
async def test_listing_status_alone_does_not_confirm_normal_trading():
    class ListingProvider(Provider):
        async def get_stock_risk_metrics(self, code):
            facts = await super().get_stock_risk_metrics(code)
            return [f.model_copy(update={"value": "上市"}) if f.field == "trading_status" else f for f in facts]
    _, _, advice = await service(ListingProvider()).run(request(), understanding())
    assert not advice.stock_recommendation.recommendations
    assert all(c.status == "review" for c in advice.stock_recommendation.candidates)


@pytest.mark.asyncio
async def test_single_candidate_provider_failure_isolated_and_sanitized():
    _, audit, advice = await service(Provider(fail_symbol="600001")).run(request(), understanding())
    assert [r.symbol for r in advice.stock_recommendation.recommendations] == ["600002"]
    assert "quote:600001" in audit.failed_capabilities
    assert "secret provider body" not in advice.model_dump_json() + audit.model_dump_json()


@pytest.mark.asyncio
async def test_bounded_deadline_reports_pending_candidates_without_recommending_them():
    backend = service(Provider(delay=.1), timeout_seconds=.02)
    _, audit, advice = await backend.run(request(), understanding())
    # Shared public-cache loaders intentionally outlive a canceled waiter. The
    # application closes them on shutdown; close the test-owned pipeline too.
    await backend.pipeline.cache.aclose()
    assert not advice.stock_recommendation.recommendations
    assert all(c.status == "review" for c in advice.stock_recommendation.candidates)
    assert "RESEARCH_TIMEOUT" in audit.model_dump_json()


@pytest.mark.asyncio
async def test_final_semantic_block_withdraws_every_previously_selected_stock():
    _, _, advice = await service(Provider(), Model(final_block=True)).run(request(), understanding())
    assert advice.compliance.status == "BLOCK"
    assert not advice.stock_recommendation.recommendations
    assert all(c.status != "recommended" for c in advice.stock_recommendation.candidates)


@pytest.mark.asyncio
async def test_model_dispatch_respects_analysis_action_even_when_query_contains_recommend():
    semantic = SemanticService(Model(action="analyze"))
    req = request()
    result = await semantic.understand(req)
    assert result.action == "analyze"
    assert not StockRecommendationService.should_run(req, result)


def test_existing_analyze_endpoint_dispatches_recommendation_with_no_new_frontend_type(monkeypatch):
    provider = Provider()
    backend = service(provider)
    monkeypatch.setattr(main, "coordinator", backend.coordinator)
    monkeypatch.setattr(main, "research_pipeline", backend.pipeline)
    body = TestClient(main.app).post("/api/v1/portfolio/analyze", json=request().model_dump(mode="json")).json()
    assert body["intent"] == "security_research"
    assert body["stock_recommendation"]["recommendations"]
    assert "600002" in body["conclusion"]
    assert body["profile_version"] == 1
    assert body["data_acquisition"]["mode"] == "live"
