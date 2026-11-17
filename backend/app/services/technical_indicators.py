"""Deterministic technical indicators on dated, same-basis history; no filling."""
from __future__ import annotations

import math
from datetime import date, datetime
from statistics import fmean, pstdev
from typing import Any, Iterable

from backend.app.models.trend import IndicatorPoint, TechnicalIndicator


def _number(value: Any, *, zero: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and (result >= 0 if zero else result > 0) else None


def _day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def history_bars(points: Iterable[Any]) -> list[dict[str, Any]]:
    """Join FactRecord fields by day; reject conflicts, never substitute turnover."""
    rows: dict[date, dict] = {}
    conflicts: set[tuple[date, str]] = set()
    fields = {'close_price': 'value', 'fund_nav': 'value', 'high_price': 'high',
              'low_price': 'low', 'volume': 'volume'}
    for point in points:
        if isinstance(point, (tuple, list)) and len(point) == 2:
            data = {'date': point[0], 'value': point[1]}
        elif isinstance(point, dict):
            data = point
        else:
            data = point.model_dump() if hasattr(point, 'model_dump') else vars(point)
        day = _day(data.get('date') or data.get('period'))
        if day is None:
            continue
        if 'field' in data:
            key = fields.get(data['field'])
            if key is None:
                continue
            values = {key: data.get('value')}
            unit = data.get('unit')
        else:
            values = {key: data.get(key) for key in ('value', 'high', 'low', 'volume')}
            unit = data.get('volume_unit')
        row = rows.setdefault(day, {'date': day})
        for key, value in values.items():
            value = _number(value, zero=key == 'volume')
            if value is None:
                continue
            normalized_unit = unit
            if key == 'volume':
                # Keep shares, lots, fund units and bond units separate.
                units = {'股': ('股', 1), '万股': ('股', 1e4), '亿股': ('股', 1e8),
                         '手': ('手', 1), '万手': ('手', 1e4), '亿手': ('手', 1e8),
                         '份': ('份', 1), '万份': ('份', 1e4), '亿份': ('份', 1e8),
                         '张': ('张', 1), '万张': ('张', 1e4)}
                normalized_unit, multiplier = units.get(str(unit), (None, 1))
                value *= multiplier
                if not math.isfinite(value):
                    continue
                if key in row and row.get('volume_unit') != normalized_unit:
                    conflicts.add((day, key))
            if key in row and not math.isclose(row[key], value, rel_tol=1e-9, abs_tol=1e-9):
                conflicts.add((day, key))
            row[key] = value
            if key == 'volume':
                row['volume_unit'] = normalized_unit
    output = []
    for day, row in sorted(rows.items()):
        for key in ('value', 'high', 'low', 'volume'):
            if (day, key) in conflicts:
                row.pop(key, None)
        if 'value' not in row:
            continue
        high, low, close = row.get('high'), row.get('low'), row['value']
        if (high is not None and high < close) or (low is not None and low > close) or (
                high is not None and low is not None and high < low):
            row.pop('high', None)
            row.pop('low', None)
        output.append(row)
    return output


SPECS = {
    'macd': ('MACD', '趋势', '12,26,9', 'EMA 以区间首个收盘价初始化；DIF=EMA12−EMA26，DEA=EMA9(DIF)，柱值=2×(DIF−DEA)；前33期不展示。', ('dif', 'dea', 'histogram'), 34),
    'rsi': ('RSI', '动量', '14', 'Wilder 平滑：首14次涨跌分别取均值，随后按1/14递推；无涨无跌记50，只有上涨记100，只有下跌记0。', ('rsi',), 15),
    'kdj': ('KDJ', '动量', '9,3,3', 'RSV=(收盘−9期最低)/(9期最高−9期最低)×100；K、D初值50并按1/3递推，J=3K−2D；零振幅RSV记50，J不裁剪。', ('k', 'd', 'j'), 9),
    'boll': ('BOLL', '波动率', '20,2', '中轨为20期简单均线，上下轨为中轨±2倍总体标准差（ddof=0）。', ('upper', 'middle', 'lower'), 20),
    'atr': ('ATR', '波动率', '14', 'TR=max(最高−最低,|最高−前收|,|最低−前收|)；首14个TR取均值，随后Wilder平滑；至少15期完整高低收。', ('atr',), 15),
    'obv': ('OBV', '量能', '区间累计', '区间首日为0；此后上涨加当日成交量、下跌减成交量、平盘不变。整个区间成交量须齐全且单位一致；不是全历史累计值。', ('obv',), 2),
}


def calculate_indicators(points: Iterable[Any]) -> dict[str, TechnicalIndicator]:
    bars = history_bars(points)
    dates = [bar['date'] for bar in bars]
    closes = [bar['value'] for bar in bars]
    computed = {key: [{field: None for field in spec[4]} for _ in bars]
                for key, spec in SPECS.items()}
    ema12 = ema26 = dea = None
    gains, losses = [], []
    avg_gain = avg_loss = None
    hlc: list[dict] = []
    k = d = 50.0
    trs: list[float] = []
    atr = None
    previous_hlc = None
    for i, bar in enumerate(bars):
        close = bar['value']
        ema12 = close if ema12 is None else ema12 + 2 / 13 * (close - ema12)
        ema26 = close if ema26 is None else ema26 + 2 / 27 * (close - ema26)
        dif = ema12 - ema26
        dea = dif if dea is None else dea + 2 / 10 * (dif - dea)
        if i >= 33:
            computed['macd'][i] = {'dif': dif, 'dea': dea, 'histogram': 2 * (dif - dea)}
        if i:
            delta = close - closes[i - 1]
            gains.append(max(delta, 0))
            losses.append(max(-delta, 0))
            if i == 14:
                avg_gain, avg_loss = fmean(gains), fmean(losses)
            elif i > 14:
                avg_gain = (avg_gain * 13 + gains[-1]) / 14
                avg_loss = (avg_loss * 13 + losses[-1]) / 14
            if avg_gain is not None:
                rsi = 50.0 if avg_gain == avg_loss == 0 else (
                    100.0 if avg_loss == 0 else 100 * avg_gain / (avg_gain + avg_loss))
                computed['rsi'][i] = {'rsi': rsi}
        if i >= 19:
            window = closes[i - 19:i + 1]
            mid, sigma = fmean(window), pstdev(window)
            computed['boll'][i] = {'upper': mid + 2 * sigma, 'middle': mid, 'lower': mid - 2 * sigma}
        if bar.get('high') is None or bar.get('low') is None:
            hlc, trs = [], []
            previous_hlc, atr = None, None
            k = d = 50.0
            continue
        hlc.append(bar)
        if len(hlc) >= 9:
            high = max(item['high'] for item in hlc[-9:])
            low = min(item['low'] for item in hlc[-9:])
            rsv = 50.0 if high == low else (close - low) / (high - low) * 100
            k = (2 * k + rsv) / 3
            d = (2 * d + k) / 3
            computed['kdj'][i] = {'k': k, 'd': d, 'j': 3 * k - 2 * d}
        if previous_hlc is not None:
            tr = max(bar['high'] - bar['low'], abs(bar['high'] - previous_hlc), abs(bar['low'] - previous_hlc))
            trs.append(tr)
            if len(trs) == 14:
                atr = fmean(trs)
            elif len(trs) > 14:
                atr = (atr * 13 + tr) / 14
            if atr is not None:
                computed['atr'][i] = {'atr': atr}
        previous_hlc = close
    volume_units = {bar.get('volume_unit') for bar in bars}
    volume_ok = len(bars) >= 2 and len(volume_units) == 1 and None not in volume_units and all(
        bar.get('volume') is not None for bar in bars)
    if volume_ok:
        obv = 0.0
        for i, bar in enumerate(bars):
            if i:
                obv += bar['volume'] * (1 if closes[i] > closes[i - 1] else -1 if closes[i] < closes[i - 1] else 0)
            computed['obv'][i] = {'obv': obv}
    output = {}
    for key, (name, category, parameters, method, fields, needed) in SPECS.items():
        values = computed[key]
        available = bool(values) and all(values[-1][field] is not None for field in fields)
        message = None
        if not available:
            message = f'至少需要{needed}期有效数据；当前{len(bars)}期。'
            if key in {'kdj', 'atr'}:
                message = f'需要最近连续{needed}期同口径最高价、最低价和收盘价；缺失或矛盾数据不补齐。'
            elif key == 'obv':
                message = '至少需要2期收盘价；整个区间成交量须完整、单位明确且统一，成交额不能替代成交量。'
        elif key in {'kdj', 'atr'} and len(hlc) < len(bars):
            message = f'曾遇到缺失或无效高低价，从最近连续{len(hlc)}期重新预热。'
        output[key] = TechnicalIndicator(
            name=name, category=category, parameters=parameters, method=method,
            available=available, latest_date=dates[-1] if available else None,
            latest={field: round(values[-1][field], 6) for field in fields} if available else {},
            series=[IndicatorPoint(date=day, values={field: round(row[field], 6)
                    if row[field] is not None else None for field in fields}) for day, row in zip(dates, values)],
            unit=next(iter(volume_units)) if key == 'obv' and volume_ok else None, message=message,
        )
    return output
