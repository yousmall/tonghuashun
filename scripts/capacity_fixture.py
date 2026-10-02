"""Explicit offline upstreams for real-HTTP concurrency and history checks."""
import asyncio
import json
import httpx
from backend.app import main
from backend.app.agents.llm_agents import LLMConfig, OpenAICompatibleLLM, make_investment_agents
from backend.app.agents.coordinator import CoordinatorAgent, verify_facts, basic_compliance_check
from backend.app.data_provider.iwencai import IwencaiSkillHubProvider
from backend.app.semantic import SemanticService
from backend.app.services.research import AutomatedResearchPipeline


async def model(request):
    await asyncio.sleep(.01)
    payload = json.loads(json.loads(request.content)["messages"][1]["content"])
    title = payload.get("required_schema", {}).get("title")
    if title == "RequestUnderstanding":
        result = {"intent": "security_research", "confidence": .95, "risk_rules": [],
                  "reason": "合成并发验收", "target": "600519", "data_requirements": []}
    elif title == "OutputReview":
        result = {"confidence": .95, "risk_rules": [], "conflicting_agents": [], "reason": "合成审核"}
    else:
        result = {"agent_id": payload["required_output"]["agent_id"], "status": "completed", "score": 55,
                  "opinion": "合成测试资料用于并发验收。", "confidence": .9,
                  "facts_used": [fact["fact_id"] for fact in payload["authorized_facts"][:2]]}
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)},
        "finish_reason": "stop"}], "usage": {"prompt_tokens": 100, "completion_tokens": 20}})


async def provider(request):
    await asyncio.sleep(.01)
    return httpx.Response(200, json={"data": [{"股票代码": "600519", "名称": "合成测试对象", "日期": "20261002",
        "经济增长评分": 55, "通胀评分": 55, "流动性评分": 55, "政策评分": 55, "风险偏好评分": 55,
        "景气度评分": 55, "估值评分": 55, "资金流向评分": 55, "拥挤度评分": 55,
        "综合评分": 55, "技术面评分": 55, "事件评分": 55, "治理评分": 55, "最新价": 10} ]})


llm = OpenAICompatibleLLM(LLMConfig("https://synthetic.example", "synthetic", "synthetic-capacity",
    input_price_per_million=1, output_price_per_million=1), transport=httpx.MockTransport(model))
agents, _ = make_investment_agents(llm)
main.coordinator = CoordinatorAgent(agents, verify_facts, basic_compliance_check, semantic=SemanticService(llm))
main.llm_enabled = True
main.data_provider = IwencaiSkillHubProvider("synthetic-capacity", base_url="https://synthetic.example", transport=httpx.MockTransport(provider))
main.data_provider.source_id = "SYNTHETIC_CAPACITY_ONLY"
main.research_pipeline = AutomatedResearchPipeline(main.data_provider, cache=main.research_pipeline.cache)
app = main.app
