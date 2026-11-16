"""走势图、五日均线与“中和判断”的展示组件。

只渲染后端返回的带日期序列与确定性分析结果；界面不自己补数据、不把缺失
日期连成曲线，也不把走势描述写成收益预测。
"""
from __future__ import annotations

from typing import Any

import altair as alt
import pandas as pd
import streamlit as st
from frontend.charts import render_chart

VERDICT_COLORS = {
    "上行": "#ad384e",
    "偏强震荡": "#c76b7e",
    "震荡": "#6b7280",
    "偏弱震荡": "#4f8a72",
    "下行": "#18794e",
    "数据不足": "#9ca3af",
}
UP = "#ad384e"
DOWN = "#18794e"
MA_COLOR = "#246b89"


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def analysis_of(result: dict[str, Any] | None) -> dict[str, Any]:
    return ((result or {}).get("analysis") or {}) if isinstance(result, dict) else {}


def three_day_text(analysis: dict[str, Any], unit_label: str = "") -> str | None:
    three = analysis.get("three_day") or {}
    if not three.get("available"):
        return None
    percent = three.get("change_pct")
    height = three.get("height")
    if percent is None or height is None:
        return None
    direction = "上涨" if percent > 0 else "下跌" if percent < 0 else "持平"
    unit = unit_label or ""
    return (f"最近 3 个交易日{direction} {abs(percent):.2f}%，"
            f"上升高度 {height:+.2f}{unit}；连续上涨 {three.get('consecutive_up_days', 0)} 天。")


def verdict_badge(analysis: dict[str, Any]) -> str:
    verdict = analysis.get("verdict") or "数据不足"
    score = analysis.get("score")
    if score is None:
        return f"走势判断：{verdict}"
    return f"走势判断：{verdict}（{score:.1f} 分，置信度 {analysis.get('confidence', 0):.0%}）"


def render_trend_metrics(analysis: dict[str, Any], *, unit_label: str = "") -> None:
    """四张卡：MA5、收盘相对 MA5、三日累计、判断。"""

    if not analysis.get("available"):
        reason = next((note for note in analysis.get("notes") or [] if note), "带日期的收盘价不足，暂不给出走势判断。")
        st.caption(reason)
        return
    unit = unit_label or ""
    columns = st.columns(4)
    with columns[0]:
        ma5 = _number(analysis.get("ma5"))
        st.metric("五日均线 MA5", f"{ma5:,.2f}{unit}" if ma5 is not None else "—",
                  delta=(f"{analysis['ma5_slope_pct']:+.2f}%" if analysis.get("ma5_slope_pct") is not None else None),
                  border=True)
    with columns[1]:
        bias = analysis.get("price_vs_ma5_pct")
        st.metric("收盘相对 MA5", f"{bias:+.2f}%" if bias is not None else "—", border=True)
    with columns[2]:
        three = analysis.get("three_day") or {}
        change = three.get("change_pct")
        st.metric("连续三日累计", f"{change:+.2f}%" if change is not None else "—",
                  delta=(f"高度 {three['height']:+.2f}" if three.get("height") is not None else None),
                  border=True)
    with columns[3]:
        verdict = analysis.get("verdict") or "数据不足"
        st.metric("中和判断", verdict,
                  delta=(f"{analysis['score']:.1f} 分" if analysis.get("score") is not None else None),
                  border=True)
    caption = three_day_text(analysis, unit)
    if caption:
        st.caption(caption)


