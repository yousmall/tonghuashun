"""LLM 语义边界和调用预算；只使用受控 HTTP 响应，不访问外部模型。"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backend.app import main
from backend.app.agents.coordinator import CoordinatorAgent, basic_compliance_check, verify_facts
from backend.app.agents.llm_agents import LLMConfig, OpenAICompatibleLLM
from backend.app.agents.rule_agents import make_rule_agents
from backend.app.models import (
    AgentResult,
    FactRecord,
    Intent,
    OrchestrationRequest,
    ProfileAssessmentRequest,
)
from backend.app.semantic import SemanticService
from backend.app.services.profile import assess_profile


def request(query="不用保证收益，解释一下这种说法为什么不可信", **kwargs):
    return OrchestrationRequest(query=query, profile={"user_id": "test", "confirmed": True}, **kwargs)


def fact(field, value, *, entity):
    """构造一条带来源与时点的已授权事实。"""
    return FactRecord(
        fact_id=f"F-{field}-{entity}", entity=entity, field=field, value=value,
        snapshot_time=datetime.now(timezone.utc) - timedelta(minutes=5),
        source_id="IWENCAI_SKILLHUB", quality=0.9,
    )


# 覆盖宏观与行业两个节点的完整评分维度，使它们真正形成观点。
MARKET_AND_INDUSTRY_FACTS = [
    *(fact(field, 55, entity="A股市场") for field in
      ("growth_score", "inflation_score", "liquidity_score", "policy_score", "risk_appetite_score")),
    *(fact(field, 58, entity="白酒行业") for field in
      ("prosperity_score", "valuation_score", "capital_flow_score", "crowding_score", "policy_score")),
]


def response(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}}]})


def service(content, **config):
    calls = []

    def handler(req):
        calls.append(json.loads(req.content))
        return response(content)

    client = OpenAICompatibleLLM(
        LLMConfig("https://model.test/v1", "fake-key", "test-model", max_retries=0, **config),
        transport=httpx.MockTransport(handler),
    )
    return SemanticService(client), calls


def understanding(intent="education", rules=None, confidence=0.95):
    return {"intent": intent, "confidence": confidence, "risk_rules": rules or [], "reason": "理解否定、引用及上下文"}


@pytest.mark.asyncio
async def test_intent_is_model_result_with_context_and_no_keyword_override():
    semantic, calls = service(understanding())
    result = await semantic.understand(request(
        "基金是什么？不要向我推荐稳赚产品",
        context_messages=[{"role": "user", "content": "我只需要概念讲解"}],
    ))
    assert result.intent is Intent.EDUCATION
    assert result.risk_rules == []
    payload = json.loads(calls[0]["messages"][1]["content"])
    assert payload["conversation"][0]["content"] == "我只需要概念讲解"
    assert "user_id" not in payload["profile"]
    assert calls[0]["max_tokens"] == 2000
    assert "否定" in calls[0]["messages"][0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    understanding("buy_now"), understanding(confidence=0.1),
    {**understanding(), "confidence": float("nan")},
    {**understanding(), "risk_rules": ["RUN_SHELL"]},
    {**understanding(), "confirmed": True}, [], None, {},
])
async def test_invalid_or_uncertain_intent_returns_unknown_without_guessing(content):
    semantic, calls = service(content)
    result = await semantic.understand(request("请诊断我的持仓组合"))
    assert result.intent is Intent.UNKNOWN
    assert result.confidence == 0
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_profile_chinese_numbers_explicit_overrides_and_confirmation_gate():
    narrative = "两年以后要用钱，我不再频繁交易，能承受百分之八的损失，目标年收益百分之九"
    semantic, calls = service({
        "patch": {"horizon_months": 24, "max_drawdown": 0.08, "expected_annual_return": 0.09,
                  "behavioral_notes": ["自述不再频繁交易"]},
        "evidence": {"horizon_months": "两年以后要用钱", "max_drawdown": "百分之八的损失",
                     "expected_annual_return": narrative, "behavioral_notes": "不再频繁交易"},
        "confidence": 0.95,
    })
    draft = await assess_profile(ProfileAssessmentRequest(
        user_id="u", narrative=narrative, expected_annual_return=0.05,
        questionnaire={key: 40 for key in (
            "financial_capacity", "loss_tolerance", "investment_horizon",
            "knowledge_experience", "behavior_stability")},
    ), semantic)
    assert draft.profile.horizon_months == 24
    assert draft.profile.max_drawdown == 0.08
    assert draft.profile.expected_annual_return == 0.05
    assert draft.profile.behavioral_notes == ["自述不再频繁交易"]
    assert draft.profile.risk_score == 40
    assert draft.profile.risk_level == "R3"
    assert draft.profile.confirmed is False
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("patch,evidence", [
    ({"horizon_months": -2}, {"horizon_months": "两年"}),
    ({"horizon_months": 24}, {"horizon_months": "伪造的原文"}),
    ({"horizon_months": 24}, {}),
    ({"confirmed": True, "risk_level": "R5"}, {}),
    ({"single_security_limit": 1}, {}),
])
async def test_profile_rejects_invalid_values_fabricated_evidence_and_forbidden_fields(patch, evidence):
    semantic, _ = service({"patch": patch, "evidence": evidence, "confidence": 0.95})
    result = await assess_profile(ProfileAssessmentRequest(user_id="u", narrative="两年后需要用钱"), semantic)
    assert result.profile.horizon_months is None
    assert result.profile.risk_level is None
    assert result.profile.single_security_limit == 0.2
    assert result.profile.confirmed is False
    assert "不可用" in result.evidence[0]


@pytest.mark.asyncio
async def test_empty_profile_does_not_call_model_and_missing_model_never_guesses():
    semantic, calls = service({})
    await assess_profile(ProfileAssessmentRequest(user_id="u", narrative="  "), semantic)
    assert calls == []
    unavailable = SemanticService()
    assert (await unavailable.understand(request("持仓组合"))).intent is Intent.UNKNOWN
    draft = await assess_profile(ProfileAssessmentRequest(user_id="u", narrative="两年后买房"), unavailable)
    assert draft.profile.horizon_months is None


@pytest.mark.asyncio
async def test_analysis_reuses_understanding_and_batches_final_review(monkeypatch):
    calls = []

    def handler(req):
        payload = json.loads(json.loads(req.content)["messages"][1]["content"])
        calls.append(payload)
        if payload["required_schema"]["title"] == "RequestUnderstanding":
            return response(understanding("portfolio_review"))
        assert len(payload["results"]) == 5
        return response({"confidence": 0.95, "risk_rules": [], "conflicting_agents": [], "reason": "无冲突"})

    llm = OpenAICompatibleLLM(LLMConfig("https://model.test/v1", "fake", "test", max_retries=0),
                             transport=httpx.MockTransport(handler))
    monkeypatch.setattr(main, "coordinator", CoordinatorAgent(
        make_rule_agents(), verify_facts, basic_compliance_check, semantic=SemanticService(llm)))
    result = await main.analyze_portfolio(request("这几项放在一起是否合适"), user=None)
    assert result.intent is Intent.PORTFOLIO_REVIEW
    assert len(calls) == 2  # 一次理解 + 五个专业结果合并审核；run 不重复理解。
    assert result.compliance.status == "REVIEW"  # LLM 通过不能覆盖缺少事实的硬检查。


@pytest.mark.asyncio
async def test_risk_preflight_blocks_before_fetch_or_specialists(monkeypatch):
    semantic, calls = service(understanding("security_research", ["NO_RETURN_PROMISE", "UNVERIFIED_RUMOR"]))
    coordinator = CoordinatorAgent(make_rule_agents(), verify_facts, basic_compliance_check, semantic=semantic)
    monkeypatch.setattr(main, "coordinator", coordinator)
    result = await main.analyze_portfolio(request("把资金全部押上并承诺翻倍"), user=None)
    assert result.compliance.status == "BLOCK"
    assert result.agent_results == []
    assert result.task_plan.nodes == []
    assert result.data_acquisition.requested_capabilities == []
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_output_review_adds_semantic_conflict_without_overriding_hard_checks():
    semantic, _ = service({"confidence": 0.9, "risk_rules": [], "conflicting_agents": ["market", "industry"],
                           "reason": "同一时间范围的结论相互矛盾"})
    coordinator = CoordinatorAgent(make_rule_agents(), verify_facts, basic_compliance_check, semantic=semantic)
    from backend.app.semantic import RequestUnderstanding
    # 必须提供事实，否则两个节点都处于 DEGRADED（未形成观点），不构成矛盾方。
    result = await coordinator.run(
        request(facts=MARKET_AND_INDUSTRY_FACTS),
        understanding=RequestUnderstanding(**understanding("portfolio_review")),
    )
    voiced = {item.agent_id for item in result.agent_results if item.status == "completed"}
    assert {"market", "industry"} <= voiced
    assert result.cross_validation.status == "REVIEW"
    assert result.cross_validation.issues[-1].code == "SEMANTIC_AGENT_CONFLICT"
    # 语义矛盾必须体现在最终合规结果上，不能被数值硬检查覆盖回 PASS。
    assert result.compliance.status == "REVIEW"
    assert "EVIDENCE_INSUFFICIENT" in result.compliance.matched_rules or (
        "CROSS_AGENT_INCONSISTENCY" in result.compliance.matched_rules)


@pytest.mark.asyncio
async def test_degraded_node_is_not_treated_as_dissenting_opinion():
    """缺数据的节点没有说话，不能被当成"持相反意见"而把整份分析降级。"""
    semantic, _ = service({"confidence": 0.9, "risk_rules": [], "conflicting_agents": ["market", "industry"],
                           "reason": "模型误把缺数据节点当成矛盾方"})
    coordinator = CoordinatorAgent(make_rule_agents(), verify_facts, basic_compliance_check, semantic=semantic)
    from backend.app.semantic import RequestUnderstanding
    result = await coordinator.run(
        request(facts=MARKET_AND_INDUSTRY_FACTS),
        understanding=RequestUnderstanding(**understanding("portfolio_review")),
    )
    # market/industry 在本次请求中已形成观点，因此仍应被认定为矛盾方；对照组见下。
    assert "SEMANTIC_AGENT_CONFLICT" in {issue.code for issue in result.cross_validation.issues}

    # 同一判定下，只带个股事实（宏观/行业缺数据）不得再触发跨智能体分歧。
    security_only = [
        fact("fundamental_score", 40, entity="贵州茅台"),
        fact("technical_score", 80, entity="贵州茅台"),
    ]
    result2 = await coordinator.run(
        request(facts=security_only),
        understanding=RequestUnderstanding(**understanding("portfolio_review")),
    )
    assert result2.cross_validation.status != "REVIEW"
    assert "SEMANTIC_AGENT_CONFLICT" not in {issue.code for issue in result2.cross_validation.issues}
    assert "CROSS_AGENT_INCONSISTENCY" not in result2.compliance.matched_rules


@pytest.mark.asyncio
async def test_review_failure_requires_review():
    coordinator = CoordinatorAgent(make_rule_agents(), verify_facts, basic_compliance_check)
    from backend.app.semantic import RequestUnderstanding
    result = await coordinator.run(request(), understanding=RequestUnderstanding(**understanding("portfolio_review")))
    assert result.compliance.status == "REVIEW"
    assert "SEMANTIC_REVIEW_UNAVAILABLE" in result.compliance.matched_rules


@pytest.mark.asyncio
async def test_output_review_rejects_unknown_agent_reference():
    semantic, _ = service({"confidence": 0.9, "risk_rules": [], "conflicting_agents": ["invented"],
                           "reason": "假智能体"})
    assert await semantic.review(request(), []) is None


@pytest.mark.asyncio
async def test_concurrency_is_bounded_and_timeout_includes_queue():
    active = peak = 0

    async def handler(req):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.015)
            return response({"ok": True})
        finally:
            active -= 1

    llm = OpenAICompatibleLLM(
        LLMConfig("https://model.test", "fake", "test", max_concurrency=2, max_retries=0),
        transport=httpx.MockTransport(handler))
    results = await asyncio.gather(*(llm.complete_json(system="test", payload={}) for _ in range(6)))
    assert peak == 2
    assert all(item == {"ok": True} for item in results)

    slow = OpenAICompatibleLLM(
        LLMConfig("https://model.test", "fake", "test", timeout_seconds=0.001, max_retries=0),
        transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="总时间预算"):
        await slow.complete_json(system="test", payload={})
    assert active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected_calls", [(401, 1), (400, 1), (429, 2), (503, 2)])
async def test_retry_limit_and_non_retryable_errors(status, expected_calls):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(status)

    llm = OpenAICompatibleLLM(
        LLMConfig("https://model.test", "fake", "test", max_retries=1),
        transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError):
        await llm.complete_json(system="test", payload={})
    assert len(calls) == expected_calls


@pytest.mark.asyncio
async def test_input_budget_rejects_before_network():
    semantic, calls = service(understanding(), max_input_chars=1000)
    result = await semantic.understand(request("测试" * 2000))
    assert result.intent is Intent.UNKNOWN
    assert calls == []



@pytest.mark.asyncio
async def test_full_hybrid_analysis_has_at_most_seven_logical_calls(monkeypatch):
    from datetime import datetime, timezone
    from backend.app.agents.llm_agents import make_investment_agents
    from backend.app.models import FactRecord

    calls = []

    def handler(req):
        payload = json.loads(json.loads(req.content)["messages"][1]["content"])
        calls.append(payload)
        schema = payload.get("required_schema", {}).get("title")
        if schema == "RequestUnderstanding":
            return response(understanding("portfolio_review"))
        if schema == "OutputReview":
            return response({"confidence": 0.95, "risk_rules": [], "conflicting_agents": [],
                             "reason": "五个专业结果已合并审核"})
        return response({
            "agent_id": payload["required_output"]["agent_id"], "status": "completed",
            "opinion": "仅解释所提供的持仓快照", "confidence": 0.8, "facts_used": ["F-weight"],
            "details": {"single_security_limit": 1}, "risk_flags": [],
        })

    llm = OpenAICompatibleLLM(LLMConfig("https://model.test", "fake", "test", max_retries=0),
                             transport=httpx.MockTransport(handler))
    agents, _ = make_investment_agents(llm)
    coordinator = CoordinatorAgent(agents, verify_facts, basic_compliance_check, semantic=SemanticService(llm))
    monkeypatch.setattr(main, "coordinator", coordinator)
    result = await main.analyze_portfolio(request(
        "请审视这些资产放在一起的风险",
        facts=[FactRecord(fact_id="F-weight", entity="示例", field="weight", value=0.4,
                          source_id="TEST", snapshot_time=datetime.now(timezone.utc), quality=0.9)]),
        user=None,
    )
    assert len(calls) == 7
    assert len(result.agent_results) == 5
    portfolio = next(item for item in result.agent_results if item.agent_id == "portfolio")
    assert portfolio.details["single_security_limit"] == 0.2
    assert portfolio.details["llm_assessment"]["single_security_limit"] == 1
    assert "单标的集中度超限" in portfolio.risk_flags
    market = next(item for item in result.agent_results if item.agent_id == "market")
    assert market.status == "degraded"  # 模型不能抹去缺少宏观维度的降级状态。


@pytest.mark.asyncio
async def test_queue_wait_cannot_exceed_total_timeout():
    calls = []

    def handler(req):
        calls.append(req)
        return response({})

    llm = OpenAICompatibleLLM(
        LLMConfig("https://model.test", "fake", "test", timeout_seconds=0.002, max_concurrency=1),
        transport=httpx.MockTransport(handler))
    await llm._semaphore.acquire()
    try:
        with pytest.raises(RuntimeError, match="总时间预算"):
            await llm.complete_json(system="test", payload={})
        assert calls == []
    finally:
        llm._semaphore.release()


@pytest.mark.asyncio
async def test_output_return_promise_is_blocked_after_specialist_review():
    from backend.app.semantic import RequestUnderstanding
    semantic, _ = service({"confidence": 0.95, "risk_rules": ["NO_RETURN_PROMISE"],
                           "conflicting_agents": [], "reason": "输出给出收益保证"})
    coordinator = CoordinatorAgent(make_rule_agents(), verify_facts, basic_compliance_check, semantic=semantic)
    result = await coordinator.run(request(), understanding=RequestUnderstanding(**understanding("portfolio_review")))
    assert result.compliance.status == "BLOCK"
    assert result.confidence == 0
    assert result.allocation == []


def test_application_shares_client_between_semantics_and_all_specialists(monkeypatch):
    monkeypatch.setattr(LLMConfig, "from_env", classmethod(
        lambda cls: LLMConfig("https://model.test", "fake", "test")))
    coordinator, enabled = main.build_coordinator()
    assert enabled
    assert all(handler.__self__.llm is coordinator.semantic.llm for handler in coordinator.agents.values())



@pytest.mark.asyncio
@pytest.mark.parametrize("model,mode,expected", [
    ("deepseek-v4-flash", "auto", {"type": "disabled"}),
    ("another-compatible-model", "auto", None),
    ("deepseek-v4-flash", "omit", None),
    ("another-compatible-model", "disabled", {"type": "disabled"}),
    ("deepseek-v4-pro", "enabled", {"type": "enabled"}),
])
async def test_thinking_parameter_is_provider_compatible(model, mode, expected):
    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        return response({"ok": True})

    llm = OpenAICompatibleLLM(
        LLMConfig("https://model.test", "fake", model, thinking_mode=mode),
        transport=httpx.MockTransport(handler))
    await llm.complete_json(system="test", payload={})
    assert sent[0].get("thinking") == expected
    assert sent[0]["max_tokens"] == 2000


@pytest.mark.asyncio
async def test_token_truncation_is_rejected_even_when_partial_json_is_valid():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "length", "message": {"content": '{"intent":"education"}'},
        }]})

    llm = OpenAICompatibleLLM(
        LLMConfig("https://model.test", "fake", "test", max_retries=1),
        transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="token 上限"):
        await llm.complete_json(system="test", payload={})
    assert len(calls) == 1


@pytest.mark.parametrize("timeout", [1, 30, 60])
def test_orchestration_budget_covers_model_and_stays_within_node_limit(timeout):
    llm = OpenAICompatibleLLM(LLMConfig("https://model.test", "fake", "test", timeout_seconds=timeout))
    coordinator = CoordinatorAgent(make_rule_agents(), verify_facts, basic_compliance_check,
                                   semantic=SemanticService(llm))
    plan = coordinator.plan(request(), "T-BUDGET", Intent.MARKET_ANALYSIS)
    for node in plan.nodes:
        if node.agent_id != "fact_verifier":
            assert timeout <= node.timeout_seconds <= 60


def test_unknown_thinking_mode_is_rejected():
    with pytest.raises(ValueError, match="thinking_mode"):
        LLMConfig("https://model.test", "fake", "test", thinking_mode="invalid")

