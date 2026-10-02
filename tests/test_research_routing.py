"""五个研究入口的模型分类、检索上下文和执行前路由闸门。"""
import pytest
from streamlit.testing.v1 import AppTest

from backend.app import main
from backend.app.agents.coordinator import CoordinatorAgent, basic_compliance_check, verify_facts
from backend.app.agents.rule_agents import make_rule_agents
from backend.app.models import Intent, OrchestrationRequest
from backend.app.research_routing import route_guidance
from backend.app.semantic import SemanticService


class RouteLLM:
    def __init__(self, intent: str):
        self.intent = intent
        self.calls = []

    async def complete_json(self, *, system, payload):
        self.calls.append((system, payload))
        if payload["required_schema"]["title"] == "RequestUnderstanding":
            return {"intent": self.intent, "confidence": 0.96, "risk_rules": [],
                    "reason": "已识别研究对象和请求类型"}
        return {"confidence": 0.96, "risk_rules": [], "conflicting_agents": [], "reason": "已复核"}


def research_request(direction: str, question: str) -> OrchestrationRequest:
    return OrchestrationRequest(
        query=f"{direction}：{question}", research_direction=direction,
        profile={"user_id": "test", "confirmed": True},
    )


def test_stock_screen_route_card_is_retrieved_despite_fund_page_prefix():
    cards = route_guidance("基金筛选：筛选沪深A股中非ST公司，ROE大于8%，市盈率低于40")
    assert cards[0]["direction"] == "个股研究"
    assert len(route_guidance("完全无关的内容")) == 5


def test_streamlit_sends_selected_page_separately_from_question():
    app = AppTest.from_string("""
from concurrent.futures import Future
from unittest.mock import patch
import streamlit as st
from frontend import streamlit_app as ui
ui.init_session()
st.session_state.profile['confirmed'] = True
class CaptureExecutor:
    def submit(self, *args):
        st.session_state.sent_payload = args[5]
        return Future()
with patch.object(ui, 'analysis_executor', return_value=CaptureExecutor()), \\
     patch.object(ui, 'backend_http_client', return_value=object()):
    ui.submit_chat_analysis('http://localhost', '基金筛选', '基金筛选：筛选沪深A股')
""").run(timeout=15)
    assert not app.exception
    assert app.session_state["sent_payload"]["research_direction"] == "基金筛选"
    assert app.session_state["sent_payload"]["query"] == "基金筛选：筛选沪深A股"


@pytest.mark.asyncio
async def test_five_pages_use_one_model_call_with_retrieved_route_knowledge():
    llm = RouteLLM("security_research")
    semantic = SemanticService(llm)
    request = research_request("基金筛选", "筛选沪深A股中非ST公司，ROE大于8%")

    result = await semantic.understand(request)

    assert result.intent is Intent.SECURITY_RESEARCH
    assert len(llm.calls) == 1
    _, payload = llm.calls[0]
    assert payload["query"] == "筛选沪深A股中非ST公司，ROE大于8%"
    assert len(payload["route_catalog"]) == 5
    assert payload["retrieved_route_guidance"][0]["direction"] == "个股研究"
    assert "research_direction" not in payload


@pytest.mark.asyncio
async def test_wrong_page_returns_correct_page_without_provider_or_specialists(monkeypatch):
    llm = RouteLLM("security_research")
    monkeypatch.setattr(main, "coordinator", CoordinatorAgent(
        make_rule_agents(), verify_facts, basic_compliance_check, semantic=SemanticService(llm)))
    request = research_request("基金筛选", "筛选沪深A股中非ST公司，ROE大于8%")

    advice = await main.analyze_portfolio(request, user=None)

    assert "个股研究" in advice.conclusion
    assert advice.compliance.status == "REVIEW"
    assert advice.agent_results == []
    assert advice.task_plan.nodes == []
    assert advice.data_acquisition.requested_capabilities == []
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_matching_page_continues_and_unrelated_input_is_rejected(monkeypatch):
    llm = RouteLLM("security_research")
    monkeypatch.setattr(main, "coordinator", CoordinatorAgent(
        make_rule_agents(), verify_facts, basic_compliance_check, semantic=SemanticService(llm)))
    matching = await main.analyze_portfolio(research_request("个股研究", "分析贵州茅台"), user=None)
    assert matching.task_plan.nodes

    unrelated_llm = RouteLLM("unknown")
    monkeypatch.setattr(main, "coordinator", CoordinatorAgent(
        make_rule_agents(), verify_facts, basic_compliance_check,
        semantic=SemanticService(unrelated_llm)))
    unrelated = await main.analyze_portfolio(research_request("基金筛选", "帮我写旅游计划"), user=None)
    assert "无法输出相关信息" in unrelated.conclusion
    assert unrelated.task_plan.nodes == []
    assert unrelated.data_acquisition.requested_capabilities == []


@pytest.mark.asyncio
async def test_model_outage_is_not_mislabeled_as_unrelated_input():
    coordinator = CoordinatorAgent(
        make_rule_agents(), verify_facts, basic_compliance_check, semantic=SemanticService())
    advice = await coordinator.run(research_request("基金筛选", "比较两只 ETF"))
    assert "语义判断服务暂时不可用" in advice.conclusion
    assert "无法输出相关信息" not in advice.conclusion
    assert advice.agent_results == []
