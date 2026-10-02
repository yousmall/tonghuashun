"""Recovery tests use deterministic providers and never call external services."""
import asyncio
from datetime import datetime, timezone

from fastapi.testclient import TestClient
import pytest

import backend.app.main as main
from backend.app.agents.coordinator import cross_validate_results
from backend.app.models import (
    AdvicePackage, AgentResult, DataAcquisitionResult, FactRecord, Intent,
    OrchestrationRequest, TaskPlan,
)
from backend.app.services.evidence_coverage import AGENT_FIELDS
from backend.app.services.research import AutomatedResearchPipeline
from backend.app.services.research_recovery import recover_research


def facts_for(agent, *, omitted=()):
    return [FactRecord(fact_id=f"{agent}-{field}", entity=agent, field=field, value=55,
                       snapshot_time=datetime.now(timezone.utc), source_id="TEST", quality=.9)
            for field in AGENT_FIELDS[agent] if field not in omitted]


def request(facts):
    return OrchestrationRequest(query="请研究示例科技这只个股",
                                profile={"confirmed": True, "risk_level": "R3"}, facts=facts)


def advice_for(req, *, status="REVIEW", issues=()):
    return AdvicePackage(trace_id="test", intent=Intent.SECURITY_RESEARCH,
                         conclusion="待核验", confidence=0, compliance={"status": status},
                         task_plan=TaskPlan(trace_id="test", intent=Intent.SECURITY_RESEARCH),
                         agent_results=[AgentResult(agent_id="market", status="degraded",
                                                    opinion="缺项", confidence=0,
                                                    details={"missing_fields": ["liquidity_score"]})],
                         cross_validation={"issues": list(issues)})


class Provider:
    source_id = "TEST"

    def __init__(self, response=None, *, fail=False, delay=0):
        self.calls = []
        self.response = response
        self.fail, self.delay = fail, delay

    async def get_macro_data(self, scope):
        self.calls.append(scope)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("private provider error")
        return self.response if self.response is not None else facts_for("market")


@pytest.mark.asyncio
async def test_gap_refresh_bypasses_reuse_and_cache_and_stops_after_one_round():
    partial = facts_for("market", omitted=("liquidity_score",))
    provider = Provider()
    pipeline = AutomatedResearchPipeline(provider)
    req = request([*partial, *facts_for("industry"), *facts_for("security")])
    calls = pipeline._calls_for(req, Intent.SECURITY_RESEARCH, "示例科技")
    macro = next(call for call in calls if call.label == "macro")
    req.facts = [fact.model_copy(update={"produced_by": macro.key}) if fact.entity == "market" else fact
                 for fact in req.facts]
    import json
    cache_key = json.dumps([macro.method, macro.args], ensure_ascii=False, sort_keys=True)
    pipeline.cache.entries[cache_key] = partial
    output, audit = await recover_research(pipeline, req, Intent.SECURITY_RESEARCH,
                                           advice_for(req), DataAcquisitionResult())
    assert provider.calls == ["中国最新宏观经济"]
    assert "market" not in audit.missing_fields_by_agent
    assert audit.recovery_rounds == 1 and audit.recovery_successful_capabilities == ["macro"]
    assert any(fact.field == "liquidity_score" and fact.entity == "market" for fact in output.facts)
    await recover_research(pipeline, output, Intent.SECURITY_RESEARCH, advice_for(output), audit)
    assert len(provider.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["empty", "failure", "timeout"])
async def test_failed_recovery_preserves_report_and_does_not_expose_exception(case):
    provider = Provider(response=[], fail=case == "failure", delay=.1 if case == "timeout" else 0)
    pipeline = AutomatedResearchPipeline(provider, call_timeout_seconds=.01)
    req = request(facts_for("market", omitted=("liquidity_score",)))
    output, audit = await recover_research(pipeline, req, Intent.SECURITY_RESEARCH,
                                           advice_for(req), DataAcquisitionResult())
    assert output is req and audit.recovery_rounds == 1
    assert not audit.recovery_reanalyzed
    assert (audit.recovery_empty_capabilities if case == "empty" else audit.recovery_failed_capabilities) == ["macro"]
    assert "private provider error" not in audit.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["opt_out", "profile", "block", "no_results", "unknown", "no_provider"])
