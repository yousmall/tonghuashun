"""从当前账号已确认的问卷生成可复算的投资建议与大类资产方案。"""
from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR

from pydantic import BaseModel, ConfigDict, Field

from backend.app.profile_guidance import ALLOCATION_RULE_VERSION, SCENARIOS
from backend.app.risk_questionnaire import (
    QUESTIONNAIRE_VERSION, assessment_is_current, evaluate_answers, local_today,
)

VERSION = "personalized-investment-v1"
NOTICE = "方案依据已确认问卷和平台规则生成，仅供规划参考。收益目标不是预测；情景损失不是最大回撤保证，低波动资产也可能亏损。"


class PersonalizedInvestmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    investment_amount: Decimal | None = Field(default=None, ge=Decimal("0.01"),
        le=Decimal("1000000000000"), max_digits=15, decimal_places=2, allow_inf_nan=False)


def _amounts(amount: Decimal, rows: list[dict]) -> list[float]:
    """按最大余数法分配到分，三类金额合计严格等于本金。"""
    weights = [Decimal(str(row["reference_weight"])) for row in rows]
    total = sum(weights)
    raw = [amount * 100 * weight / total for weight in weights]
    cents = [int(value.to_integral_value(rounding=ROUND_FLOOR)) for value in raw]
    order = sorted(range(len(rows)), key=lambda i: raw[i] - cents[i], reverse=True)
    for i in order[:int(amount * 100) - sum(cents)]:
        cents[i] += 1
    return [float(Decimal(value) / 100) for value in cents]


