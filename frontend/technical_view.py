"""Shared technical indicator cards and charts for every trend panel."""
from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from frontend.charts import render_chart

LABELS = {'dif': 'DIF', 'dea': 'DEA', 'histogram': 'MACD柱', 'rsi': 'RSI',
          'k': 'K', 'd': 'D', 'j': 'J', 'upper': '上轨', 'middle': '中轨',
          'lower': '下轨', 'atr': 'ATR', 'obv': 'OBV'}
PRIMARY = {'macd': 'histogram', 'rsi': 'rsi', 'kdj': 'j', 'boll': 'middle', 'atr': 'atr', 'obv': 'obv'}
CARD_LABELS = {'macd': 'MACD · 柱值', 'rsi': 'RSI · 14', 'kdj': 'KDJ · J',
               'boll': 'BOLL · 中轨', 'atr': 'ATR · 14', 'obv': 'OBV · 区间累计'}
CATEGORIES = {'趋势': ('macd',), '动量': ('rsi', 'kdj'), '波动率': ('boll', 'atr'), '量能': ('obv',)}


def _indicator_lines(indicator: dict, *, exclude: tuple = ()) -> pd.DataFrame:
    """Separate segments at missing values so a line cannot bridge invalid bars."""
    rows, segments = [], {}
    for point in indicator.get('series') or []:
        for field, value in (point.get('values') or {}).items():
            if field in exclude:
                continue
            segments.setdefault(field, 0)
            if value is None:
                segments[field] += 1
                continue
            rows.append({'日期': str(point['date']), '指标': LABELS.get(field, field),
                         '数值': float(value), '分段': str(segments[field])})
    return pd.DataFrame(rows, columns=['日期', '指标', '数值', '分段'])


def _draw_indicator(indicator: dict, *, code: str, key: str, points: list) -> None:
    frame = _indicator_lines(indicator, exclude=('histogram',))
    if frame.empty or frame['日期'].nunique() < 2:
        st.caption('有效读数不足两期，暂不绘制指标曲线。')
        return
    layers = []
    if code == 'boll':
        band_rows = [{'日期': str(point['date']), '上轨': point['values'].get('upper'),
                      '下轨': point['values'].get('lower')}
                     for point in indicator.get('series') or []
                     if point.get('values', {}).get('upper') is not None]
        band = pd.DataFrame(band_rows)
        layers.append(alt.Chart(band).mark_area(opacity=0.10, color='#246b89').encode(
            x='日期:T', y=alt.Y('下轨:Q', scale=alt.Scale(zero=False)), y2='上轨:Q'))
        close_rows = [{'日期': str(point['date']), '指标': '收盘', '数值': float(point['value']), '分段': '0'}
                      for point in points]
        frame = pd.concat([frame, pd.DataFrame(close_rows)], ignore_index=True)
    lines = alt.Chart(frame).mark_line(strokeWidth=2, point=True).encode(
        x=alt.X('日期:T', title=None, axis=alt.Axis(format='%m/%d')),
        y=alt.Y('数值:Q', title=indicator.get('unit') or indicator['name'], scale=alt.Scale(zero=False)),
        color=alt.Color('指标:N', title=None), detail='分段:N',
        tooltip=[alt.Tooltip('日期:T', format='%Y-%m-%d'), '指标:N', alt.Tooltip('数值:Q', format=',.4f')],
    )
    if code == 'macd':
        histogram = [{'日期': str(point['date']), '柱值': point['values']['histogram']}
                     for point in indicator.get('series') or []
                     if point.get('values', {}).get('histogram') is not None]
        layers.append(alt.Chart(pd.DataFrame(histogram)).mark_bar(opacity=0.45).encode(
            x='日期:T', y='柱值:Q',
            color=alt.condition(alt.datum.柱值 >= 0, alt.value('#ad384e'), alt.value('#18794e')),
            tooltip=[alt.Tooltip('日期:T', format='%Y-%m-%d'), alt.Tooltip('柱值:Q', format=',.4f')]))
    layers.append(lines)
    thresholds = {'macd': [0], 'rsi': [30, 70], 'kdj': [20, 80]}.get(code)
    if thresholds:
        layers.append(alt.Chart(pd.DataFrame({'参考线': thresholds})).mark_rule(
            color='#9ca3af', strokeDash=[4, 4], opacity=0.6).encode(y='参考线:Q'))
    chart = alt.layer(*layers).resolve_scale(y='shared').properties(height=250)
    render_chart(chart.configure_view(stroke=None), key=f'technical_chart_{key}_{code}')


def render_technical_indicators(analysis: dict, *, points: list, key: str,
                                show_annotations: bool = True) -> None:
    indicators = analysis.get('indicators') or {}
    if not indicators:
        return
    with st.container(border=True):
        st.markdown('**技术指标**')
        if show_annotations:
            st.caption('六种指标 · 四类观察 · 默认参数。读数描述历史走势，不直接代表买卖建议。')
        with st.container(horizontal=True):
            for code, primary in PRIMARY.items():
                item = indicators.get(code)
                if not item:
                    continue
                value = (item.get('latest') or {}).get(primary) if item.get('available') else None
                suffix = f" {item['unit']}" if item.get('unit') else ''
                display = f'{value:,.4f}{suffix}' if value is not None else '—'
                card_label = CARD_LABELS[code]
                if value is not None and code == 'obv':
                    scale, label = (1e8, '亿') if abs(value) >= 1e8 else ((1e4, '万') if abs(value) >= 1e4 else (1, ''))
                    display = f"{value / scale:,.2f}"
                    card_label = f"OBV · {label}{item.get('unit') or ''}"
                st.metric(card_label, display,
                          help=item.get('message') or item.get('method'), border=True, width=160)
        category = st.segmented_control('指标类别', list(CATEGORIES), default='趋势',
                                        key=f'technical_category_{key}') or '趋势'
        for code in CATEGORIES[category]:
            item = indicators.get(code)
            if not item:
                continue
            st.markdown(f"**{item['name']}（{item.get('parameters', '')}）**")
            if item.get('message'):
                st.info(item['message'], icon=':material/info:')
            if item.get('available'):
                date = item.get('latest_date') or ''
                values = ' · '.join(f'{LABELS.get(field, field)} {value:,.4f}'
                                    for field, value in item.get('latest', {}).items())
                st.caption(f'{date} · {values}')
            _draw_indicator(item, code=code, key=key, points=points)
            if show_annotations:
                st.caption(item.get('method') or '')
            if show_annotations and code in {'rsi', 'kdj'}:
                st.caption('虚线为常用观察阈值；进入阈值区域不代表价格会立即反转。KDJ 的 J 值可能超出 0–100。'
                           if code == 'kdj' else '虚线为 RSI 30/70 观察阈值；进入阈值区域不代表价格会立即反转。')
            with st.expander(f"{item['name']} 数据与下载", key=f'technical_data_{key}_{code}', on_change='rerun') as panel:
                if panel.open:
                    rows = [{'日期': str(point['date']), **{LABELS.get(field, field): value
                             for field, value in point.get('values', {}).items()}}
                            for point in item.get('series') or []]
                    data = pd.DataFrame(rows)
                    if show_annotations:
                        st.caption('预热期与缺失数据保留为空；OBV 从本次区间首日零点累计。')
                    st.dataframe(data, hide_index=True, width='stretch')
                    st.download_button('下载指标 CSV', data.to_csv(index=False).encode('utf-8-sig'),
                                       file_name=f'{code}_history.csv', mime='text/csv',
                                       key=f'technical_csv_{key}_{code}', icon=':material/download:')
