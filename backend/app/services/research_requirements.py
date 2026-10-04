"""Separate transport results from fulfillment of server-defined evidence needs."""
from __future__ import annotations

import math
import re
from collections import defaultdict

from backend.app.fact_taxonomy import NEWS_FIELDS, fact_is_current
from backend.app.services.return_expectation import _currency, _date, _price

DEFAULT_FIELDS = {"get_institutional_research": ("target_price",), "get_quote": ("close_price",)}
DOCUMENT_FIELDS = {*NEWS_FIELDS, "event"}


def matches_entity(fact, entity):
    if not entity:
        return True
    code = re.fullmatch(r"(\d{6})(?:\.(?:SH|SZ|BJ|TI))?", entity.upper())
    if code and fact.entity_code:
        return (fact.entity_code.upper() == entity.upper() if "." in entity else
                str(fact.entity_code).split(".")[0] == code[1])
    return fact.entity == entity


def finite_value(fact):
    if isinstance(fact.value, bool):
        return False
    try:
        return math.isfinite(float(str(fact.value).replace(",", "")))
    except (ValueError, TypeError):
        return False


def evidence_status(call, facts, now):
    """Only facts satisfying scope, metadata and compatible raw inputs count as fulfilled."""
    required = call.required_fields or DEFAULT_FIELDS.get(call.method, ())
    output = {"status": "returned" if facts else "empty", "required_fields": list(required),
              "missing_fields": [], "invalid_fields": [], "reason_codes": []}
    if not required:
        return output
    entity = call.expected_entity
    if entity is None and call.method in DEFAULT_FIELDS and call.args:
        # Default routes sometimes use a whole user question rather than a symbol;
        # only a server-validated compact code/name can constrain that response.
        entity = str(call.args[0])
    current = [f for f in facts if fact_is_current(f, now)]
    scoped = [f for f in current if matches_entity(f, entity)]
    valid_by_field = {}
    for field in required:
        candidates = [f for f in scoped if f.field == field or field == "document" and f.field in DOCUMENT_FIELDS]
        if not candidates:
            output["missing_fields"].append(field)
            continue
        valid = []
        for fact in candidates:
            okay = True
            if field in {"close_price", "target_price"}:
                okay = _price(fact) is not None and bool(_currency(fact) and _date(fact))
                if field == "target_price":
                    okay = okay and bool(fact.source_url)
            elif field == "document":
                okay = isinstance(fact.value, str) and bool(fact.value.strip() and fact.source_url and _date(fact))
            elif field == "industry":
                okay = isinstance(fact.value, str) and bool(fact.value.strip())
            elif field == "market_session":
                okay = finite_value(fact) and bool(_date(fact))
            elif field == "market_session_count":
                okay = finite_value(fact) and bool(fact.period) and float(str(fact.value).replace(",", "")).is_integer()
            else:
                okay = finite_value(fact) and bool(_date(fact))
                if field in {"m2_growth", "industry_revenue_growth", "industry_turnover_history"}:
                    okay = okay and fact.normalized_value is not None
                if field in {"capital_flow", "turnover_value"}:
                    okay = okay and fact.unit in {"CNY", "万元", "亿元"}
                if field in {"advancing_count", "market_total_count"}:
                    okay = okay and float(str(fact.value).replace(",", "")).is_integer()
                if field in {"industry_revenue_growth", "industry_turnover_history", "capital_flow", "turnover_value"}:
                    okay = okay and bool(fact.entity_code and fact.entity_code.upper().endswith(".TI"))
            if okay:
                valid.append(fact)
        if not valid:
            output["invalid_fields"].append(field)
        valid_by_field[field] = valid
    # A count pair or cash-flow pair must refer to one actual entity/date/unit.
    for left, right in (("advancing_count", "market_total_count"), ("capital_flow", "turnover_value")):
        if left not in required or right not in required:
            continue
        if not valid_by_field.get(left) or not valid_by_field.get(right):
            continue  # A missing partner does not invalidate the source already obtained.
        pairs = [(a, b) for a in valid_by_field.get(left, []) for b in valid_by_field.get(right, [])
                 if a.entity == b.entity and a.entity_code == b.entity_code and _date(a) == _date(b)
                 and (left == "advancing_count" or a.unit == b.unit)]
        if not any(float(str(b.value).replace(",", "")) > 0 and
                   (0 <= float(str(a.value).replace(",", "")) <= float(str(b.value).replace(",", ""))
                    if left == "advancing_count" else
                    abs(float(str(a.value).replace(",", ""))) <= float(str(b.value).replace(",", "")))
                   for a, b in pairs):
            output["invalid_fields"].extend(f for f in (left, right) if f not in output["missing_fields"])
    if "close_price" in required and valid_by_field.get("close_price"):
        latest = max(_date(f) for f in valid_by_field["close_price"])
        latest_values = {_price(f) for f in valid_by_field["close_price"] if _date(f) == latest}
        if len(latest_values) != 1:
            output["invalid_fields"].append("close_price")
    if 'industry_turnover_history' in required and len(call.args) == 3:
        start, end = call.args[1:]
        calendar = {f.period for f in current if f.field == 'market_session' and f.value == 1
                    and f.entity == '中国A股交易日历' and start <= (f.period or '') <= end}
        totals = {float(f.value) for f in current if f.field == 'market_session_count'
                  and f.period == f'{start}/{end}' and finite_value(f)}
        history = valid_by_field.get('industry_turnover_history', [])
        days = {f.period for f in history}
        codes = {f.entity_code for f in history}
        conflicts = any(len({(str(f.value), f.unit) for f in history if f.period == day}) != 1 for day in days)
        if totals != {float(len(calendar))} or len(calendar) < 200 or not calendar <= days or len(codes) != 1 or conflicts:
            output['invalid_fields'].append('industry_turnover_history')
            output['reason_codes'].append('HISTORY_COVERAGE_INCOMPLETE')
    output["invalid_fields"] = sorted(set(output["invalid_fields"]))
    if output["missing_fields"]:
        output["reason_codes"].append("REQUIRED_FIELD_MISSING")
    if output["invalid_fields"]:
        output["reason_codes"].append("METADATA_OR_SCOPE_INVALID")
    if current and not scoped:
        output["reason_codes"].append("ENTITY_MISMATCH")
    if facts and not current:
        output["reason_codes"].append("STALE_OR_LOW_QUALITY")
    output["status"] = "partial" if output["missing_fields"] or output["invalid_fields"] else "complete"
    return output


def select_calls(pipeline, calls, budget):
    """Stable priority selection with indivisible dependencies, within one shared budget."""
    unique = {}
    for call in calls:
        key = pipeline._cache_key(call)
        if key not in unique or call.priority > unique[key].priority:
            unique[key] = call
    groups = defaultdict(list)
    for key, call in unique.items():
        groups[call.bundle or key].append(call)
    chosen, deferred = [], []
    for group in sorted(groups.values(), key=lambda items: max(c.priority for c in items), reverse=True):
        if any(c.method == "get_industry_turnover_history" for c in group) and not any(
                c.method == "get_market_calendar" for c in group):
            deferred.extend(group)
        elif len(chosen) + len(group) <= max(0, budget):
            chosen.extend(group)
        else:
            deferred.extend(group)
    return chosen, deferred
