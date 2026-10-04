"""由已作答问卷生成可追溯的财务约束、配置区间与假设情景。

这是平台确定性规则，不调用社区技能，不估计用户未提供的精确金额或收益率。
情景跌幅是公开标注的假设，不能当作预测、产品风险评级或回撤保证。
"""
from __future__ import annotations

from typing import Any


ALLOCATION_RULE_VERSION = "financial-constraints-v1"
BASE_ALLOCATION_RANGES = {
    "R1": ((0, .20), (.60, .90), (.10, .30)),
    "R2": ((.20, .40), (.40, .70), (.10, .25)),
    "R3": ((.40, .60), (.25, .50), (.05, .20)),
    "R4": ((.60, .80), (.10, .30), (.05, .15)),
    "R5": ((.75, .95), (0, .20), (0, .10)),
}
ASSET_CLASSES = ("权益类", "固收类", "现金及低波动类")
SCENARIOS = (
    ("假设情景一", (-.20, -.05, 0)),
    ("假设情景二", (-.40, -.10, 0)),
)


def build_profile_guidance(answers: dict[str, str], values: dict[str, Any]) -> dict[str, Any]:
    """输入完整有效问卷；所有输出在服务端确认时从答案重建。"""
    def index(qid: str) -> int:
        return ord(answers[qid]) - 65

    details = values["questionnaire_details"]
    labels = {f"q{i:02}": label for i, label in enumerate(details.values(), 1)}
    capital_min, capital_max = ((0, 50_000), (50_000, 200_000), (200_000, 500_000),
                                (500_000, 1_000_000), (1_000_000, None))[index("q20")]
    spending_min, spending_max = ((0, 0), (0, .10), (.10, .30), (.30, .50), (.50, 1))[index("q21")]
    emergency_min, emergency_max = ((0, 1), (1, 3), (3, 6), (6, None))[index("q22")]
    debt_min, debt_max = ((0, 0), (0, .20), (.20, .40), (.40, .60), (.60, None))[index("q23")]
    goal_min, goal_max = ((0, 12), (12, 36), (36, 60), (60, None), (None, None))[index("q25")]
    drawdown = (0, .05, .10, .20, None)[index("q26")]
    return_min, return_max = ((None, None), (None, .05), (.05, .10), (.10, .20), (.20, None))[index("q27")]
    horizons = [n for n in (values["horizon_max_months"], goal_max) if n is not None]
    horizon_max = min(horizons) if horizons else None
    warnings: list[str] = []
    followups: list[str] = []
    policies: list[str] = []
    equity_cap = 1.0
    reserve_floor = spending_max
    if answers["q15"] == "A":
        equity_cap = 0
        policies.append("第15题本金安全优先：平台不安排权益资金；固收类仍须核对实际产品风险。")
    if spending_max:
        policies.append(f"第21题必要支出比例为{labels['q21']}；平台按区间上限预留{spending_max:.0%}，确认具体金额后可重新评估。")
    if answers["q21"] == "E":
        warnings.append("未来一年必用资金超过投资资金的一半；在金额未细化前，优先保留全部资金的可用性。")
        followups.append("请核对未来一年必要支出的具体金额；当前按比例区间上限预留资金。")
    if emergency_min < 3:
        reserve_floor = max(reserve_floor, .30)
        equity_cap = min(equity_cap, .20)
        warnings.append(f"第22题应急储备为{labels['q22']}，优先补足应急资金。")
        policies.append("应急储备不足3个月：平台提高流动性底线至30%，权益上限收紧至20%。")
    elif emergency_min < 6:
        warnings.append("应急储备不足6个月，请结合家庭收入稳定性复核储备需求。")
    if index("q23") >= 3:
        reserve_floor = max(reserve_floor, .50)
        equity_cap = min(equity_cap, .20 if answers["q23"] == "D" else 0)
        warnings.append(f"第23题还款负担为{labels['q23']}，应优先核对偿债安排。")
        policies.append("还款负担超过40%：平台提高流动性底线至50%；超过60%或收入不足时不安排权益资金。")
    if answers["q01"] == "E":
        reserve_floor = max(reserve_floor, .30)
        equity_cap = min(equity_cap, .20)
        warnings.append("第1题无固定收入，应先核对生活开支和资金来源的持续性。")
    if answers["q02"] == "A":
        warnings.append("第2题证券投资占家庭资产70%以上，请关注家庭资产集中风险。")
    if horizon_max is not None and horizon_max <= 12:
        equity_cap = 0
        policies.append("最早用款期限不超过1年：平台不安排权益资金，优先匹配到期和赎回时间。")
    elif horizon_max is not None and horizon_max <= 36:
        equity_cap = min(equity_cap, .30)
        policies.append("用款期限不超过3年：平台将权益上限收紧至30%。")
    if goal_max is not None and values["horizon_max_months"] is not None and goal_max < values["horizon_max_months"]:
        warnings.append("目标用款时间早于第11题拟投资期限，配置与产品期限按更短的用款时间约束。")
    if goal_min is not None and values["horizon_max_months"] is not None and goal_min >= values["horizon_max_months"]:
        warnings.append("规划目标与拟投资期限存在跨度差异，请核对是否需要分开安排不同用途的资金。")
    if answers["q25"] == "E":
        followups.append("规划目标的最早用款时间尚未明确，请在购买有持有期的产品前确认。")
    if drawdown == 0:
        reserve_floor = 1.0
        equity_cap = 0
        warnings.append("不能接受回撤：风险档位按保守型处理；低波动产品也不能保证本金或零回撤。")
    if drawdown is not None:
        policies.append("平台按假设情景一（权益跌20%、固收跌5%、现金类不变）进一步收紧配置，使示例损失不超过已选择的回撤上限；这不是实际回撤保证。")
    if (answers["q27"] in ("D", "E") and
            (drawdown is not None and drawdown <= .10 or values["risk_level"] in ("R1", "R2"))):
        warnings.append("较高收益目标与较低损失边界存在张力，不能保证同时实现，请复核收益预期。")
    if answers["q26"] == "E":
        followups.append("第26题未设明确回撤上限，暂不推算具体百分比。")
    liquidity = "高" if (reserve_floor >= .30 or horizon_max is not None and horizon_max <= 12) else (
        "中" if spending_max > 0 or emergency_min < 6 or horizon_max is not None and horizon_max <= 36 else "低")
    capacity = "受约束" if reserve_floor >= .30 or equity_cap == 0 else (
        "需关注" if emergency_min < 6 or index("q23") >= 2 else "相对充足")

    plan = {
        "rule_version": ALLOCATION_RULE_VERSION,
        "investment_capital_label": labels["q20"], "investment_capital_min": capital_min,
        "investment_capital_max": capital_max, "near_term_spending_label": labels["q21"],
        "near_term_spending_ratio_min": spending_min, "near_term_spending_ratio_max": spending_max,
        "emergency_reserve_label": labels["q22"], "emergency_months_min": emergency_min,
        "emergency_months_max": emergency_max, "debt_service_label": labels["q23"],
        "debt_service_ratio_min": debt_min, "debt_service_ratio_max": debt_max,
        "goal_label": labels["q24"], "goal_horizon_label": labels["q25"],
        "goal_horizon_min_months": goal_min, "goal_horizon_max_months": goal_max,
        "drawdown_label": labels["q26"], "expected_return_label": labels["q27"],
        "expected_return_min": return_min, "expected_return_max": return_max,
        "cash_floor": reserve_floor, "equity_cap": equity_cap, "policy_explanations": policies,
        "followups": followups,
    }
    enriched = dict(values, financial_plan=plan, max_drawdown=drawdown, liquidity_need=liquidity,
                    effective_horizon_max_months=horizon_max, financial_capacity_label=capacity)
    allocation = allocation_for_profile(enriched)
    stress = []
    for name, shocks in SCENARIOS:
        loss = round(-sum(row["reference_weight"] * shock for row, shock in zip(allocation, shocks, strict=True)), 6)
        stress.append({"name": name, "assumptions": dict(zip(ASSET_CLASSES, shocks, strict=True)),
                       "estimated_loss": loss, "exceeds_tolerance": drawdown is not None and loss > drawdown + 1e-8})
    if any(s["exceeds_tolerance"] for s in stress):
        warnings.append("配置参考在较大跌幅假设下仍可能超过您的回撤边界；请降低风险暴露或复核目标，情景测算不是损失上限保证。")
    groups = (
        ("财务承受能力", capacity, ("q01", "q02", "q03", "q04", "q19", "q20", "q21", "q22", "q23")),
        ("风险意愿与回撤边界", f"损失偏好：{values['loss_tolerance_label']}；回撤选择：{labels['q26']}", ("q13", "q14", "q15", "q26")),
        ("投资经验", values["experience_label"], ("q05", "q06", "q09", "q18")),
        ("目标与用款期限", f"{labels['q24']}；最早用款：{labels['q25']}", ("q11", "q16", "q24", "q25")),
        ("收益预期", labels["q27"], ("q27",)),
    )
    reasons = [{"dimension": dimension, "summary": summary, "question_ids": list(qids),
                "answers": [f"第{int(qid[1:])}题：{labels[qid]}" for qid in qids]} for dimension, summary, qids in groups]
    return {
        "financial_plan": plan, "financial_capacity_label": capacity, "liquidity_need": liquidity,
        "effective_horizon_max_months": horizon_max, "max_drawdown": drawdown,
        "target": labels["q24"], "assessment_reasons": reasons,
        "financial_warnings": warnings, "allocation_guidance": allocation, "stress_scenarios": stress,
    }


