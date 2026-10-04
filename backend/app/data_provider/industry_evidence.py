"""Complete, same-period constituent revenue proxy; never average partial rows."""
import hashlib
import json
import math
import re
from datetime import date, datetime, timedelta, timezone

from backend.app.models import FactRecord


def industry_query_name(industry):
    # The official third-level classification uses a tier suffix; the .TI index
    # display name omits it. Do not map broader industries or other index systems.
    return industry.removesuffix('Ⅲ').removesuffix('III')


def latest_report_period(now):
    day = now.astimezone(timezone(timedelta(hours=8))).date()
    # Use the most recent reporting period whose normal disclosure deadline ended.
    period = (date(day.year, 9, 30) if day > date(day.year, 10, 31) else
              date(day.year, 6, 30) if day > date(day.year, 8, 31) else
              date(day.year, 3, 31) if day > date(day.year, 4, 30) else date(day.year - 1, 9, 30))
    return period.isoformat(), period.replace(year=period.year - 1).isoformat()


def report_label(period):
    return period[:4] + {'03-31': '年一季报', '06-30': '年中报', '09-30': '年三季报'}[period[5:]]


def aggregate_industry_revenue(payload, *, target, source_id):
    from backend.app.data_provider.iwencai import _root_column_metadata, _find_record_lists
    industry, index_code, current, previous = target.split('|')
    rows = list(_find_record_lists(payload))
    count = payload.get('row_count') if isinstance(payload, dict) else None
    if (isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100
            or count != len(rows) or not re.fullmatch(r'\d{6}\.TI', index_code)):
        return []
    columns = _root_column_metadata(payload)
    keys = [f'营业收入[{p.replace("-", "")}]' for p in (current, previous)]
    scales = {'元': 1, 'CNY': 1, '万元': 1e4, '亿元': 1e8}
    if any(columns.get(key, {}).get('unit') not in scales for key in keys):
        return []
    captured = datetime.now(timezone.utc)
    roots, seen, totals = [], set(), [0., 0.]
    def fact(entity, field, value, period, code, *, parents=(), rule=None, unit=None):
        identity = json.dumps([target, entity, code, field, period, value], ensure_ascii=False)
        return FactRecord(fact_id='IW-IND-' + hashlib.sha256(identity.encode()).hexdigest()[:24],
            entity=entity, entity_code=code, field=field, value=value, period=period,
            observation_date=date.fromisoformat(period), unit=unit, snapshot_time=captured,
            source_id='DERIVED_INDUSTRY_REVENUE_V1' if parents else source_id,
            source_field='同花顺行业成分股同报告期营业收入合计口径',
            source_url='https://www.iwencai.com/unifiedwap/chat', quality=.85 if parents else .9,
            derived_from=[f.fact_id for f in parents], derivation_rule=rule)
    for row in rows:
        code = str(row.get('股票代码') or row.get('证券代码') or '')
        membership = row.get('所属同花顺行业')
        if not isinstance(membership, list) or industry not in membership:
            return []
        if not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', code) or code in seen:
            return []
        seen.add(code)
        for index, (key, period) in enumerate(zip(keys, (current, previous))):
            value = row.get(key)
            if isinstance(value, bool):
                return []
            try:
                value = float(str(value).replace(',', '')) * scales[columns[key]['unit']]
            except (ValueError, TypeError):
                return []
            if not math.isfinite(value) or value < 0:
                return []
            totals[index] += value
            roots.append(fact(str(row.get('股票简称') or code), 'constituent_revenue', value, period, code, unit='CNY'))
    if totals[1] <= 0 or not all(math.isfinite(v) for v in totals):
        return []
    inventory = fact(industry, 'industry_constituent_inventory', sorted(seen),
        captured.astimezone(timezone(timedelta(hours=8))).date().isoformat(), index_code)
    inventory = inventory.model_copy(update={'source_field': '当前同花顺行业成分股完整名单；不代表历史时点成分股'})
    parents = [inventory, *roots]
    score = fact(industry, 'industry_revenue_growth', (totals[0] / totals[1] - 1) * 100,
        current, index_code, unit='percent', parents=parents,
        rule='INDUSTRY_CONSTITUENT_REVENUE_YOY_V1: (sum(current)/sum(previous)-1)*100; '
             'complete declared inventory; exact industry membership; same reporting periods; current constituent proxy')
    return [*parents, score]
