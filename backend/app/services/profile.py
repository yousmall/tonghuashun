"""用户画像草稿、确认与风险等级计算。

问卷打分保留确定性公式，自然语言通过受限 LLM 提取并附带原文证据。文本
只用于收集画像线索，返回的 ``confirmed`` 始终为 False，直到用户显式调用
确认接口。
"""

from __future__ import annotations

from backend.app.semantic import SemanticService

from backend.app.models import (
    ProfileAssessment,
    ProfileAssessmentRequest,
    UserProfile,
)

# 手册给出的五项风险分权重。键名也是 /profile/assess 的 questionnaire 参数名。
RISK_WEIGHTS = {
    "financial_capacity": 0.30,
    "loss_tolerance": 0.25,
    "investment_horizon": 0.20,
    "knowledge_experience": 0.15,
    "behavior_stability": 0.10,
}


def _risk_level(score: float | None) -> str | None:
    """把可追溯的 0-100 分映射为 R1-R5；缺少问卷不能假装有等级。"""
    if score is None:
        return None
    return ("R1", "R2", "R3", "R4", "R5")[min(4, int(score // 20))]


async def assess_profile(request: ProfileAssessmentRequest, semantic: SemanticService | None = None) -> ProfileAssessment:
    """根据问卷与文本创建一份未确认画像草稿。

    问卷必须五项齐全才计算加权风险分；部分答题时保留已有线索并明确列出缺失项，
    以免用不完整信息给出看似精确的适当性结论。
    """
    narrative_patch, evidence = await (semantic or SemanticService()).extract_profile(request.narrative or "")
    missing = [name for name in RISK_WEIGHTS if name not in request.questionnaire]
    score = None
    if not missing:
        score = round(sum(request.questionnaire[name] * weight for name, weight in RISK_WEIGHTS.items()), 2)
        evidence.append("已按五项问卷加权公式计算风险分")
    else:
        evidence.append("问卷维度不完整，未计算风险等级")
    explicit_values = {
        "investment_experience_years": request.investment_experience_years,
        "investment_history": request.investment_history,
        "holding_history": request.holding_history,
        "expected_annual_return": request.expected_annual_return,
    }
    for field, value in explicit_values.items():
        if value not in (None, [], {}):
            narrative_patch[field] = value
            evidence.append(f"已记录用户明确提交的 {field}")

    profile = UserProfile(
        user_id=request.user_id,
        risk_score=score,
        risk_level=_risk_level(score),
        confirmed=False,
        **narrative_patch,
    )
    # 手册要求：期限、回撤、流动性等关键字段必须确认。缺失时给予明确下一步。
    for field in (
        "horizon_months",
        "max_drawdown",
        "liquidity_need",
        "investment_experience_years",
        "expected_annual_return",
    ):
        if getattr(profile, field) is None:
            missing.append(field)
    return ProfileAssessment(profile=profile, missing_fields=sorted(set(missing)), evidence=evidence)


def confirm_profile(profile: UserProfile) -> UserProfile:
    """确认并版本化画像。

    API 层不持久化用户档案，因此调用方需要提交上一版本；真实部署时应在事务中
    校验版本号并写入数据库，防止两个终端互相覆盖。
    """
    return profile.model_copy(update={"confirmed": True, "version": profile.version + 1})
