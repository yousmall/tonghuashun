"""Explicit percentage units. Raw values remain available for audit."""
from __future__ import annotations

from backend.app.numeric import finite_number
from typing import Any
from backend.app.fund_fields import FUND_PERCENT_FIELDS

PERCENT_FIELDS = frozenset({
    "capital_flow_ratio", "interval_change",
    "max_drawdown_1y",
    "change", "roe", "roe_weighted", "revenue_growth", "net_profit_growth",
    "fee_rate", "tracking_error", "interest_rate", "cpi", "ppi",
    "turnover_rate", "volatility", "amplitude", "nav_change",
    "conversion_premium_rate", "pure_bond_premium_rate", "yield_to_maturity",
    "gross_margin", "net_margin", "operating_margin",
    "m2_growth", "market_advancing_ratio", "industry_median_change", "industry_revenue_growth", "industry_turnover_percentile", "industry_turnover_history",
}) | FUND_PERCENT_FIELDS


def percentage_points(value: Any, unit: str | None) -> float | None:
    """Return percentage points; bare numbers require a declared unit."""
    unit = {"%": "percent", "％": "percent", "percentage_points": "percent"}.get(unit, unit)
    if isinstance(value, str):
        text = value.strip().replace("％", "%")
        if text.endswith("%"):
            if unit not in {None, "percent"}:
                return None
            unit, text = "percent", text[:-1].strip()
        value = text
    number = finite_number(value, allow_text=True)
    if number is None:
        return None
    if unit == "ratio":
        return finite_number(number * 100)
    return number if unit == "percent" else None