async def test_recovery_obeys_existing_gates(case):
    provider = Provider()
    req = request([])
    advice = advice_for(req, status="BLOCK" if case == "block" else "REVIEW")
    if case == "opt_out":
        req.auto_fetch = False
    if case == "profile":
        req.profile.confirmed = False
    if case == "no_results":
        advice.agent_results = []
    output, audit = await recover_research(AutomatedResearchPipeline(None if case == "no_provider" else provider),
                                           req, Intent.UNKNOWN if case == "unknown" else Intent.SECURITY_RESEARCH,
                                           advice, DataAcquisitionResult())
    assert output is req and audit.recovery_rounds == 0 and not provider.calls


@pytest.mark.asyncio
async def test_same_period_conflicting_source_records_are_retained_after_refresh():
    facts = [*facts_for("market"), *facts_for("industry"), *facts_for("security")]
    first = next(fact for fact in facts if fact.field == "fundamental_score")
    first.period = "2026Q2"
    second = first.model_copy(update={"fact_id": "contradiction", "value": 80})
    facts.append(second)
    result = AgentResult(agent_id="security", status="completed", opinion="测试", confidence=.8,
                         facts_used=[first.fact_id], score=55)
    cross = cross_validate_results([result], facts)
    provider = Provider()
    async def financial(scope):
        provider.calls.append(scope)
        return [first.model_copy(update={"fact_id": "fresh", "snapshot_time": datetime.now(timezone.utc)})]
    provider.get_financial_metrics = financial
    req = request(facts)
    advice = advice_for(req, issues=[issue.model_dump() for issue in cross.issues])
    advice.agent_results = [result]
    output, audit = await recover_research(AutomatedResearchPipeline(provider), req,
                                           Intent.SECURITY_RESEARCH, advice, DataAcquisitionResult())
    assert provider.calls == [req.query] and audit.recovery_rounds == 1
    assert {first.fact_id, second.fact_id, "fresh"} <= {fact.fact_id for fact in output.facts}
    assert "INTERNAL_VALUE_CONFLICT" in {issue.code for issue in cross_validate_results([result], output.facts).issues}


def test_endpoint_recovers_transient_macro_gap_before_running_all_gates(monkeypatch):
    class TransientProvider(Provider):
        async def get_macro_data(self, scope):
            self.calls.append(scope)
            return facts_for("market", omitted=("liquidity_score",) if len(self.calls) == 1 else ())
    provider = TransientProvider()
    calls = {"analysis": 0, "verification": 0, "review": 0}
    original_run, original_verify = main.coordinator.run, main.coordinator.verifier
    original_review = main.coordinator.semantic.review
    async def run(*args, **kwargs):
        calls["analysis"] += 1
        return await original_run(*args, **kwargs)
    async def verify(*args, **kwargs):
        calls["verification"] += 1
        return await original_verify(*args, **kwargs)
    async def review(*args, **kwargs):
        calls["review"] += 1
        return await original_review(*args, **kwargs)
    monkeypatch.setattr(main.coordinator, "run", run)
    monkeypatch.setattr(main.coordinator, "verifier", verify)
    monkeypatch.setattr(main.coordinator.semantic, "review", review)
    monkeypatch.setattr(main, "research_pipeline", AutomatedResearchPipeline(provider))
    req = request([*facts_for("industry"), *facts_for("security")])
    body = TestClient(main.app).post("/api/v1/portfolio/analyze", json=req.model_dump(mode="json")).json()
    assert provider.calls == ["中国最新宏观经济", "中国最新宏观经济"]
    assert body["data_acquisition"]["recovery_rounds"] == 1
    assert not body["data_acquisition"]["recovery_reanalyzed"]
    assert body["data_acquisition"]["recovery_phase"] == "before_analysis"
    assert body["data_acquisition"]["missing_fields_by_agent"] == {}
    assert body["compliance"]["status"] == "PASS"
    assert body["cross_validation"]["status"] == "PASS"
    assert all(result["status"] == "completed" for result in body["agent_results"])
    assert calls == {"analysis": 1, "verification": 1, "review": 1}
    assert set(body["evidence"]) <= {fact["fact_id"] for fact in body["facts"]}