def render_trend_chart(result: dict[str, Any], *, key: str, height: int = 260) -> None:
    """收盘序列叠加五日均线；两段数据都必须带真实日期。"""

    points = result.get("points") or []
    ma5 = result.get("ma5") or []
    if len(points) < 2:
        st.caption("带日期的数值不足，暂时无法绘制走势。")
        return
    label = str(result.get("metric_label") or "收盘价")
    close_frame = pd.DataFrame([{"日期": str(point["date"]), "数值": float(point["value"])} for point in points])
    close_frame["日期"] = pd.to_datetime(close_frame["日期"])
    layers = [alt.Chart(close_frame).mark_line(color=UP, point=True, strokeWidth=2.5).encode(
        x=alt.X("日期:T", title=None, axis=alt.Axis(format="%m/%d")),
        y=alt.Y("数值:Q", title=label, scale=alt.Scale(zero=False)),
        tooltip=[alt.Tooltip("日期:T", title="日期", format="%Y-%m-%d"),
                 alt.Tooltip("数值:Q", title=label, format=",.4f")],
    )]
    if len(ma5) >= 2:
        ma_frame = pd.DataFrame([{"日期": str(point["date"]), "均线": float(point["value"])} for point in ma5])
        ma_frame["日期"] = pd.to_datetime(ma_frame["日期"])
        layers.append(alt.Chart(ma_frame).mark_line(color=MA_COLOR, strokeWidth=1.8, strokeDash=[5, 3]).encode(
            x="日期:T", y=alt.Y("均线:Q", title=None),
            tooltip=[alt.Tooltip("日期:T", title="日期", format="%Y-%m-%d"),
                     alt.Tooltip("均线:Q", title="MA5", format=",.4f")],
        ))
    chart = alt.layer(*layers).resolve_scale(y="shared").properties(height=height)
    render_chart(chart.configure_view(stroke=None), width="stretch", key=f"trend_chart_{key}")
    caption = (f"数据区间 {str(points[0]['date'])} 至 {str(points[-1]['date'])} · {len(points)} 期"
               + (" · 虚线为五日均线" if len(ma5) >= 2 else ""))
    st.caption(caption)


def render_signal_chart(analysis: dict[str, Any], *, key: str) -> None:
    signals = analysis.get("signals") or []
    if not signals:
        return
    rows = [{"信号": signal.get("label") or signal.get("key"), "得分": float(signal.get("score") or 0),
             "权重": float(signal.get("weight") or 0), "读数": signal.get("reading") or ""}
            for signal in signals]
    frame = pd.DataFrame(rows)
    chart = alt.Chart(frame).mark_bar(cornerRadiusEnd=4).encode(
        y=alt.Y("信号:N", sort=None, title=None, axis=alt.Axis(labelLimit=180)),
        x=alt.X("得分:Q", title="偏弱 ← 0 → 偏强", scale=alt.Scale(domain=[-1, 1])),
        color=alt.condition(alt.datum.得分 >= 0, alt.value(UP), alt.value(DOWN)),
        tooltip=[alt.Tooltip("信号:N"), alt.Tooltip("读数:N"),
                 alt.Tooltip("得分:Q", format=".2f"), alt.Tooltip("权重:Q", format=".2f")],
    ).properties(height=max(180, len(rows) * 34))
    render_chart(chart.configure_view(stroke=None), width="stretch", key=f"signal_chart_{key}")


def render_trend_panel(result: dict[str, Any], *, key: str, unit_label: str = "",
                       show_signals: bool = True) -> None:
    """完整的走势观察面板：卡片 + 曲线 + 信号拆解 + 文字限制。

    旧版响应可能没有 ``analysis`` 字段，此时只画价格曲线，不虚构均线与判断。
    """

    analysis = analysis_of(result)
    if analysis:
        render_trend_metrics(analysis, unit_label=unit_label)
    render_trend_chart(result, key=key)
    if analysis and analysis.get("neutralized"):
        st.info("多空信号同时成立，系统按“中和”原则不给出单边方向，判断降级为震荡。",
                icon=":material/balance:")
    if show_signals:
        with st.expander("查看信号拆解与计算口径", key=f"trend_signals_{key}", on_change="rerun") as panel:
            if panel.open:
                render_signal_chart(analysis, key=key)
                for signal in analysis.get("signals") or []:
                    note = signal.get("note")
                    st.caption(f"{signal.get('label')}：{signal.get('reading')}" + (f"（{note}）" if note else ""))
    for note in analysis.get("notes") or []:
        if note:
            st.caption(note)
    st.caption("走势判断由收盘价与均线等可复算信号加权得出，是描述性观察，不预测收益，也不进入投资建议的证据链。")
