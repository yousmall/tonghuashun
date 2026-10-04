"""Deterministic recovery from dated source observations, never model estimates."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timedelta, timezone

from backend.app.fact_taxonomy import fact_is_current
from backend.app.models import FactRecord


def history_window(now: datetime) -> tuple[str, str]:
    # Use completed days even when a request arrives during an open session.
    end = now.astimezone(timezone(timedelta(hours=8))).date() - timedelta(days=1)
    try:
        start = end.replace(year=end.year - 1)
    except ValueError:  # leap day
        start = end.replace(year=end.year - 1, day=28)
    return start.isoformat(), end.isoformat()


def _day(value):
    try:
        text = str(value)
        return datetime.strptime(text, '%Y%m%d').date() if len(text) == 8 and text.isdigit() else date.fromisoformat(text)
    except ValueError:
        return None


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _series(facts, field, *, entity, code, now, start, end):
    points, conflicts = {}, set()
    if not code and len({f.entity_code for f in facts if f.entity == entity and f.field == field
                        and fact_is_current(f, now) and f.entity_code}) > 1:
        return {}
    for fact in facts:
        if fact.field != field or fact.entity != entity or not fact_is_current(fact, now):
            continue
        if code and str(fact.entity_code or '').split('.')[0] != code:
            continue
        day, value = _day(fact.period), _number(fact.value)
        if day is None or not start <= day <= end or value is None:
            continue
        previous = points.get(day)
        if previous and (previous.value != fact.value or previous.unit != fact.unit):
            conflicts.add(day)
        elif previous is None or fact.snapshot_time > previous.snapshot_time:
            points[day] = fact
    return {day: fact for day, fact in points.items() if day not in conflicts}


def _calendar(facts, now, start, end):
    points = _series(facts, 'market_session', entity='中国A股交易日历', code=None,
                     now=now, start=start, end=end)
    points = {day: fact for day, fact in points.items() if fact.value == 1 and day.weekday() < 5}
    days = sorted(points)
    declarations = [f for f in facts if f.entity == '中国A股交易日历' and f.field == 'market_session_count'
                    and f.period == f'{start}/{end}' and fact_is_current(f, now)]
    declared_counts = {_number(f.value) for f in declarations}
    # Reject short charts, truncated pages and sparse calendars. The source
    # calendar remains a prerequisite; weekdays alone are not trading days.
    if (declared_counts != {float(len(days))} or len(days) < 200
            or (days[0] - start).days > 16 or (end - days[-1]).days > 16
            or any((b - a).days > 16 for a, b in zip(days, days[1:]))):
        return {}, []
    return points, declarations


def _derived(entity, code, field, value, unit, parents, rule, start, end):
    parent_ids = sorted({fact.fact_id for fact in parents})
    identity = json.dumps([entity, code, field, value, parent_ids, rule], sort_keys=True)
    return FactRecord(fact_id='RECOVERY-' + hashlib.sha256(identity.encode()).hexdigest()[:24],
        entity=entity, entity_code=code, field=field, value=value, unit=unit,
        period=f'{start}/{end}', snapshot_time=min(f.snapshot_time for f in parents),
        quality=min(f.quality for f in parents) * .95, source_id='DERIVED_RECOMMENDATION_HISTORY_V1',
        derived_from=parent_ids, derivation_rule=rule)


def derive_history_metrics(facts, *, code, name, industry, now, start, end):
    start, end = date.fromisoformat(start), date.fromisoformat(end)
    calendar, calendar_proofs = _calendar(facts, now, start, end)
    if not calendar:
        return []
    output, dates = [], sorted(calendar)
    prices = _series(facts, 'adjusted_close_history', entity=name, code=code,
                     now=now, start=start, end=end)
    if (set(calendar) <= set(prices) and
            all(prices[day].unit == 'CNY:qfq' and _number(prices[day].value) > 0 for day in dates)):
        peak, drawdown = 0., 0.
        for day in dates:
            value = float(prices[day].value)
            peak = max(peak, value)
            drawdown = max(drawdown, 1 - value / peak)
        output.append(_derived(name, code, 'max_drawdown_1y', round(drawdown * 100, 6), 'percent',
            [*(prices[day] for day in dates), *calendar.values(), *calendar_proofs],
            'MAX_DRAWDOWN_QFQ_V1: max(1-price/running_peak)*100; complete_source_calendar', start, end))
    amounts = _series(facts, 'daily_turnover_history', entity=name, code=code,
                      now=now, start=start, end=end)
    recent = dates[-20:]
    if set(recent) <= set(amounts) and all(amounts[day].unit == 'CNY' and _number(amounts[day].value) >= 0 for day in recent):
        output.append(_derived(name, code, 'avg_turnover_20d',
            round(sum(float(amounts[day].value) for day in recent) / 20, 2), 'CNY',
            [*(amounts[day] for day in recent), *(calendar[day] for day in recent), *calendar_proofs],
            'TURNOVER_MEAN_20_V1: sum(CNY turnover on last 20 source sessions)/20; zero retained', start, end))
    turnover = _series(facts, 'industry_turnover_history', entity=industry, code=None,
                       now=now, start=start, end=end)
    if (set(calendar) <= set(turnover) and
            all(turnover[day].unit == 'percent' and _number(turnover[day].value) >= 0 for day in dates)):
        values = [float(turnover[day].value) for day in dates]
        # Empirical CDF, explicitly a time-series percentile, not today's
        # cross-sectional rank among unrelated industries.
        percentile = sum(value <= values[-1] for value in values) / len(values) * 100
        output.append(_derived(industry, None, 'industry_turnover_percentile', round(percentile, 6), 'percent',
            [*(turnover[day] for day in dates), *calendar.values(), *calendar_proofs],
            'INDUSTRY_TURNOVER_ECDF_V1: count(history<=latest)/N*100; complete_source_calendar', start, end))
    return output


def derive_explicit_state(facts, *, now):
    """Only explicit, compatible dated counts/states can fill these gaps."""
    output = []
    entities = {f.entity for f in facts}
    for entity in entities:
        current = [f for f in facts if f.entity == entity and fact_is_current(f, now)]
        def latest(field):
            local_day = now.astimezone(timezone(timedelta(hours=8))).date()
            options = [f for f in current if f.field == field and _day(f.period) is not None
                       and local_day - timedelta(days=16) <= _day(f.period) <= local_day]
            return max(options, key=lambda f: f.snapshot_time, default=None)
        advancing, total = latest('advancing_count'), latest('market_total_count')
        if advancing and total and advancing.period == total.period:
            a, n = _number(advancing.value), _number(total.value)
            if a is not None and n is not None and a.is_integer() and n.is_integer() and 0 <= a <= n and n > 0:
                output.append(_derived(entity, advancing.entity_code, 'market_advancing_ratio', a / n * 100,
                    'percent', [advancing, total], 'MARKET_BREADTH_COUNTS_V1: advancing/total*100; same_date_scope',
                    advancing.period, total.period))
        halted, listed = latest('is_suspended'), latest('listing_status')
        if halted and listed and halted.period == listed.period and halted.entity_code == listed.entity_code:
            state = None
            if str(halted.value).casefold() in {'true', '1', '是'}:
                state = '停牌'
            elif str(halted.value).casefold() in {'false', '0', '否'} and str(listed.value) in {'上市', '正常上市', '已上市'}:
                state = '正常交易'
            if state:
                output.append(_derived(entity, halted.entity_code, 'trading_status', state, None,
                    [halted, listed], 'EXPLICIT_TRADING_STATE_V1: dated suspension flag and listing status',
                    halted.period, listed.period))
    return output