def allocation_for_profile(profile: Any) -> list[dict[str, Any]]:
    """生成相互可行的区间以及合计100%的示例配置，规则版本及假设均显式记录。"""
    get = profile.get if isinstance(profile, dict) else lambda key, default=None: getattr(profile, key, default)
    risk_level = get("risk_level")
    if risk_level not in BASE_ALLOCATION_RANGES:
        return []
    base = BASE_ALLOCATION_RANGES[risk_level]
    plan = get("financial_plan") or {}
    equity_cap = min(base[0][1], plan.get("equity_cap", 1))
    cash_min = max(base[2][0], plan.get("cash_floor", 0))
    if get("preferred_product_types") == ["固定收益类"]:
        equity_cap = 0
    horizon = get("effective_horizon_max_months")
    if horizon is None:
        horizon = get("horizon_max_months") or get("horizon_months")
    if horizon is not None and horizon <= 12:
        equity_cap = 0
    elif horizon is not None and horizon <= 36:
        equity_cap = min(equity_cap, .30)
    if get("liquidity_need") == "高":
        cash_min = max(cash_min, .30)
    drawdown = get("max_drawdown")
    if drawdown is not None:
        # 假设情景一：权益跌20%、固收跌5%。按现金底线测算保守权益上限。
        cash_min = max(cash_min, 1 - drawdown / .05)
        equity_cap = min(equity_cap, max(0, (drawdown - .05 * (1 - cash_min)) / .15))
    cash_min = min(1, max(0, cash_min))
    equity_max = min(equity_cap, 1 - cash_min)
    equity_min = min(base[0][0], equity_max)
    cash_max = min(1 - equity_min, max(base[2][1], cash_min))
    fixed_min = max(0, 1 - equity_max - cash_max)
    fixed_max = max(0, 1 - equity_min - cash_min)
    equity_reference = (equity_min + equity_max) / 2
    cash_reference = min((cash_min + cash_max) / 2, 1 - equity_reference)
    references = (equity_reference, 1 - equity_reference - cash_reference, cash_reference)
    ranges = ((equity_min, equity_max), (fixed_min, fixed_max), (cash_min, cash_max))
    basis = f"问卷画像 {risk_level}；平台约束区间，需确认画像并结合产品风险、期限和集中度复核"
    return [{"asset_class": name, "min_weight": round(lower, 6), "max_weight": round(upper, 6),
             "reference_weight": round(reference, 6), "basis": basis,
             "rule_version": ALLOCATION_RULE_VERSION} for name, (lower, upper), reference in zip(ASSET_CLASSES, ranges, references, strict=True)]
