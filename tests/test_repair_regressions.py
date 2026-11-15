"""Regression coverage for the October full-run findings; no live services."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from time import perf_counter

import httpx
import pytest
from fastapi import HTTPException

from backend.app import main
from backend.app.agents.coordinator import semantic_compliance, verify_facts
from backend.app.agents.llm_agents import HybridInvestmentAgent, LLMConfig, OpenAICompatibleLLM
from backend.app.agents.rule_agents import make_rule_agents
from backend.app.models import AgentResult, DataAcquisitionResult, FactRecord, Intent
from backend.app.services.model_telemetry import analysis_telemetry, mark_model_result
from backend.app.services.on_demand_research import read_gap_documents
from backend.app.services.research import AutomatedResearchPipeline
from backend.app.services.research_admission import ResearchAdmission
from backend.app.services.research_recovery import recover_research
from backend.app.services.model_input import slice_facts_for_agent
from frontend.api_client import AnalysisProgress, request_analysis
from tests.test_semantic import service, request
from tests.test_research_recovery import Provider, facts_for, request as research_request, advice_for


@pytest.mark.asyncio
@pytest.mark.parametrize("rules,expected", [([], "REVIEW"), (["NO_RETURN_PROMISE"], "BLOCK")])
async def test_uncertain_review_keeps_reason_and_hard_blocks(rules, expected):
    semantic, _ = service({"confidence": .3, "risk_rules": rules,
        "conflicting_agents": [], "reason": "证据不足，需要核实行业来源"})
    collector = {"calls": []}
    token = analysis_telemetry.set(collector)
    try:
        review = await semantic.review(request(), [])
        assert review is not None and "行业来源" in review.reason
        assert "SEMANTIC_REVIEW_UNCERTAIN" in review.risk_rules
        assert semantic_compliance(review.risk_rules, review.reason).status == expected
        assert collector["calls"][-1]["reason_code"] == "MODEL_LOW_CONFIDENCE"
        assert collector["calls"][-1]["status"] == "uncertain"
    finally:
        analysis_telemetry.reset(token)
        await semantic.llm.aclose()


def test_contract_diagnostics_update_the_matching_parallel_role():
    collector = {"calls": [{"purpose": "security"}, {"purpose": "market"}]}
    token = analysis_telemetry.set(collector)
    try:
        mark_model_result("security", reason_code="MODEL_UNKNOWN_REFERENCE")
        assert collector["calls"][0]["reason_code"] == "MODEL_UNKNOWN_REFERENCE"
        assert "reason_code" not in collector["calls"][1]
    finally:
        analysis_telemetry.reset(token)


def test_specialist_model_excludes_stale_extra_quotes_and_retains_audit_evidence():
    old = FactRecord(fact_id="old-quote", entity="示例科技", field="close_price", value=50,
        snapshot_time=datetime.now(timezone.utc) - timedelta(days=5), source_id="TEST", quality=.9)
    current = old.model_copy(update={"fact_id": "current-quote", "snapshot_time": datetime.now(timezone.utc), "value": 60})
    baseline = facts_for("security")[0]
    facts = [old, current, baseline]
    selected = slice_facts_for_agent(facts, "security", [baseline.fact_id])
    assert {f.fact_id for f in selected} == {current.fact_id, baseline.fact_id}
    assert old in facts and old.value == 50


@pytest.mark.asyncio
async def test_successful_http_retry_clears_stale_failure_diagnostics():
    count = 0
    def handler(req):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})
    llm = OpenAICompatibleLLM(LLMConfig("https://model.test", "fake", "test", max_retries=1),
                             transport=httpx.MockTransport(handler))
    collector = {"calls": []}
    token = analysis_telemetry.set(collector)
    try:
        assert await llm.complete_json(system="test", payload={}) == {"ok": True}
        call = collector["calls"][0]
        assert call["attempts"] == 2 and call["status"] == "completed"
        assert "reason_code" not in call and "http_status" not in call
    finally:
        analysis_telemetry.reset(token)
        await llm.aclose()


@pytest.mark.asyncio
async def test_invalid_agent_reference_exposes_safe_cause_only():
    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "confidence": .8, "opinion": "private-user-output", "facts_used": ["private-unknown-id"]})}}]})
    llm = OpenAICompatibleLLM(LLMConfig("https://model.test", "fake", "test", max_retries=0),
                             transport=httpx.MockTransport(handler))
    collector = {"calls": []}
    token = analysis_telemetry.set(collector)
    try:
        req = research_request(facts_for("security"))
        result = await HybridInvestmentAgent("security", make_rule_agents()["security"], llm).run(req)
        assert result.details["engine"] == "rule_fallback"
        assert result.details["model_failure"]["reason_code"] == "MODEL_UNKNOWN_REFERENCE"
        assert "private-" not in result.model_dump_json() + json.dumps(collector["calls"])
    finally:
        analysis_telemetry.reset(token)
        await llm.aclose()


@pytest.mark.asyncio
async def test_rejected_evidence_retains_execution_diagnostics_but_removes_claims():
    result = AgentResult(agent_id="security", status="completed", opinion="unsupported", confidence=.9,
        facts_used=["missing"], score=90, details={"engine": "third_party_llm", "model": "test",
        "unsafe_target": 999, "model_failure": {"reason_code": "MODEL_SCHEMA_INVALID"}})
    verified = (await verify_facts([result], []))[0]
    assert verified.status == "degraded" and verified.score is None and not verified.facts_used
    assert verified.details["engine"] == "third_party_llm"
    assert verified.details["rejected_reference_reasons"] == ["UNKNOWN_REFERENCE"]
    assert "unsafe_target" not in verified.details and "unsupported" not in verified.opinion
    assert result.score == 90


@pytest.mark.asyncio
async def test_second_recovery_uses_only_unspent_budget():
    provider = Provider(delay=.12)
    pipeline = AutomatedResearchPipeline(provider, recovery_timeout_seconds=.10)
    req = research_request(facts_for("market", omitted=("liquidity_score",)))
    audit = DataAcquisitionResult(recovery_elapsed_ms=80)
    started = perf_counter()
    try:
        output, audit = await recover_research(pipeline, req, Intent.SECURITY_RESEARCH, advice_for(req), audit)
        assert output is req and audit.recovery_errors["macro"]["code"] == "RECOVERY_TIMEOUT"
        assert audit.recovery_elapsed_ms >= 100 and audit.recovery_budget_exhausted
        assert perf_counter() - started < .10
        calls = list(provider.calls)
        await recover_research(pipeline, req, Intent.SECURITY_RESEARCH, None, audit)
        assert provider.calls == calls
    finally:
        await pipeline.cache.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("same_identity", [True, False])
async def test_official_pdf_reuse_requires_matching_identity_and_lineage(monkeypatch, same_identity):
    import backend.app.services.on_demand_research as module
    now = datetime.now(timezone.utc)
    source = FactRecord(fact_id="official-title", entity="示例科技", entity_code="600001.SH",
        field="announcement", value="年度报告", period=now.date().isoformat(), snapshot_time=now,
        source_id="TEST", quality=.95, source_url="https://static.cninfo.com.cn/finalpage/2026/report.pdf")
    excerpt = source.model_copy(update={"fact_id": "verified-excerpt", "field": "announcement_excerpt",
        "source_id": "OFFICIAL_DISCLOSURE_PDF_V1", "derived_from": [source.fact_id],
        "entity_code": "600001.SH" if same_identity else "600002.SH", "value": "核验过的原文",
        "derivation_rule": "PDF_TEXT_V1: bounded original-page extraction; partial coverage"})
    req = research_request([source, excerpt])
    calls = []
    async def reader(*args, **kwargs):
        calls.append(kwargs)
        return []
    monkeypatch.setattr(module, "read_disclosure", reader)
    result = await read_gap_documents(AutomatedResearchPipeline(Provider()), req,
        Intent.SECURITY_RESEARCH, "示例科技", [source], timeout_seconds=1)
    assert len(calls) == (0 if same_identity else 1)
    assert bool(result[1]) == (not same_identity)


@pytest.mark.asyncio
async def test_analysis_deadline_cancels_work_and_releases_admission(monkeypatch):
    admission = ResearchAdmission(limit=1, queue_seconds=.01)
    monkeypatch.setattr(main, "research_admission", admission)
    monkeypatch.setattr(main, "analysis_timeout_seconds", .03)
    cancelled = asyncio.Event()
    async def slow_understand(req):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()
    monkeypatch.setattr(main.coordinator, "understand_request", slow_understand)
    with pytest.raises(HTTPException) as error:
        await main.analyze_portfolio(request(research_mode="fast"), None)
    assert error.value.status_code == 504 and cancelled.is_set()
    slot = await admission.acquire()
    slot.release()
    assert analysis_telemetry.get() is None


@pytest.mark.parametrize("channel", ["stream_event", "http"])
def test_frontend_honors_429_backoff_and_retries_only_once(monkeypatch, channel):
    delays, calls = [], []
    monkeypatch.setattr("frontend.api_client.sleep", delays.append)
    def handler(req):
        calls.append(req)
        if channel == "http":
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"detail": "繁忙"})
        return httpx.Response(200, text=json.dumps({"type": "error", "status_code": 429,
            "retry_after": "2", "message": "繁忙"}))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = request_analysis(client, "https://api.test", "POST", "/portfolio/analyze", {}, "fake", AnalysisProgress())
    assert len(calls) == 2 and delays == [2] and result.status == 429


def test_frontend_does_not_replay_a_timed_out_paid_analysis(monkeypatch):
    calls = []
    monkeypatch.setattr("frontend.api_client.sleep", lambda delay: pytest.fail("timeout must not retry"))
    def handler(req):
        calls.append(req)
        return httpx.Response(200, text=json.dumps({"type": "error", "status_code": 504, "message": "超时"}))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = request_analysis(client, "https://api.test", "POST", "/portfolio/analyze", {}, None, AnalysisProgress())
    assert len(calls) == 1 and result.status == 504


def test_frontend_delivers_the_result_after_admission_retry(monkeypatch):
    calls, delays = [], []
    monkeypatch.setattr("frontend.api_client.sleep", delays.append)
    def handler(req):
        calls.append(req)
        event = ({"type": "error", "status_code": 429, "retry_after": "2", "message": "繁忙"}
                 if len(calls) == 1 else {"type": "result", "advice": {"compliance": {"status": "REVIEW"}}})
        return httpx.Response(200, text=json.dumps(event))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = request_analysis(client, "https://api.test", "POST", "/portfolio/analyze", {}, "fake", AnalysisProgress())
    assert len(calls) == 2 and calls[0].content == calls[1].content and delays == [2]
    assert result.data["compliance"]["status"] == "REVIEW"
