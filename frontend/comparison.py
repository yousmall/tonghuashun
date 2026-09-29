"""确定性的指标矩阵：身份由后端核对，缺失、期间差异和冲突都明确显示。"""
from datetime import date
import math
from frontend.financial_view import period_info, fact_unit, numeric_value
from frontend.research_board import LABELS

FIELDS = ["close_price", "fund_nav", "change", "nav_change", "pe_ttm", "pb", "roe", "revenue_growth", "net_profit_growth", "fund_size", "fee_rate", "tracking_error", "conversion_premium_rate", "yield_to_maturity", "remaining_size", "industry", "fund_manager", "bond_rating"]


def comparison_matrix(items):
    rows = []
    names = [item.get("target", "—") for item in items]
    for field in FIELDS:
        buckets = []
        for item in items:
            values, conflicts = {}, set()
            for fact in item.get("facts", []):
                if fact.get("field") != field:
                    continue
                if field not in {"industry", "fund_manager", "bond_rating"} and numeric_value(fact.get("value")) is None:
                    continue
                info = period_info(fact.get("period"))
                if info and info[1] > date.today():
                    continue
                key = (info[0] if info else "日期未提供", fact_unit(fact))
                previous = values.get(key)
                if previous:
                    first, second = numeric_value(previous.get("value")), numeric_value(fact.get("value"))
                    same = math.isclose(first, second, rel_tol=1e-8, abs_tol=5e-5) if first is not None and second is not None else previous.get("value") == fact.get("value")
                    if not same:
                        conflicts.add(key)
                values[key] = fact
            buckets.append((values, conflicts))
        if not any(values for values, _ in buckets):
            continue
        common = set.intersection(*(set(values)-conflicts for values, conflicts in buckets)) if buckets else set()
        common = {key for key in common if key[0] != "日期未提供"}
        chosen = max(common, key=lambda key: period_info(key[0])[1]) if common else None
        row = {"指标": LABELS.get(field, field), "单位": chosen[1] if chosen else "见各标的", "期间": chosen[0] if chosen else "见各标的", "可比性": "同期间、同单位" if chosen else "暂不可直接比较"}
        if field in {"industry", "fund_manager", "bond_rating"}:
            row["可比性"] = "描述信息"
        for name, (values, conflicts) in zip(names, buckets):
            if not values:
                row[name] = "未取得"
                continue
            key = chosen or max(values, key=lambda key: period_info(key[0])[1] if period_info(key[0]) else date.min)
            if key in conflicts:
                row[name] = "同期间数据冲突"
                row["可比性"] = "存在冲突，需核实"
                continue
            value = values[key].get("value")
            number = numeric_value(value)
            formatted = f"{number:,.4f}" if field == "fund_nav" and number is not None else f"{number:,.2f}" if number is not None else str(value)
            row[name] = formatted if chosen else f"{formatted} {key[1]}（{key[0]}）"
        rows.append(row)
    return rows
