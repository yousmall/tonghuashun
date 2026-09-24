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
from backend.app.agents.rule_agents import make_rule_agents
from backend.app.models import FactRecord, OrchestrationRequest, UserProfile


def make_coordinator(semantic) -> CoordinatorAgent:
    """装配确定性依赖，避免测试依赖外部模型、行情接口或网络。"""
    return CoordinatorAgent(make_rule_agents(), verify_facts, basic_compliance_check, semantic=semantic)


@pytest.mark.asyncio
async def test_portfolio_request_builds_parallel_specialist_dag(semantic) -> None:
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
    output = await make_coordinator(semantic).run(request)

    assert len([node for node in output.task_plan.nodes if node.agent_id in {"market", "industry", "security", "fund", "portfolio"}]) == 5
    assert output.compliance.status == "PASS"
    assert output.evidence == ["F-1"]
    # 专业、事实和合规节点都必须写回状态，才能按手册要求在协作过程页可视化。
    assert all(node.status != "pending" for node in output.task_plan.nodes)


@pytest.mark.asyncio
async def test_one_authorized_source_does_not_force_risk_review(semantic) -> None:
    request = OrchestrationRequest(
        query="请诊断我的持仓组合",
        profile=UserProfile(user_id="u-one-source", risk_level="R3", confirmed=True),
        facts=[FactRecord(
            fact_id="F-weight", entity="示例ETF", field="weight", value=0.2,
            snapshot_time=datetime.now(timezone.utc),
            source_id="IWENCAI_SKILLHUB", quality=0.9,
        )],
    )
    output = await make_coordinator(semantic).run(request)
    assert output.cross_validation.status == "PASS"
    assert output.compliance.status == "PASS"


@pytest.mark.asyncio
async def test_unconfirmed_profile_requires_review(semantic) -> None:
    """画像尚未确认时，系统只能追问/复核，不能擅自生成个性化建议。"""
    request = OrchestrationRequest(
        query="帮我看看持仓",
        profile=UserProfile(user_id="u-1", confirmed=False),
    )
    output = await make_coordinator(semantic).run(request)

    assert output.compliance.status == "REVIEW"
    assert output.cross_validation.status == "REVIEW"
    assert "确认" in output.conclusion


@pytest.mark.asyncio
async def test_return_promise_is_blocked(semantic) -> None:
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
    output = await make_coordinator(semantic).run(request)

    assert output.compliance.status == "BLOCK"
    assert output.confidence == 0



@pytest.mark.asyncio
async def test_one_source_risk_conclusion_is_bounded(semantic) -> None:
    request = OrchestrationRequest(
        query="请诊断我的持仓组合",
        profile=UserProfile(user_id="u-risk", risk_level="R3", confirmed=True),
        facts=[FactRecord(
            fact_id="F-risk", entity="示例ETF", field="weight", value=0.2,
            snapshot_time=datetime.now(timezone.utc),
            source_id="IWENCAI_SKILLHUB", quality=0.9,
        )],
    )
    output = await make_coordinator(semantic).run(request)
    assert output.cross_validation.status == "PASS"
    assert output.compliance.status == "PASS"
    assert "未触发已配置的硬性风险规则" in output.risk_conclusion
    assert "不表示标的没有投资风险" in output.risk_conclusion


@pytest.mark.asyncio
async def test_concentration_risk_conclusion_uses_confirmed_limit(semantic) -> None:
    request = OrchestrationRequest(
        query="请诊断我的持仓组合",
        profile=UserProfile(
            user_id="u-concentrated", risk_level="R3", confirmed=True,
            single_security_limit=0.3,
        ),
        portfolio=[{"symbol": "示例ETF", "weight": 0.5}],
    )
    output = await make_coordinator(semantic).run(request)
    assert output.compliance.status == "REVIEW"
    assert "集中度风险" in output.risk_conclusion
    assert "比例上限" in output.risk_conclusion


@pytest.mark.asyncio
async def test_same_source_conflict_prevents_certain_risk_conclusion(semantic) -> None:
    timestamp = datetime.now(timezone.utc)
    request = OrchestrationRequest(
        query="请诊断我的持仓组合",
        profile=UserProfile(user_id="u-conflict", risk_level="R3", confirmed=True),
        facts=[
            FactRecord(
                fact_id=f"F-conflict-{value}", entity="示例ETF",
                field="weight", value=value, snapshot_time=timestamp,
                source_id="IWENCAI_SKILLHUB", quality=0.9,
            )
            for value in (0.2, 0.25)
        ],
    )
    output = await make_coordinator(semantic).run(request)
    assert output.cross_validation.status == "REVIEW"
    assert "INTERNAL_VALUE_CONFLICT" in {issue.code for issue in output.cross_validation.issues}
    assert "待核对" in output.risk_conclusion
    assert "规则未触发" not in output.risk_conclusion



@pytest.mark.asyncio
async def test_missing_skill_data_cannot_produce_positive_risk_conclusion(semantic) -> None:
    request = OrchestrationRequest(
        query="请诊断我的持仓组合",
        profile=UserProfile(user_id="u-no-data", risk_level="R3", confirmed=True),
    )
    output = await make_coordinator(semantic).run(request)
    assert output.cross_validation.status == "REVIEW"
    assert output.compliance.status == "REVIEW"
    assert output.evidence == []
    assert "缺少通过核验的资料引用" in output.risk_conclusion
