"""Strict parsers for date-series recovery; query text is not unit evidence."""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from backend.app.models import FactRecord


def parse_recovery_series(payload, *, target, source_id, kind):
    from backend.app.data_provider.iwencai import (
        _find_record_lists, _history_date, _root_column_metadata, _source_url, _entity_code_from_record)
    captured = datetime.now(timezone.utc)
    today = captured.astimezone(timezone(timedelta(hours=8))).date()
    columns = _root_column_metadata(payload)
    result, seen, conflicts = {}, {}, set()
    for record in list(_find_record_lists(payload))[:1000]:
        code = _entity_code_from_record(record)
        if kind == 'stock_series' and (not code or code.split('.')[0] != target):
            continue
        if kind == 'industry_series':
            if not code or not code.upper().endswith('.TI'):
                continue
            name = record.get('指数简称') or record.get('行业名称') or record.get('指数名称')
            from backend.app.data_provider.industry_evidence import industry_query_name
            if name != industry_query_name(target):
                continue
        row_day = next((_history_date(record.get(key)) for key in ('交易日期', '日期', 'date')
                        if _history_date(record.get(key))), None)
        for raw, value in record.items():
            raw = str(raw)
            if kind == 'calendar_series' and raw == '区间交易日总数':
                start = _history_date(record.get('区间开始日期'))
                end = _history_date(record.get('区间结束日期'))
                try:
                    count = float(value)
                except (ValueError, TypeError):
                    continue
                if (isinstance(value, bool) or not start or not end or end >= today or
                        not math.isfinite(count) or not count.is_integer() or not 200 <= count <= 270):
                    continue
                identity = ('market_session_count', start, end)
                signature = (count, None)
                if identity in seen and seen[identity] != signature:
                    conflicts.add(identity)
                seen[identity] = signature
                result[identity] = FactRecord(fact_id='IW-SERIES-' + uuid4().hex[:20], entity='中国A股交易日历',
                    field='market_session_count', value=count, period=f'{start}/{end}', snapshot_time=captured,
                    source_id=source_id, source_field=raw, quality=.9)
                continue
            match = re.search(r'\[(\d{8})\]$', raw)
            day = (_history_date(value) if kind == 'calendar_series' and raw in {'交易日期', '交易日'} else
                   _history_date(match[1]) if match else row_day)
            if not day or day >= today:
                continue
            field, unit = None, None
            column_unit = str(columns.get(raw, {}).get('unit') or '')
            row_unit = str(record.get('单位') or record.get('unit') or '')
            if column_unit and row_unit and column_unit != row_unit:
                continue
            declaration = column_unit or row_unit
            if kind == 'calendar_series':
                # Explicit trading dates only; do not manufacture a calendar
                # from arbitrary weekdays or another security's sparse chart.
                if raw not in {'交易日期', '交易日'} or record.get('是否交易日') in {False, '否', 0}:
                    continue
                day = _history_date(value)
                if not day or day >= today or day.weekday() >= 5:
                    continue
                field, value, code = 'market_session', 1, None
            elif kind == 'stock_series' and raw.startswith('前复权收盘价'):
                if declaration not in {'元', 'CNY'}:
                    continue
                field, unit = 'adjusted_close_history', 'CNY:qfq'
            elif kind == 'stock_series' and raw.startswith('成交额'):
                field = 'daily_turnover_history'
            elif kind == 'industry_series' and raw.startswith('换手率'):
                if declaration not in {'%', '％', 'percent', '百分比'}:
                    continue
                field, unit = 'industry_turnover_history', 'percent'
            if field is None or isinstance(value, bool):
                continue
            try:
                value = float(str(value).replace(',', ''))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value) or value < 0 or (field == 'adjusted_close_history' and value == 0):
                continue
            if field == 'daily_turnover_history':
                scale = {'元': 1, 'CNY': 1, '万元': 10000, '亿元': 100000000}.get(declaration)
                if not scale:
                    continue
                value, unit = value * scale, 'CNY'
            identity = (field, day, code)
            signature = (value, unit)
            if identity in seen and seen[identity] != signature:
                conflicts.add(identity)
            seen[identity] = signature
            result[identity] = FactRecord(fact_id='IW-SERIES-' + uuid4().hex[:20],
                entity='中国A股交易日历' if kind == 'calendar_series' else target, entity_code=code,
                field=field, value=value, unit=unit, source_field=raw, period=day.isoformat(),
                snapshot_time=captured, source_id=source_id, quality=.9, source_url=_source_url(record))
    return [fact for key, fact in result.items() if key not in conflicts]
