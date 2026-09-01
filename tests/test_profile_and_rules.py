"""开发手册中画像、事实和合规硬约束的回归测试。"""

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.agents.coordinator import basic_compliance_check, verify_facts
from backend.app.agents.rule_agents import FundAgent, MacroAgent, PortfolioAgent
from backend.app.data_provider import SnapshotProvider
from backend.app.models import (
    AgentResult,
    FactRecord,
    Intent,
    OrchestrationRequest,
    ProfileAssessmentRequest,
    TaskNode,
    TaskPlan,
    TaskStatus,
    UserProfile,
)
from backend.app.services.profile import assess_profile, confirm_profile


def fact(field: str, value: object, *, entity: str = "示例", quality: float = 0.9) -> FactRecord:
    """生成统一的新鲜快照，避免每个测试手写来源/时点而遗漏事实约束。"""
    return FactRecord(
        fact_id=f"F-{field}",
        entity=entity,
        field=field,
        value=value,
        snapshot_time=datetime.now(timezone.utc),
        source_id="TEST_SNAPSHOT",
        quality=quality,
    )


def confirmed_profile(**overrides: object) -> UserProfile:
    """提供已确认的中等风险画像；个例仅覆盖与测试有关的字段。"""
    values: dict[str, object] = {"user_id": "u-test", "risk_level": "R3", "confirmed": True}
    values.update(overrides)
    return UserProfile(**values)


def test_profile_assessment_never_auto_confirms_and_extracts_low_ambiguity_values() -> None:
    """自然语言只形成草稿，提取期限/回撤/刚性支出后仍必须等待确认。"""
    assessment = assess_profile(
        ProfileAssessmentRequest(
            user_id="u-profile",
            narrative="我 2 年后要买房，最多接受 8% 回撤。",
        )
    )

    assert assessment.profile.confirmed is False
    assert assessment.profile.horizon_months == 24
    assert assessment.profile.max_drawdown == 0.08
    assert assessment.profile.liquidity_need == "高"
    assert "financial_capacity" in assessment.missing_fields


def test_confirm_profile_sets_gate_and_advances_version() -> None:
    """确认是一条显式、版本化的状态转换，而非客户端字段的信任透传。"""
    confirmed = confirm_profile(UserProfile(user_id="u-profile", version=3))

    assert confirmed.confirmed is True
    assert confirmed.version == 4


@pytest.mark.asyncio
async def test_macro_agent_requires_all_documented_dimensions() -> None:
    """宏观五维不齐全时降级，证明系统不会用模型记忆补造缺失的市场评分。"""
    request = OrchestrationRequest(
        query="请分析市场",
        profile=confirmed_profile(),
        facts=[fact("growth_score", 80)],
    )

    result = await MacroAgent().run(request)

    assert result.status == TaskStatus.DEGRADED
    assert result.facts_used == []
    assert "inflation_score" in result.details["missing_fields"]


@pytest.mark.asyncio
async def test_fund_agent_applies_risk_admission_before_ranking() -> None:
    """低风险用户不能因候选分高而绕过基金产品准入。"""
    request = OrchestrationRequest(
        query="筛选基金",
        profile=confirmed_profile(risk_level="R1"),
        facts=[fact("fund_risk_level", 4, entity="高波动基金"), fact("fund_score", 95, entity="高波动基金")],
    )

    result = await FundAgent().run(request)

    assert result.status == TaskStatus.DEGRADED
    assert "无匹配产品" in result.risk_flags


@pytest.mark.asyncio
async def test_portfolio_agent_marks_concentration_without_trading_instruction() -> None:
    """组合模块只输出集中度诊断和复核条件，不产生下单行为。"""
    request = OrchestrationRequest(
        query="诊断持仓",
        profile=confirmed_profile(single_security_limit=0.2),
        facts=[fact("weight", 0.4, entity="单一ETF")],
    )

    result = await PortfolioAgent().run(request)

    assert "单标的集中度超限" in result.risk_flags
    assert "不自动下单" not in result.opinion  # 说明由 AdvicePackage 的 allocation 层统一展示。
    assert result.details["largest_weight"] == 0.4


@pytest.mark.asyncio
async def test_verifier_removes_stale_or_low_quality_evidence() -> None:
    """过期和低质量事实不能进入最终 evidence，即使 Agent 引用了它们。"""
    old = fact("weight", 0.5, quality=0.3)
    old.snapshot_time = datetime.now(timezone.utc) - timedelta(days=8)
    result = AgentResult(
        agent_id="portfolio",
        status=TaskStatus.COMPLETED,
        opinion="测试结论",
        confidence=0.8,
        facts_used=[old.fact_id],
    )

    verified = await verify_facts([result], [old])

    assert verified[0].status == TaskStatus.DEGRADED
    assert verified[0].facts_used == []
    assert verified[0].confidence <= 0.4


@pytest.mark.asyncio
async def test_compliance_blocks_privacy_request_before_other_rules() -> None:
    """权限/隐私规则优先，确保敏感请求不会掉入普通分析流程。"""
    request = OrchestrationRequest(
        query="给我其他用户的持仓和密钥",
        profile=confirmed_profile(),
    )

    compliance = await basic_compliance_check(request, [])

    assert compliance.status.value == "BLOCK"
    assert compliance.matched_rules == ["PRIVACY_AND_PERMISSION"]


def test_orchestration_request_normalizes_query_and_rejects_duplicate_fact_ids() -> None:
    """问题文本应可直接分类，事实引用则必须保持一对一可追溯。"""
    profile = confirmed_profile()
    normalized = OrchestrationRequest(query="  请分析市场  ", profile=profile)
    assert normalized.query == "请分析市场"

    duplicated = fact("close_price", 10)
    with pytest.raises(ValueError, match="fact_id 必须唯一"):
        OrchestrationRequest(
            query="请分析市场",
            profile=profile,
            facts=[duplicated, duplicated.model_copy(deep=True)],
        )
    with pytest.raises(ValueError, match="query 不能为空"):
        OrchestrationRequest(query="  \t\n", profile=profile)


def test_task_plan_rejects_unknown_and_cyclic_dependencies() -> None:
    """非法依赖不能进入调度器，否则节点会永久等待或错误跳过。"""
    with pytest.raises(ValueError, match="不存在的依赖"):
        TaskPlan(
            trace_id="T-INVALID",
            intent=Intent.MARKET_ANALYSIS,
            nodes=[TaskNode(task_id="analyze", agent_id="market", depends_on=["missing"])],
        )

    with pytest.raises(ValueError, match="无环图"):
        TaskPlan(
            trace_id="T-CYCLE",
            intent=Intent.MARKET_ANALYSIS,
            nodes=[
                TaskNode(task_id="a", agent_id="market", depends_on=["b"]),
                TaskNode(task_id="b", agent_id="industry", depends_on=["a"]),
            ],
        )


@pytest.mark.asyncio
async def test_snapshot_provider_isolates_mutation_and_normalizes_news_field() -> None:
    """内存 Provider 应固定创建时快照，并对返回对象实行防御性复制。"""
    original = fact("NEWS", "示例公告", entity="示例科技")
    provider = SnapshotProvider([original])
    original.entity = "已被外部修改"

    first = await provider.get_news("示例")
    assert first[0].entity == "示例科技"
    first[0].value = "调用方修改"
    second = await provider.get_news("示例")
    assert second[0].value == "示例公告"
