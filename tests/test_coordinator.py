"""主协调智能体的关键安全路径测试。

测试重点不是验证金融结论，而是验证“并行规划、画像确认、事实引用、收益承诺
拦截”这些不能被后续重构破坏的控制链路。
"""

from datetime import datetime, timezone

import pytest

from backend.app.agents.coordinator import (
    CoordinatorAgent,
    basic_compliance_check,
    verify_facts,
)
from backend.app.agents.demo_agents import make_fact_based_agent
from backend.app.models import FactRecord, OrchestrationRequest, UserProfile


def make_coordinator() -> CoordinatorAgent:
    """装配确定性依赖，避免测试依赖外部模型、行情接口或网络。"""
    agents = {
        name: make_fact_based_agent(name)
        for name in ("market", "industry", "security", "fund", "portfolio")
    }
    return CoordinatorAgent(agents, verify_facts, basic_compliance_check)


@pytest.mark.asyncio
async def test_portfolio_request_builds_parallel_specialist_dag() -> None:
    """组合诊断应计划五类专业能力，并保留经核验的证据。"""
    request = OrchestrationRequest(
        query="请诊断我的持仓组合",
        profile=UserProfile(user_id="u-1", risk_level="R3", confirmed=True),
        facts=[
            FactRecord(
                fact_id="F-1",
                entity="示例ETF",
                field="weight",
                value=0.4,
                snapshot_time=datetime.now(timezone.utc),
                source_id="DEMO_SNAPSHOT",
                quality=0.8,
            )
        ],
    )
    output = await make_coordinator().run(request)

    assert len([node for node in output.task_plan.nodes if node.agent_id in {"market", "industry", "security", "fund", "portfolio"}]) == 5
    assert output.compliance.status == "PASS"
    assert output.evidence == ["F-1"]


@pytest.mark.asyncio
async def test_unconfirmed_profile_requires_review() -> None:
    """画像尚未确认时，系统只能追问/复核，不能擅自生成个性化建议。"""
    request = OrchestrationRequest(
        query="帮我看看持仓",
        profile=UserProfile(user_id="u-1", confirmed=False),
    )
    output = await make_coordinator().run(request)

    assert output.compliance.status == "REVIEW"
    assert "确认" in output.conclusion


@pytest.mark.asyncio
async def test_return_promise_is_blocked() -> None:
    """收益承诺属于硬拦截，无论专业智能体是否产生结果都不可放行。"""
    request = OrchestrationRequest(
        query="推荐稳赚股票",
        profile=UserProfile(user_id="u-1", risk_level="R3", confirmed=True),
        facts=[
            FactRecord(
                fact_id="F-1",
                entity="示例",
                field="value",
                value=1,
                snapshot_time=datetime.now(timezone.utc),
                source_id="DEMO_SNAPSHOT",
                quality=0.8,
            )
        ],
    )
    output = await make_coordinator().run(request)

    assert output.compliance.status == "BLOCK"
    assert output.confidence == 0
