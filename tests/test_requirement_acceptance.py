"""技术要求的故障边界、完整计时与协作展示验收。"""
import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from backend.app import main
from backend.app.agents.coordinator import CoordinatorAgent, basic_compliance_check, verify_facts
from backend.app.agents.rule_agents import make_rule_agents
from backend.app.models import FactRecord, Intent, OrchestrationRequest
from backend.app.services.history import summarise_advice, used_fact_ids_of
from backend.app.services.monitoring import RequestMetricsMiddleware, ServiceMetrics
from backend.app.services.research import AutomatedResearchPipeline
from frontend.presentation import collaboration_rows, source_trace_rows


def request():
    return OrchestrationRequest(
        query="请诊断我的持仓组合", profile={"risk_level": "R3", "confirmed": True},
        auto_fetch=False, facts=[FactRecord(
            fact_id="ACCEPT-WEIGHT", entity="合成ETF", field="weight", value=0.2,
            snapshot_time=datetime.now(timezone.utc), source_id="SYNTHETIC_ACCEPTANCE", quality=0.95,
        )],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["fact_verifier", "compliance"])
@pytest.mark.parametrize("failure", ["exception", "timeout"])
async def test_gate_failure_returns_review_without_unchecked_opinions(semantic, gate, failure):
    async def fail(*args):
        if failure == "timeout":
            await asyncio.sleep(1)
        raise RuntimeError("SENSITIVE_INTERNAL_ERROR")

    coordinator = CoordinatorAgent(
        make_rule_agents(), fail if gate == "fact_verifier" else verify_facts,
        fail if gate == "compliance" else basic_compliance_check, semantic=semantic,
    )
    original = coordinator.plan

    def plan(*args, **kwargs):
        result = original(*args, **kwargs)
        for node in result.nodes:
            if node.agent_id == gate:
                node.timeout_seconds = 0.02
        return result

    coordinator.plan = plan
    result = await asyncio.wait_for(coordinator.run(request()), timeout=0.5)
    assert result.compliance.status == "REVIEW"
    assert not result.evidence and not result.allocation
    assert all(not item.facts_used and item.score is None and item.confidence == 0 for item in result.agent_results)
    assert "SENSITIVE_INTERNAL_ERROR" not in result.model_dump_json()
    nodes = {node.agent_id: node for node in result.task_plan.nodes}
    assert nodes[gate].status == ("degraded" if failure == "timeout" else "failed")
    if gate == "fact_verifier":
        assert nodes["compliance"].status == "skipped"


@pytest.mark.asyncio
async def test_semantic_review_uses_compliance_total_budget(semantic):
    async def slow(*args):
        await asyncio.sleep(1)

    semantic.review = slow
    coordinator = CoordinatorAgent(make_rule_agents(), verify_facts, basic_compliance_check, semantic=semantic)
    coordinator._llm_node_timeout = 0.02
    result = await asyncio.wait_for(coordinator.run(request()), timeout=0.5)
    assert result.compliance.matched_rules == ["COMPLIANCE_UNAVAILABLE"]
    assert not result.evidence


@pytest.mark.asyncio
async def test_slow_data_capability_does_not_discard_other_results():
    class Provider:
        source_id = "SYNTHETIC_ACCEPTANCE"

        async def get_macro_data(self, query):
            await asyncio.sleep(1)

        async def get_industry_rank(self, query):
            return request().facts

        async def get_news(self, query):
            return []

    pipeline = AutomatedResearchPipeline(Provider(), call_timeout_seconds=0.02)
    req = request().model_copy(update={"auto_fetch": True})
    prepared, audit = await asyncio.wait_for(pipeline.prepare(req, Intent.MARKET_ANALYSIS), timeout=0.5)
    assert audit.failed_capabilities == ["macro"]
    assert audit.successful_capabilities == ["industry"]
    assert audit.empty_capabilities == ["news"]
    assert prepared.facts


