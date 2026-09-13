"""第三方模型、问财数据、多轮上下文、一致性和容量冒烟回归。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backend.app.agents.coordinator import cross_validate_results, verify_facts
from backend.app.agents.coordinator import CoordinatorAgent, basic_compliance_check
from backend.app.agents.demo_agents import make_fact_based_agent
from backend.app.agents.llm_agents import HybridInvestmentAgent, LLMConfig, OpenAICompatibleLLM
from backend.app.agents.rule_agents import make_rule_agents
from backend.app import main as main_module
from backend.app.data_provider import IwencaiSkillHubProvider
from backend.app.main import app, coordinator
from backend.app.models import AgentResult, FactRecord, Intent, OrchestrationRequest, ProfileAssessmentRequest, TaskStatus, UserProfile
from backend.app.services import AutomatedResearchPipeline
from backend.app.services.profile import assess_profile


def make_fact(field: str, value: object, *, age_seconds: int = 0, source: str = "TEST") -> FactRecord:
    return FactRecord(
        fact_id=f"F-{field}-{source}",
        entity="示例",
        field=field,
        value=value,
        snapshot_time=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        source_id=source,
        quality=0.9,
    )


@pytest.mark.asyncio
async def test_extended_profile_records_history_experience_and_expected_return(semantic) -> None:
    assessment = await assess_profile(
        ProfileAssessmentRequest(
            user_id="u-extended",
            narrative="我投资 3 年，2 年后买房，最多接受 8% 回撤，期望年化收益 9%。",
            investment_history=["长期定投宽基 ETF"],
        ), semantic
    )
    assert assessment.profile.investment_experience_years == 3
    assert assessment.profile.expected_annual_return == 0.09
    assert assessment.profile.investment_history == ["长期定投宽基 ETF"]
    assert assessment.profile.confirmed is False


@pytest.mark.asyncio
async def test_field_specific_freshness_rejects_old_quote_but_accepts_financial_metric() -> None:
    quote = make_fact("close_price", 10, age_seconds=120)
    financial = make_fact("pe_ttm", 12, age_seconds=8 * 86_400)
    result = AgentResult(
        agent_id="security",
        status=TaskStatus.COMPLETED,
        opinion="测试",
        confidence=0.9,
        facts_used=[quote.fact_id, financial.fact_id],
    )
    verified = await verify_facts([result], [quote, financial])
    assert verified[0].facts_used == [financial.fact_id]
    assert verified[0].status is TaskStatus.DEGRADED


def test_cross_validation_detects_source_conflict_and_agent_dispersion() -> None:
    first = make_fact("valuation_score", 30, source="A")
    second = make_fact("valuation_score", 80, source="B")
    results = [
        AgentResult(agent_id="market", status=TaskStatus.COMPLETED, opinion="a", score=20, confidence=0.8, facts_used=[first.fact_id]),
        AgentResult(agent_id="industry", status=TaskStatus.COMPLETED, opinion="b", score=55, confidence=0.8, facts_used=[first.fact_id]),
        AgentResult(agent_id="security", status=TaskStatus.COMPLETED, opinion="c", score=90, confidence=0.8, facts_used=[second.fact_id]),
    ]
    validation = cross_validate_results(results, [first, second])
    assert validation.status.value == "REVIEW"
    assert {issue.code for issue in validation.issues} == {"SOURCE_VALUE_CONFLICT", "AGENT_SCORE_DISPERSION"}
    assert validation.consensus_score == 55


@pytest.mark.asyncio
async def test_hybrid_agent_accepts_only_structured_authorized_llm_output() -> None:
    fact = make_fact("growth_score", 60)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret"
        content = {
            "agent_id": "ignored",
            "status": "completed",
            "opinion": "授权事实显示市场环境中性。",
            "score": 60,
            "confidence": 0.8,
            "facts_used": [fact.fact_id],
            "risk_flags": [],
            "invalidation_conditions": ["事实更新"],
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]})

    llm = OpenAICompatibleLLM(
        LLMConfig(base_url="https://llm.example/v1", api_key="secret", model="third-party", max_retries=0),
        transport=httpx.MockTransport(handler),
    )
    agent = HybridInvestmentAgent("market", make_fact_based_agent("market"), llm)
    request = OrchestrationRequest(
        query="分析市场",
        profile=UserProfile(user_id="u", confirmed=True),
        facts=[fact],
        context_messages=[{"role": "user", "content": "先看风险"}],
    )
    result = await agent.run(request)
    assert result.agent_id == "market"
    assert result.details["engine"] == "third_party_llm"
    assert result.citations == ["TEST"]


@pytest.mark.asyncio
async def test_iwencai_provider_normalizes_response_and_never_exposes_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer iw-secret"
        assert request.headers["x-claw-call-type"] == "normal"
        assert request.headers["x-claw-skill-id"] == "hithink-market-query"
        assert request.headers["x-claw-skill-version"] == "1.0.0"
        assert request.headers["x-claw-plugin-id"] == "none"
        assert request.headers["x-claw-plugin-version"] == "none"
        assert re.fullmatch(r"[0-9a-f]{64}", request.headers["x-claw-trace-id"])
        assert request.url.path == "/v1/query2data"
        payload = json.loads(request.content)
        assert payload["is_cache"] == "1"
        assert payload["expand_index"] == "true"
        assert "source" not in payload
        if "可转债" in payload["query"]:
            return httpx.Response(200, json={"data": [{"证券简称": "示例转债", "转股溢价率": 12.5, "债券评级": "AA+"}]})
        return httpx.Response(200, json={"data": [{"证券简称": "贵州茅台", "最新价": 1500, "涨跌幅": 1.2}]})

    provider = IwencaiSkillHubProvider(
        "iw-secret",
        base_url="https://iwencai.example",
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    facts = await provider.get_quote("600519")
    assert {fact.field for fact in facts} == {"close_price", "change"}
    assert all(fact.source_id == "IWENCAI_SKILLHUB" for fact in facts)
    assert "iw-secret" not in repr(facts)
    bond_facts = await provider.get_convertible_bond("示例转债")
    assert {fact.field for fact in bond_facts} == {"conversion_premium_rate", "bond_rating"}
    assert await main_module.coordinator.understand_intent("请分析示例可转债") is Intent.CONVERTIBLE_BOND_ANALYSIS


@pytest.mark.asyncio
async def test_iwencai_provider_reuses_and_closes_http_client() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"data": [{"证券简称": "示例", "最新价": 10}]})

    provider = IwencaiSkillHubProvider(
        "iw-secret",
        base_url="https://iwencai.example",
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    client = provider._client
    await provider.get_quote("示例")
    await provider.get_quote("示例")

    assert provider._client is client
    assert requests == 2
    assert client.is_closed is False
    await provider.aclose()
    assert client.is_closed is True


def test_llm_default_concurrency_covers_all_portfolio_specialists() -> None:
    config = LLMConfig(base_url="https://llm.example/v1", api_key="secret", model="third-party")

    assert config.max_concurrency == 5


@pytest.mark.asyncio
async def test_iwencai_provider_exposes_selected_skillhub_capabilities() -> None:
    queries: list[str] = []
    paths: list[str] = []
    channels: list[str] = []
    skill_headers: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        query = payload["query"]
        queries.append(query)
        paths.append(request.url.path)
        channels.extend(payload.get("channels", []))
        skill_headers.append(
            (
                request.headers["x-claw-skill-id"],
                request.headers["x-claw-skill-version"],
            )
        )
        if "上市公司公告" in query:
            return httpx.Response(200, json={"data": [{"证券简称": "示例", "公告标题": "重大合同公告"}]})
        if "券商研报" in query:
            return httpx.Response(200, json={"data": [{"证券简称": "示例", "研报标题": "公司研究"}]})
        return httpx.Response(200, json={"data": [{"证券简称": "示例", "最新价": 10}]})

    provider = IwencaiSkillHubProvider(
        "iw-secret",
        base_url="https://iwencai.example",
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    results = await asyncio.gather(
        provider.get_basic_info("示例"),
        provider.get_company_operations("示例"),
        provider.get_shareholder_equity("示例"),
        provider.get_event_data("示例"),
        provider.get_macro_data("中国最近一年"),
        provider.get_institutional_research("示例"),
        provider.get_research_reports("示例"),
        provider.get_announcements("示例"),
        provider.screen_stocks("高 ROE 低负债"),
        provider.screen_sectors("近一月资金净流入"),
    )

    returned_fields = {fact.field for facts in results for fact in facts}
    assert {"announcement", "research_report"} <= returned_fields
    assert paths.count("/v1/comprehensive/search") == 2
    assert set(channels) == {"report", "announcement"}
    assert ("report-search", "2.0.0") in skill_headers
    assert ("announcement-search", "1.0.0") in skill_headers
    assert ("hithink-macro-query", "1.0.0") in skill_headers
    assert ("hithink-astock-selector", "1.0.0") in skill_headers
    combined = "\n".join(queries)
    for marker in (
        "基本资料",
        "主营构成",
        "控股股东",
        "重大事件",
        "宏观数据",
        "机构研究",
        "券商研报",
        "上市公司公告",
        "A股筛选",
        "板块筛选",
    ):
        assert marker in combined


@pytest.mark.asyncio
async def test_iwencai_provider_does_not_retry_invalid_credentials() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, request=request)

    provider = IwencaiSkillHubProvider(
        "iw-secret",
        max_retries=2,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RuntimeError, match="未接受当前密钥"):
        await provider.get_quote("600519")

    assert attempts == 1


@pytest.mark.asyncio
async def test_iwencai_provider_distinguishes_forbidden_capability() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, request=request)

    provider = IwencaiSkillHubProvider(
        "iw-secret",
        max_retries=2,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RuntimeError, match="开通对应数据能力"):
        await provider.get_quote("600519")


@pytest.mark.asyncio
async def test_iwencai_provider_reports_network_failures_without_secrets() -> None:
    attempts = 0
    call_types: list[str] = []
    trace_ids: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        call_types.append(request.headers["x-claw-call-type"])
        trace_ids.append(request.headers["x-claw-trace-id"])
        raise httpx.ConnectError("TLS handshake failed", request=request)

    provider = IwencaiSkillHubProvider(
        "iw-secret",
        max_retries=2,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RuntimeError) as exc_info:
        await provider.get_quote("600519")

    message = str(exc_info.value)
    assert attempts == 3
    assert call_types == ["normal", "retry", "retry"]
    assert len(set(trace_ids)) == 3
    assert all(re.fullmatch(r"[0-9a-f]{64}", trace_id) for trace_id in trace_ids)
    assert "网络、DNS 或代理" in message
    assert "iw-secret" not in message
    assert provider.base_url not in message


@pytest.mark.asyncio
async def test_one_hundred_concurrent_requests_finish_within_smoke_budget(monkeypatch, semantic) -> None:
    monkeypatch.setattr(
        main_module,
        "coordinator",
        CoordinatorAgent(
            agents=make_rule_agents(),
            verifier=verify_facts,
            compliance_checker=basic_compliance_check,
            semantic=semantic,
        ),
    )
    monkeypatch.setattr(main_module, "research_pipeline", AutomatedResearchPipeline(None))
    payload = {
        "query": "请诊断我的持仓组合",
        "profile": {"user_id": "load-test", "risk_level": "R3", "confirmed": True},
        "facts": [make_fact("weight", 0.2).model_dump(mode="json")],
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async def one() -> tuple[int, float]:
            started = time.perf_counter()
            response = await client.post("/api/v1/portfolio/analyze", json=payload)
            return response.status_code, time.perf_counter() - started

        results = await asyncio.gather(*(one() for _ in range(100)))
    latencies = sorted(latency for _, latency in results)
    assert all(status == 200 for status, _ in results)
    assert latencies[94] < 3.0
