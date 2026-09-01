"""用户画像草稿、确认与风险等级计算。

该模块有意不调用 LLM：问卷打分和有限的正则抽取都可复算、可审计。自然语言
只用于收集画像线索，返回的 ``confirmed`` 始终为 False，直到用户显式调用
确认接口。
"""

from __future__ import annotations

import re

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


def _extract_narrative(narrative: str) -> tuple[dict[str, object], list[str]]:
    """提取少量低歧义表达并返回证据说明。

    这里不尝试猜测用户风险等级；例如“稳一点”没有稳定量化含义，只能进入
    ``missing_fields``。每一项提取均附带原始匹配文本，方便前端让用户核对。
    """
    patch: dict[str, object] = {}
    evidence: list[str] = []
    year_match = re.search(r"(\d+)\s*年(?:后|内)", narrative)
    month_match = re.search(r"(\d+)\s*个?月(?:后|内)", narrative)
    if year_match:
        months = int(year_match.group(1)) * 12
        patch["horizon_months"] = months
        evidence.append(f"从“{year_match.group(0)}”提取投资期限 {months} 个月")
    elif month_match:
        months = int(month_match.group(1))
        patch["horizon_months"] = months
        evidence.append(f"从“{month_match.group(0)}”提取投资期限 {months} 个月")

    drawdown_match = re.search(r"(?:最多|最大|不超过|接受)\s*(\d+(?:\.\d+)?)\s*%\s*(?:亏损|回撤|下跌)?", narrative)
    if drawdown_match:
        drawdown = float(drawdown_match.group(1)) / 100
        patch["max_drawdown"] = drawdown
        evidence.append(f"从“{drawdown_match.group(0)}”提取最大回撤 {drawdown:.0%}")

    if re.search(r"买房|购房|学费|医疗|大额支出", narrative):
        patch["liquidity_need"] = "高"
        patch["target"] = "未来存在刚性大额支出"
        evidence.append("识别到刚性大额支出，草稿流动性需求设为高")
    return patch, evidence


def assess_profile(request: ProfileAssessmentRequest) -> ProfileAssessment:
    """根据问卷与文本创建一份未确认画像草稿。

    问卷必须五项齐全才计算加权风险分；部分答题时保留已有线索并明确列出缺失项，
    以免用不完整信息给出看似精确的适当性结论。
    """
    narrative_patch, evidence = _extract_narrative(request.narrative or "")
    missing = [name for name in RISK_WEIGHTS if name not in request.questionnaire]
    score = None
    if not missing:
        score = round(sum(request.questionnaire[name] * weight for name, weight in RISK_WEIGHTS.items()), 2)
        evidence.append("已按五项问卷加权公式计算风险分")
    else:
        evidence.append("问卷维度不完整，未计算风险等级")
    profile = UserProfile(
        user_id=request.user_id,
        risk_score=score,
        risk_level=_risk_level(score),
        confirmed=False,
        **narrative_patch,
    )
    # 手册要求：期限、回撤、流动性等关键字段必须确认。缺失时给予明确下一步。
    for field in ("horizon_months", "max_drawdown", "liquidity_need"):
        if getattr(profile, field) is None:
            missing.append(field)
    return ProfileAssessment(profile=profile, missing_fields=sorted(set(missing)), evidence=evidence)


def confirm_profile(profile: UserProfile) -> UserProfile:
    """确认并版本化画像。

    API 层不持久化用户档案，因此调用方需要提交上一版本；真实部署时应在事务中
    校验版本号并写入数据库，防止两个终端互相覆盖。
    """
    return profile.model_copy(update={"confirmed": True, "version": profile.version + 1})