@pytest.mark.asyncio
async def test_stream_transport_metric_waits_for_last_body():
    metrics = ServiceMetrics()

    async def streamed_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"progress", "more_body": True})
        assert metrics.request_count == 0
        await asyncio.sleep(0.025)
        await send({"type": "http.response.body", "body": b"result", "more_body": False})

    async def send(message):
        pass

    await RequestMetricsMiddleware(streamed_app, metrics)({"type": "http"}, None, send)
    assert metrics.request_count == 1
    assert metrics.snapshot()["p50_latency_ms"] >= 20


@pytest.mark.asyncio
async def test_analysis_counts_full_runtime_and_review_separately(monkeypatch):
    metrics = ServiceMetrics()
    monkeypatch.setattr(main, "service_metrics", metrics)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        result = await client.post("/api/v1/portfolio/analyze", json=request().model_dump(mode="json"))
        assert result.status_code == 200
        assert result.json()["timings_ms"]["total"] >= 0
        await client.post("/api/v1/portfolio/analyze", json=request().model_copy(
            update={"profile": request().profile.model_copy(update={"confirmed": False})}
        ).model_dump(mode="json"))
    snapshot = metrics.snapshot()["analysis"]
    assert snapshot["count"] == 2
    assert snapshot["outcomes"] == {"PASS": 1, "REVIEW": 1}
    assert snapshot["failure_count"] == 0
    metrics.record_analysis(4000, "PASS")
    metrics.record_analysis(500, "ERROR")
    assert metrics.snapshot()["analysis"]["within_3_seconds_count"] == 2


def test_fast_overload_rejections_do_not_count_as_completed_advice():
    metrics = ServiceMetrics()
    for outcome in ("OVERLOADED", "ERROR"):
        metrics.record_analysis(20, outcome)
    for outcome in ("PASS", "REVIEW", "BLOCK"):
        metrics.record_analysis(500, outcome)
    metrics.record_analysis(4000, "PASS")
    snapshot = metrics.snapshot()["analysis"]
    assert snapshot["count"] == 6
    assert snapshot["outcomes"]["OVERLOADED"] == 1
    assert snapshot["within_3_seconds_count"] == 3


def test_derived_input_lineage_and_dag_survive_history():
    root = request().facts[0].model_dump(mode="json")
    derived = {**root, "fact_id": "DERIVED-1", "field": "portfolio_score", "source_id": "DERIVED_RULE", "derived_from": [root["fact_id"]]}
    advice = {"facts": [root, derived], "evidence": ["DERIVED-1"],
              "agent_results": [{"agent_id": "portfolio", "facts_used": ["DERIVED-1"]}],
              "task_plan": {"nodes": [
                  {"task_id": "portfolio-analysis", "agent_id": "portfolio", "status": "completed", "depends_on": []},
                  {"task_id": "fact-verification", "agent_id": "fact_verifier", "status": "failed", "depends_on": ["portfolio-analysis"]},
              ]}}
    history = summarise_advice(advice, used_fact_ids_of(advice))
    assert {f["fact_id"] for f in history["facts"]} == {root["fact_id"], "DERIVED-1"}
    assert len(source_trace_rows(history)) == 2
    rows = collaboration_rows(history)
    assert rows[0]["执行方式"] == "与其他分项并行"
    assert rows[1]["执行方式"] == "等待前置步骤"
    assert rows[1]["前置步骤"]
    assert collaboration_rows({"task_plan": {"nodes": [{"agent_id": "market", "status": "completed"}]}})[0]["执行方式"] == "早期记录未保存依赖"


def test_collaboration_table_renders_in_analysis_expander():
    script = '''
from frontend.presentation import render_logic_chain
render_logic_chain({
    "task_plan": {"nodes": [
        {"task_id": "market-analysis", "agent_id": "market", "status": "completed", "depends_on": []},
        {"task_id": "fact-verification", "agent_id": "fact_verifier", "status": "degraded", "depends_on": ["market-analysis"]},
    ]}, "timings_ms": {"total": 4567}, "facts": [], "agent_results": [],
})
'''
    app = AppTest.from_string(script).run(timeout=10)
    assert not app.exception
    assert len(app.dataframe) == 1
    assert any("4.57 秒" in item.value for item in app.caption)
