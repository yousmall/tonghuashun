"""Form comparable observations from the exact watchlist group and verified facts."""
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
from math import isclose

from backend.app.fact_taxonomy import FIELD_LABELS
from backend.app.fact_units import PERCENT_FIELDS, period_info
from backend.app.models import AgentResult, Intent, TaskStatus
from backend.app.numeric import finite_number
from backend.app.services.comparison import matching_fact

FIELDS = {
    Intent.SECURITY_RESEARCH: ("roe", "roe_weighted", "pe_ttm", "pb", "net_profit_growth", "revenue_growth", "change"),
    Intent.INDUSTRY_ANALYSIS: ("pe_ttm", "industry_revenue_growth", "change", "capital_flow", "turnover_value"),
    Intent.MARKET_ANALYSIS: ("change", "turnover_value"),
    Intent.FUND_SCREENING: ("tracking_error", "fee_rate", "nav_change", "fund_size"),
    Intent.CONVERTIBLE_BOND_ANALYSIS: ("conversion_premium_rate", "yield_to_maturity", "pure_bond_premium_rate", "change", "remaining_size"),
}
LABELS = {
    **FIELD_LABELS, "nav_change": "净值涨跌幅", "fund_size": "基金规模",
    "conversion_premium_rate": "转股溢价率", "pure_bond_premium_rate": "纯债溢价率",
    "yield_to_maturity": "到期收益率", "remaining_size": "剩余规模",
    "capital_flow": "资金净流入", "turnover_value": "成交额",
    "fund_nav": "单位净值",
}
NOTES = {
    "roe": "ROE 较高表示该期每单位净资产对应的利润较多，跨行业比较时还需核对杠杆、盈利模式和现金流。",
    "roe_weighted": "加权 ROE 还会受到权益变动影响，可结合杠杆、盈利持续性和经营现金流核对。",
    "pe_ttm": "市盈率衡量价格相对滚动盈利的倍数。若有亏损标的或经营模式差异，估值倍数需结合盈利口径理解。",
    "pb": "市净率差异需要结合资产质量和盈利能力判断。",
    "net_profit_growth": "利润增速还需结合上年基数和一次性损益判断。",
    "revenue_growth": "收入增速反映当期业务扩张速度，可结合利润增速核对增长质量。",
    "tracking_error": "跟踪同一基准时，更小的跟踪误差表示净值走势更贴近基准。",
    "fee_rate": "费率差异会影响长期持有成本，可结合费率类别、基金合同和交易成本核对。",
    "conversion_premium_rate": "转股溢价率较高时，相对转股价值付出的价格更高。可结合正股波动、赎回条款与纯债价值核对。",
    "yield_to_maturity": "到期收益率对应按时兑付和持有至到期的假设，还需核对信用资质与提前赎回条款。",
    "change": "涨跌幅反映该期间的相对表现，判断走势还需要连续多期数据。",
    "nav_change": "净值变化宜结合相同观察窗口、基准以及分红口径比较。",
    "close_price": "不同标的的每股价格、转债报价或指数点位受各自计量基数影响，估值应结合各自基本面指标。",
    "fund_nav": "基金净值受成立时点和分红影响，比较表现时应核对同一期间的净值增长率。",
}


def _metric(fact):
    if fact.field in PERCENT_FIELDS:
        return fact.normalized_value, "%"
    value = finite_number(fact.value, allow_text=True)
    if fact.unit == "conflicting":
        return None, ""
    if fact.field in {"pe_ttm", "pb"}:
        return (value, "倍") if fact.unit in {None, "倍", "multiple"} else (None, "")
    # Absolute figures require an explicit unit, including currency and denomination.
    unit = fact.unit
    if not unit:
        source = fact.source_field or ""
        unit = next((label for label in ("万亿元", "亿元", "万元", "港元", "美元", "人民币", "元", "点")
                     if source.endswith((f"（{label}）", f"({label})", f"[{label}]", f":{label}"))), "")
    return value, {"CNY": "元", "人民币": "元", "HKD": "港元", "USD": "美元"}.get(unit, unit)


def _describe(field, period, unit, ranked):
    spread = ranked[0][1] - ranked[-1][1]
    values = "、".join(f"{fact.entity} {value:,.2f}{unit}" for fact, value in ranked)
    text = f"- **{LABELS[field]}（{period}）** {values}。"
    if isclose(spread, 0, abs_tol=5e-5):
        text += "本组数值相同或接近。"
    else:
        difference_unit = "个百分点" if unit == "%" else unit
        text += f"首尾相差 {spread:,.2f}{difference_unit}。"
    if field in NOTES:
        text += NOTES[field]
    return text


def review_comparison(intent, targets, facts, *, agent_id):
    allowed = (*FIELDS.get(intent, ()), "close_price", "fund_nav")
    grouped = defaultdict(lambda: defaultdict(dict))
    seen_fields = set()
    target_set = set(targets)
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    for fact in facts:
        if fact.field not in allowed or fact.derived_from:
            continue
        matches = [target for target in targets if matching_fact(target, fact)]
        if len(matches) != 1:
            continue
        seen_fields.add(fact.field)
        period = period_info(fact.period)
        value, unit = _metric(fact)
        if not period or period[1] > today or value is None or not unit:
            continue
        key = (period[1], period[0], unit)
        grouped[fact.field][key].setdefault(matches[0], []).append((fact, value))

    lines, references, missing, conflicts = [], [], [], []
    for field in allowed:
        for key, records in sorted(grouped[field].items(), reverse=True):
            if records.keys() != target_set:
                continue
            if any(not all(isclose(value, pairs[0][1], rel_tol=1e-8, abs_tol=5e-5)
                           for _, value in pairs) for pairs in records.values()):
                if field not in conflicts:
                    conflicts.append(field)
                continue
            selected = [max(records[target], key=lambda pair: (pair[0].snapshot_time, pair[0].quality))
                        for target in targets]
            ranked = sorted(selected, key=lambda pair: pair[1], reverse=True)
            if len(lines) < 5:
                lines.append(_describe(field, key[1], key[2], ranked))
                references.extend(fact for fact, _ in ranked)
            break
        else:
            if field in seen_fields or field in allowed[:3]:
                missing.append(field)

    if not lines:
        return None
    risks = []
    if missing:
        risks.append("暂缺所有标的共有、且期间与单位一致的指标：" + "、".join(LABELS[field] for field in missing) + "。")
    if conflicts:
        risks.append("同口径数值有冲突，相关期间已跳过：" + "、".join(LABELS[field] for field in conflicts) + "。")
    return AgentResult(
        agent_id=agent_id, status=TaskStatus.COMPLETED,
        opinion="\n\n".join(lines), confidence=min(fact.quality for fact in references),
        facts_used=[fact.fact_id for fact in references], citations=sorted({fact.source_id for fact in references}),
        risk_flags=risks, invalidation_conditions=["数据期间、指标口径或来源数值更新"],
        details={"comparison_targets": targets, "engine": "local_rules", "snapshot_only": True},
    )
