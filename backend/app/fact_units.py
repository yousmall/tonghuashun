"""Explicit percentage units. Raw values remain available for audit."""
from __future__ import annotations

import math
import re
from typing import Any

PERCENT_FIELDS = frozenset({
    "capital_flow_ratio", "interval_change",
    "max_drawdown_1y",
    "change", "roe", "roe_weighted", "revenue_growth", "net_profit_growth",
    "fee_rate", "tracking_error", "interest_rate", "cpi", "ppi",
    "turnover_rate", "volatility", "amplitude", "nav_change",
    "conversion_premium_rate", "pure_bond_premium_rate", "yield_to_maturity",
    "gross_margin", "net_margin", "operating_margin",
    "m2_growth", "market_advancing_ratio", "industry_revenue_growth", "industry_turnover_percentile", "industry_turnover_history",
})


def percentage_points(value: Any, unit: str | None) -> float | None:
    """Return percentage points; bare numbers require a declared unit."""
    unit = {"%": "percent", "％": "percent", "percentage_points": "percent"}.get(unit, unit)
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("％", "%")
        if text.endswith("%"):
            if unit not in {None, "percent"}:
                return None
            unit, text = "percent", text[:-1].strip()
        if not re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", text):
            return None
        value = float(text)
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if unit == "ratio":
        return float(value) * 100
    if unit == "percent":
        return float(value)
    return None