def build_personalized_investment(profile=None, *, investment_amount=None, today=None) -> dict:
    """不读取行情、不调用模型、不改写测评；展示结论不能反向覆盖原始答案。"""
    today = today or local_today()
    saved = profile.model_dump(mode="json") if hasattr(profile, "model_dump") else dict(profile or {})
    amount = PersonalizedInvestmentRequest(investment_amount=investment_amount).investment_amount
    result = {
        "rule_version": VERSION, "allocation_rule_version": ALLOCATION_RULE_VERSION,
        "generated_on": today.isoformat(), "profile_version": saved.get("version", 1),
        "status": "needs_assessment", "summary": "请完成当前问卷并确认保存后生成个性化方案。",
        "profile_summary": {}, "investment_amount": float(amount) if amount is not None else None,
        "allocation": [], "recommendations": [], "stress_scenarios": [],
        "review_plan": [], "policy_explanations": [], "missing_information": [], "warnings": [], "notice": NOTICE,
    }
    if saved.get("questionnaire_version") != QUESTIONNAIRE_VERSION or not assessment_is_current(saved, today=today):
        result["missing_information"] = ["风险测评未确认、已过期或版本不完整，请完成当前问卷并确认保存。"]
        return result

    # 只信原始答案。不会因旧展示字段被篡改或规则更新而沿用更宽松的配置。
    values = evaluate_answers(saved["risk_answers"], today=today)
    plan = values["financial_plan"]
    answers = saved["risk_answers"]
    result["profile_summary"] = {
        "risk_level": values["risk_level"], "risk_description": values["risk_description"],
        "goal": plan["goal_label"], "horizon": plan["goal_horizon_label"],
        "effective_horizon_max_months": values["effective_horizon_max_months"],
        "capital_range": plan["investment_capital_label"], "liquidity_need": values["liquidity_need"],
        "drawdown_limit": values["max_drawdown"], "return_goal": plan["expected_return_label"],
        "suitable_product_levels": values["suitable_product_levels"],
        "preferred_product_types": values["preferred_product_types"],
        "valid_until": str(saved["valid_until"]),
    }
    result["warnings"] = list(values["financial_warnings"])
    if answers["q27"] == "E":
        result["warnings"].append("年化收益目标超过20%，请结合资金用途复核；本方案不会为满足该目标提高风险。")
    missing = result["missing_information"]
    if answers["q25"] == "E":
        missing.append("请在问卷中明确首要目标的最早用款时间，再生成资产配置。")
    if saved.get("constraints"):
        missing.append("另有已记录限制：" + "；".join(saved["constraints"]) + "。请先复核这些限制与资产类别的兼容性。")
    if amount is not None:
        lower, upper = plan["investment_capital_min"], plan["investment_capital_max"]
        # 问卷金额桶为：不足5万、5万至不足20万……100万及以上。
        if amount < lower or upper is not None and amount >= upper:
            missing.append("本次投资金额不在已确认问卷的资金区间内，请更新问卷并确认后重新生成。")
    if missing:
        result.update(status="needs_information", summary="请补充或核对以下信息后生成资产配置方案。")
        return result

    rows = [dict(row) for row in values["allocation_guidance"]]
    if amount is not None:
        for row, allocated in zip(rows, _amounts(amount, rows), strict=True):
            row["reference_amount"] = allocated
    result.update(status="ready", allocation=rows,
        summary=f"围绕“{plan['goal_label']}”，按{values['risk_description']}承受能力安排资金，优先满足{plan['goal_horizon_label']}的用款需求。")
    result["policy_explanations"] = list(plan["policy_explanations"])

    def advise(title, action, basis, qids):
        result["recommendations"].append({"title": title, "action": action, "basis": basis, "question_ids": qids})

    cash_floor = rows[2]["min_weight"]
    advise("先安排必要支出与应急资金",
        f"本方案流动资金底线为{cash_floor:.0%}；先隔离必要支出，并核对应急储备能否覆盖家庭开支。现金及低波动类中的具体工具仍须核对到账时间和本金风险。",
        f"必要支出：{plan['near_term_spending_label']}；已有应急储备：{plan['emergency_reserve_label']}。",
        ["q21", "q22"])
    if answers["q23"] in ("C", "D", "E"):
        advise("优先复核偿债安排", "按实际还款日保留偿债资金；在负担改善前控制新增风险投资，避免借款投资。",
            f"每月还款负担：{plan['debt_service_label']}。", ["q23"])
    if answers["q01"] == "E":
        advise("核对收入持续性", "先确认生活费来源和可持续投入金额，再安排长期投资。",
            "问卷记录无固定收入，已提高流动性底线并限制权益暴露。", ["q01"])
    equity = rows[0]["reference_weight"]
    advise("按用款期限选择工具",
        ("本方案权益配置为0；重点核对固收及现金类工具的实际风险、持有期和赎回安排。" if equity == 0 else
         "权益部分优先研究分散的宽基工具；固收部分关注信用质量、久期和赎回条件。入场前逐一核对产品适当性，可分批安排并控制交易成本。"),
        f"承受能力：{values['risk_level']}；最早用款：{plan['goal_horizon_label']}；回撤选择：{plan['drawdown_label']}。",
        ["q11", "q15", "q25", "q26"])
    advise("收益目标与资金用途一起复核", "收益目标偏高时，先调整目标、投入金额或期限，再评估配置；不为追求目标自动提高风险。",
        f"您的期望：{plan['expected_return_label']}。", ["q24", "q27"])
    single = saved.get("single_security_limit", .20)
    industry = saved.get("industry_limit", .30)
    advise("落实分散与集中度限制",
        f"单一证券占总资产不超过{single:.0%}，单一行业不超过{industry:.0%}；大类配置下还需按真实持仓核对穿透暴露。",
        "采用账号已记录的单标的及行业上限；本方案未读取当前持仓，不生成具体买卖数量。", [])
    trading = saved.get("trading_analysis") or {}
    if trading.get("confirmed") and trading.get("behavioral_notes"):
        advise("结合历史交易改进执行", "定期复核交易频率与成本，避免因短期波动追涨杀跌。历史交易额占比不能替代当前持仓权重。",
            "已确认交易记录摘要：" + "；".join(trading["behavioral_notes"][:3]), [])
    for title, shocks in SCENARIOS:
        loss = round(-sum(row["reference_weight"] * shock for row, shock in zip(rows, shocks, strict=True)), 6)
        result["stress_scenarios"].append({"name": title,
            "assumptions": {row["asset_class"]: shock for row, shock in zip(rows, shocks, strict=True)},
            "estimated_loss": loss, "loss_amount": round(float(amount) * loss, 2) if amount is not None else None,
            "exceeds_tolerance": values["max_drawdown"] is not None and loss > values["max_drawdown"] + 1e-8})
    result["review_plan"] = [
        {"when": "实施前", "action": "先预留必用资金，再核对每项产品的风险、费用、期限和赎回到账时间。"},
        {"when": "每3个月", "action": "复核实际大类占比和集中度；偏离参考占比超过5个百分点时人工评估再平衡，结合费用决定是否调整。该阈值为平台复核规则。"},
        {"when": "用款前", "action": "按实际用款日倒排持有期及赎回到账时间，逐步安排所需流动资金。"},
        {"when": "收入、负债或目标变化时", "action": "重新完成并确认问卷，再生成方案；风险测评到期后同样需要更新。"},
    ]
    return result
