"""用户画像草稿、确认与风险等级计算。

问卷打分保留确定性公式，自然语言通过受限 LLM 提取并附带原文证据。文本
只用于收集画像线索，返回的 ``confirmed`` 始终为 False，直到用户显式调用
确认接口。
"""

from __future__ import annotations

from backend.app.semantic import SemanticService
from backend.app.risk_questionnaire import (
    QUESTIONNAIRE_VERSION, QUESTIONS, SCORING_NOTICE, answer_issues, evaluate_answers,
)

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
    if request.questionnaire_version is not None or request.risk_answers:
        if request.questionnaire_version != QUESTIONNAIRE_VERSION:
            raise ValueError("问卷版本已更新，请刷新后重新测评。")
        missing = [q.id for q in QUESTIONS if q.id not in request.risk_answers]
        issues = answer_issues(request.risk_answers)
        if missing or issues:
            return ProfileAssessment(
                profile=UserProfile(user_id=request.user_id, questionnaire_version=QUESTIONNAIRE_VERSION,
                                    risk_answers=request.risk_answers),
                missing_fields=missing + (["answer_consistency"] if issues else []),
                evidence=issues or [f"请完成全部{len(QUESTIONS)}道题后再提交。"],
            )
        values = evaluate_answers(request.risk_answers)
        # 金额、收益与期限保留区间；仅第26题明确的回撤上限写入数值字段。
        profile = UserProfile(user_id=request.user_id, confirmed=False, **values)
        return ProfileAssessment(profile=profile, evidence=[SCORING_NOTICE, "已按前19题计算基础风险分，并按全部答案记录评级依据、财务约束和配置参考。"])

    # 旧 API 保留兼容；新界面只提交逐题答案。
    narrative_patch, evidence = await (semantic or SemanticService()).extract_profile(request.narrative or "")
    missing = [name for name in RISK_WEIGHTS if name not in request.questionnaire]
    score = None
    if not missing:
        score = round(sum(request.questionnaire[name] * weight for name, weight in RISK_WEIGHTS.items()), 2)
        evidence.append("已按五项问卷加权公式计算风险分")
    else:
        evidence.append("问卷维度不完整，未计算风险等级")
    explicit_values = {
        "horizon_months": request.horizon_months,
        "max_drawdown": request.max_drawdown,
        "liquidity_need": request.liquidity_need,
        "target": request.target,
        "investment_experience_years": request.investment_experience_years,
        "investment_history": request.investment_history,
        "holding_history": request.holding_history,
        "expected_annual_return": request.expected_annual_return,
    }
    for field, value in explicit_values.items():
        if value not in (None, "", [], {}):
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
    if profile.questionnaire_version is not None or profile.risk_answers:
        if profile.questionnaire_version != QUESTIONNAIRE_VERSION:
            raise ValueError("问卷版本已更新，请重新测评。")
        # 客户端不能修改分数、等级、有效期或把未答完的草稿直接确认。
        values = evaluate_answers(profile.risk_answers)
        values.update(horizon_months=None,
                      investment_experience_years=None, expected_annual_return=None,
                      investment_history=[], holding_history=[], behavioral_notes=[])
        profile = profile.model_copy(update=values)
    return profile.model_copy(update={"confirmed": True, "version": profile.version + 1})