@pytest.mark.asyncio
async def test_preflight_refresh_updates_public_cache_and_coalesces_other_users():
    provider = Provider(response=facts_for("market", omitted=("liquidity_score",)), delay=.02)
    pipeline = AutomatedResearchPipeline(provider)
    req = request([*facts_for("market", omitted=("liquidity_score",)),
                   *facts_for("industry"), *facts_for("security")])
    first, second = await asyncio.gather(*(
        recover_research(pipeline, req.model_copy(update={"profile": req.profile.model_copy(
            update={"user_id": user})}), Intent.SECURITY_RESEARCH, None, DataAcquisitionResult())
        for user in ("one", "two")))
    assert len(provider.calls) == 1
    assert first[0].profile.user_id == "one" and second[0].profile.user_id == "two"
    assert sorted([first[1].recovery_cached_capabilities, second[1].recovery_cached_capabilities]) == [[], ["macro"]]
    # The cooldown avoids another public read but never invents the absent score.
    output, audit = await recover_research(pipeline, req, Intent.SECURITY_RESEARCH, None, DataAcquisitionResult())
    assert len(provider.calls) == 1 and audit.recovery_cached_capabilities == ["macro"]
    assert audit.missing_fields_by_agent == {"market": ["liquidity_score"]}
    assert audit.recovery_phase == "before_analysis" and not audit.recovery_reanalyzed
    assert "liquidity_score" not in {fact.field for fact in output.facts}
    # A post-analysis rejection/conflict still forces a fresh read.
    await recover_research(pipeline, req, Intent.SECURITY_RESEARCH, advice_for(req), DataAcquisitionResult())
    assert len(provider.calls) == 2


def test_complete_evidence_conflict_still_triggers_post_analysis_repair(monkeypatch):
    facts = [*facts_for("market"), *facts_for("industry"), *facts_for("security")]
    source = next(fact for fact in facts if fact.field == "fundamental_score")
    source.period = "2026Q2"
    facts.append(source.model_copy(update={"fact_id": "contradiction", "value": 90}))
    provider = Provider()
    async def financial(scope):
        provider.calls.append(scope)
        return [source.model_copy(update={"fact_id": "refresh"})]
    provider.get_financial_metrics = financial
    monkeypatch.setattr(main, "research_pipeline", AutomatedResearchPipeline(provider))
    body = TestClient(main.app).post("/api/v1/portfolio/analyze", json=request(facts).model_dump(mode="json")).json()
    audit = body["data_acquisition"]
    assert audit["recovery_phase"] == "after_analysis" and audit["recovery_reanalyzed"]
    assert audit["recovery_rounds"] == 1
    assert audit["recovery_capabilities"] == ["financial"]
    assert provider.calls.count("中国最新宏观经济") == 1
    assert len(provider.calls) == 3  # initial macro/financial, then financial repair
    assert body["compliance"]["status"] == "REVIEW"
    assert "INTERNAL_VALUE_CONFLICT" in {issue["code"] for issue in body["cross_validation"]["issues"]}
    assert {source.fact_id, "contradiction", "refresh"} <= {fact["fact_id"] for fact in body["facts"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["empty", "failure", "timeout", "opt_out", "profile", "unknown", "education"])
async def test_preflight_preserves_gates_and_bounded_failure(case):
    provider = Provider(response=[], fail=case == "failure", delay=.1 if case == "timeout" else 0)
    pipeline = AutomatedResearchPipeline(provider, call_timeout_seconds=.01)
    req = request([])
    if case == "opt_out":
        req.auto_fetch = False
    if case == "profile":
        req.profile.confirmed = False
    intent = Intent.UNKNOWN if case == "unknown" else Intent.EDUCATION if case == "education" else Intent.SECURITY_RESEARCH
    output, audit = await recover_research(pipeline, req, intent, None, DataAcquisitionResult())
    assert output is req and not audit.recovery_reanalyzed
    assert audit.recovery_rounds == (1 if case in {"empty", "failure", "timeout"} else 0)
    assert len(provider.calls) <= 1
