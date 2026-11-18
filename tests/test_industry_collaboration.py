"""跨方向资料汇总、真实协同调度与行业页提交的离线回归。"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from streamlit.testing.v1 import AppTest

from backend.app import main
from backend.app.agents.coordinator import CoordinatorAgent, basic_compliance_check, verify_facts
from backend.app.agents.llm_agents import HybridInvestmentAgent
from backend.app.agents.rule_agents import make_rule_agents
from backend.app.models import AgentResult, FactRecord, Intent, OrchestrationRequest, UserProfile
from backend.app.semantic import RequestUnderstanding
from backend.app.services.model_input import slice_facts_for_agent
from frontend.industry_collaboration import collect_research_materials


def fact(fid, entity="测试公司", field="close_price", value=10):
    return FactRecord(fact_id=fid, entity=entity, field=field, value=value,
                      source_id="SYNTHETIC_TEST_ONLY", quality=.95,
                      period=datetime.now(timezone.utc).date().isoformat(),
                      snapshot_time=datetime.now(timezone.utc)).model_dump(mode="json")


def request(**updates):
    return OrchestrationRequest(**{
        "query": "请围绕半导体行业综合市场、个股、基金和可转债信息，开展行业多智能体协同研究",
        "research_direction": "行业分析", "research_target": "半导体", "industry_collaboration": True,
        "research_mode": "deep", "auto_fetch": False,
        "profile": UserProfile(user_id="fixture", confirmed=True, risk_level="R3"), **updates,
    })


def test_collects_all_four_directions_without_using_old_sections_or_foreign_backend():
    base = "http://fixture/api/v1"
    shared = fact("shared")
    state = {"active_research_direction": "行业分析", "board_target_industry": "半导体",
             "facts": [shared], "research_board_cache": {}, "research_sessions": {}}
    for label, code in [("市场解读", "market"), ("个股研究", "stock"), ("基金筛选", "fund"), ("可转债分析", "convertible")]:
        state["research_board_cache"][(base, code, "示例")] = {"loaded_at": 1, "result": {"sections": [
            {"status": "ok", "facts": [fact(code)]},
            {"status": "loading", "facts": [fact("loading")], "previous_facts": [fact("old")]},
        ]}}
        state["research_sessions"][label] = {"facts": [shared], "advice": {"conclusion": f"{label}的旧观点"}}
    state["research_board_cache"][("http://other", "stock", None)] = {
        "result": {"sections": [{"status": "ok", "facts": [fact("foreign")]}]}}
    bundle = collect_research_materials(state, base)
    assert {f["fact_id"] for f in bundle["facts"]} == {"shared", "market", "stock", "fund", "convertible"}
    assert {m["source"] for m in bundle["research_materials"]} == {"市场解读", "个股研究", "基金筛选", "可转债分析"}
    assert all("旧观点" not in str(f) for f in bundle["facts"])
    assert state["facts"] == [shared]


def test_conflicting_ids_are_excluded_and_trend_only_enters_reference_materials():
    first = fact("same")
    state = {"active_research_direction": "行业分析", "board_target_industry": "半导体", "facts": [first],
             "research_sessions": {"个股研究": {"facts": [{**first, "value": 99}]}},
             "industry_trend_cache": {"base|半导体": {"result": {
                 "status": "ok", "analysis": {"verdict": "上行", "latest_close": 123}}}}}
    result = collect_research_materials(state, "base")
    assert result["facts"] == [] and result["conflicting_ids"] == 1
    assert result["research_materials"][0]["source"] == "行业走势"
    state["board_target_industry"] = "银行"
    assert collect_research_materials(state, "base")["research_materials"] == []


@pytest.mark.parametrize("updates", [{"research_direction": "个股研究"}, {"research_mode": "instant"},
                                     {"industry_collaboration": False, "research_materials": [{"source": "市场解读", "summary": "旧观点"}]}])
def test_collaboration_cannot_silently_enter_a_different_flow(updates):
    with pytest.raises(ValidationError):
        request(**updates)


@pytest.mark.parametrize("agent_id,field", [("security", "conversion_premium_rate"), ("fund", "fund_nav"),
                                          ("industry", "capital_flow"), ("market", "close_price")])
def test_collaborating_agents_receive_cross_direction_metrics_and_lineage(agent_id, field):
    parent = FactRecord.model_validate(fact("parent", field="change"))
    extra = FactRecord.model_validate({**fact("cross-direction", field=field), "derived_from": ["parent"]})
    crowded = [FactRecord.model_validate(fact(f"price-{i}", entity=f"公司{i}",
                                             field="cpi" if agent_id == "market" else "close_price")) for i in range(100)]
    rows = slice_facts_for_agent([parent, extra, *crowded], agent_id, [], industry_collaboration=True)
    assert {"parent", "cross-direction"} <= {row.fact_id for row in rows}


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["半导体", None])
async def test_api_runs_four_specialists_in_parallel_then_verifies(semantic, monkeypatch, target):
    started = set()
    event = asyncio.Event()

    def handler(agent_id):
        async def run(req):
            assert req.industry_collaboration and req.research_target == target
            assert {f.fact_id for f in req.facts} >= {"market", "stock", "fund", "convertible"}
            started.add(agent_id)
            if len(started) == 4:
                event.set()
            await asyncio.wait_for(event.wait(), timeout=1)
            return AgentResult(agent_id=agent_id, status="completed", confidence=.8,
                               opinion="仅根据授权测试资料作有限分析。", facts_used=[agent_id if agent_id in {"market", "fund"} else "stock"])
        return run

    coordinator = CoordinatorAgent({name: handler(name) for name in ("market", "industry", "security", "fund")},
                                   verify_facts, basic_compliance_check, semantic=semantic)

    async def understand(req):
        pytest.fail("显式行业协同不应调用语义意图识别")
    monkeypatch.setattr(semantic, "understand", understand)
    monkeypatch.setattr(main, "coordinator", coordinator)
    result = await main.analyze_portfolio(request(
        research_target=target, facts=[fact(fid) for fid in ["market", "stock", "fund", "convertible"]],
    ), None)
    assert started == {"market", "industry", "security", "fund"}
    assert {r.agent_id for r in result.agent_results} == started
    assert {f.fact_id for f in result.facts} >= {"market", "stock", "fund", "convertible"}
    nodes = {n.agent_id: n for n in result.task_plan.nodes}
    assert len(nodes["fact_verifier"].depends_on) == 4
    assert nodes["compliance"].depends_on == ["fact-verification"]
    assert all(n.status == "completed" for n in nodes.values())
    normal = coordinator.plan(request(industry_collaboration=False), "normal", Intent.INDUSTRY_ANALYSIS)
    assert [n.agent_id for n in normal.nodes][:2] == ["market", "industry"]
    assert len(normal.nodes) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("has_facts", [True, False])
async def test_reference_opinions_are_separate_from_model_evidence(has_facts):
    sent = []
    class Client:
        config = SimpleNamespace(model="offline-fixture")
        async def complete_json(self, *, system, payload):
            sent.append((system, payload))
            return {"agent_id": "security", "status": "degraded", "confidence": .2,
                    "opinion": "缺少充分研究依据。", "facts_used": ["stock"] if has_facts else []}
    result = await HybridInvestmentAgent("security", make_rule_agents()["security"], Client()).run(request(
        facts=[fact("stock")] if has_facts else [],
        research_materials=[{"source": "可转债分析", "summary": "过往转债观点待复核"}]))
    assert sent[0][1]["reference_materials"][0]["source"] == "可转债分析"
    assert "不能作为事实" in sent[0][0]
    expected = ["stock"] if has_facts else []
    assert [f["fact_id"] for f in sent[0][1]["authorized_facts"]] == expected
    assert result.facts_used == expected


@pytest.mark.asyncio
async def test_missing_materials_leave_all_specialists_in_review(semantic):
    coordinator = CoordinatorAgent(make_rule_agents(), verify_facts, basic_compliance_check, semantic=semantic)
    understanding = RequestUnderstanding(intent=Intent.INDUSTRY_ANALYSIS, confidence=1, risk_rules=[], reason="测试固定任务")
    result = await coordinator.run(request(), understanding=understanding)
    assert {r.agent_id for r in result.agent_results} == {"market", "industry", "security", "fund"}
    assert not result.evidence and result.compliance.status == "REVIEW"
    assert "证据不足" in result.conclusion and "行业景气指标" in result.conclusion


def test_industry_button_submits_deep_research_without_changing_chat_mode():
    app = AppTest.from_string('''
import streamlit as st
from concurrent.futures import Future
from unittest.mock import patch
from frontend import streamlit_app as ui
from frontend.industry_collaboration import render_industry_collaboration
ui.init_session()
st.session_state.auth_token = "offline-token"
st.session_state.active_research_direction = "行业分析"
st.session_state.board_target_industry = "半导体"
st.session_state.research_mode = "instant"
class Executor:
    def submit(self, function, *args):
        st.session_state.sent = args[4]
        return Future()
with patch.object(ui, "profile_ready", return_value=True), patch.object(ui, "analysis_executor", return_value=Executor()):
    render_industry_collaboration("base", ready=True, submit=ui.submit_chat_analysis)
''').run()
    assert not app.exception
    next(b for b in app.button if b.label == "开始多智能体协同分析").click().run()
    assert not app.exception
    payload = app.session_state["sent"]
    assert payload["industry_collaboration"] and payload["research_mode"] == "deep"
    assert not payload["auto_fetch"]
    assert payload["research_target"] == "半导体" and payload["research_direction"] == "行业分析"
    assert payload["context_messages"] == []
    assert app.session_state["research_mode"] == "instant"
    assert len(app.session_state["conversation"]) == 1
    assert next(b for b in app.button if b.label == "开始多智能体协同分析").disabled
