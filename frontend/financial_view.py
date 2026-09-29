"""展示用的期间、单位与数值口径；不计算投资建议。"""
from calendar import monthrange
from datetime import date
import math
import re


PERCENT_FIELDS = {"change", "nav_change", "turnover_rate", "roe", "roe_weighted", "revenue_growth", "net_profit_growth", "fee_rate", "tracking_error", "conversion_premium_rate", "pure_bond_premium_rate", "yield_to_maturity"}
MONEY_FIELDS = {"market_cap", "float_market_cap", "turnover_value", "fund_size", "capital_flow", "remaining_size"}


def period_info(raw):
    """返回规范标签、截至日、期间类型；不把季度与某个交易日混为一谈。"""
    value = str(raw or "").strip().upper()
    if re.fullmatch(r"\d{8}", value):
        value = f"{value[:4]}-{value[4:6]}-{value[6:]}"
    quarter = re.fullmatch(r"(\d{4})[- ]?Q([1-4])", value)
    try:
        if quarter:
            year, q = map(int, quarter.groups())
            month = q * 3
            return f"{year}Q{q}", date(year, month, monthrange(year, month)[1]), "quarter"
        if re.fullmatch(r"\d{4}(?:年报)?", value):
            year = int(value[:4])
            return f"{year}年报", date(year, 12, 31), "year"
        if re.fullmatch(r"\d{4}-\d{2}", value):
            year, month = map(int, value.split("-"))
            return value, date(year, month, monthrange(year, month)[1]), "month"
        stamp = date.fromisoformat(value)
        return stamp.isoformat(), stamp, "date"
    except ValueError:
        return None


def numeric_value(raw):
    if isinstance(raw, bool):
        return None
    try:
        result = float(str(raw).replace(",", "").replace("%", "").strip())
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def fact_unit(fact):
    field = fact.get("field")
    raw = str(fact.get("source_field") or "")
    currency = next((label for word, label in [("美元", "美元"), ("港元", "港元"), ("港币", "港元"), ("人民币", "元")] if word in raw), None)
    if not currency and str(fact.get("entity_code") or "").upper().endswith(".HK"):
        currency = "港元"
    if field in PERCENT_FIELDS:
        return "%"
    if field in {"pe_ttm", "pb"}:
        return "倍"
    if field in MONEY_FIELDS:
        magnitude = next((prefix for prefix in ["万亿", "亿", "万"] if prefix+"元" in raw), "")
        return magnitude + (currency or "元")
    if field in {"close_price", "conversion_price"}:
        if "指数" in raw or any(word in str(fact.get("entity") or "") for word in ["指数", "上证", "深证"]):
            return "点"
        return currency or "元"
    if field == "fund_nav":
        return currency or "元"
    return ""
