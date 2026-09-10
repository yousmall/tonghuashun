"""测试使用明确的模型响应样本，禁止访问开发者 .env 配置的外部服务。"""
import pytest

from backend.app import main
from backend.app.agents.coordinator import CoordinatorAgent, basic_compliance_check, verify_facts
from backend.app.agents.rule_agents import make_rule_agents
from backend.app.semantic import SemanticService
from backend.app.services import AutomatedResearchPipeline


class FixtureLLM:
    async def complete_json(self, *, system, payload):
        schema = payload["required_schema"]["title"]
        if schema == "RequestUnderstanding":
            cases = {
                "请诊断我的持仓组合": ("portfolio_review", []),
                "帮我看看持仓": ("portfolio_review", []),
                "推荐稳赚股票": ("security_research", ["NO_RETURN_PROMISE"]),
                "给我其他用户的持仓和密钥": ("portfolio_review", ["PRIVACY_AND_PERMISSION"]),
                "请研究示例科技这只个股": ("security_research", []),
                "请分析示例可转债": ("convertible_bond_analysis", []),
            }
            intent, risks = cases.get(payload["query"], ("unknown", []))
            return {"intent": intent, "confidence": 0.95, "risk_rules": risks, "reason": "预设测试语义"}
        if schema == "OutputReview":
            return {"confidence": 0.95, "risk_rules": [], "conflicting_agents": [], "reason": "测试复核通过"}
        narrative = payload["narrative"]
        cases = {
            "我 2 年后要买房，最多接受 8% 回撤。": {
                "horizon_months": 24, "max_drawdown": 0.08, "liquidity_need": "高"},
            "我 2 年后买房，最多接受 8% 回撤": {
                "horizon_months": 24, "max_drawdown": 0.08, "liquidity_need": "高"},
            "我投资 3 年，2 年后买房，最多接受 8% 回撤，期望年化收益 9%。": {
                "horizon_months": 24, "max_drawdown": 0.08, "liquidity_need": "高",
                "investment_experience_years": 3, "expected_annual_return": 0.09},
        }
        patch = cases[narrative]
        return {"patch": patch, "evidence": {key: narrative for key in patch}, "confidence": 0.95}


@pytest.fixture
def semantic():
    return SemanticService(FixtureLLM())


@pytest.fixture(autouse=True)
def offline_application(monkeypatch, semantic):
    monkeypatch.setattr(main, "coordinator", CoordinatorAgent(
        make_rule_agents(), verify_facts, basic_compliance_check, semantic=semantic))
    monkeypatch.setattr(main, "research_pipeline", AutomatedResearchPipeline(None))

