"""主要指数看板：行情卡片、归一化走势对比与单指数趋势判断。"""
from __future__ import annotations

from time import monotonic
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st
from frontend.charts import render_chart

from frontend.trend_view import render_trend_panel

CACHE_TTL = 60


def load_index_board(api_base: str, api_request, *, refresh: bool = False) -> dict[str, Any]:
    """会话内缓存 60 秒；刷新按钮显式跳过缓存。"""

    cache = st.session_state.setdefault("index_board_cache", {})
    entry = cache.get(api_base)
    if entry and not refresh and monotonic() - entry["loaded_at"] < CACHE_TTL:
        return entry["result"]
    result = api_request(api_base, "POST", "/market/index-board", {"refresh": refresh})
    if not isinstance(result, dict) or "items" not in result:
        result = {"status": "unavailable", "items": [],
                  "notes": ["指数看板暂不可用，请确认已登录且数据源已配置。"]}
    cache[api_base] = {"loaded_at": monotonic(), "result": result}
    return result


def _normalized_frame(items: list[dict[str, Any]]) -> pd.DataFrame:
    """以各自首个点为 100 归一化，避免不同点位量级无法同图比较。"""

    rows: list[dict[str, Any]] = []
    for item in items:
        points = item.get("points") or []
        dates = item.get("dates") or []
        if len(points) < 2 or len(points) != len(dates):
            continue
        base = float(points[0]) or 1.0
        for day, value in zip(dates, points):
            rows.append({"日期": day, "指数": item.get("name"), "归一化": round(float(value) / base * 100, 2)})
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame["日期"] = pd.to_datetime(frame["日期"])
    return frame


def render_index_board(api_base: str, api_request, *, key: str = "market") -> None:
    with st.container(horizontal=True, vertical_alignment="center"):
        st.markdown("**主要指数 · 五日均线、连续三日升幅与中和判断**")
        st.space("stretch")
        refresh = st.button("刷新指数", key=f"index_board_refresh_{key}", icon=":material/refresh:")
    result = load_index_board(api_base, api_request, refresh=refresh)
    items = result.get("items") or []
    if not items:
        st.caption((result.get("notes") or ["指数看板暂不可用。"])[0])
        return
    for start in range(0, len(items), 3):
        columns = st.columns(3)
        for column, item in zip(columns, items[start:start + 3]):
            with column:
                latest = item.get("latest_close")
                change = item.get("change_pct")
                st.metric(item.get("name"), f"{latest:,.2f}" if latest is not None else "—",
                          delta=(f"{change:+.2f}%" if change is not None else None),
                          border=True)
                caption = []
                if item.get("ma5") is not None:
                    caption.append(f"MA5 {item['ma5']:,.2f}")
                if item.get("three_day_pct") is not None:
                    caption.append(f"3日 {item['three_day_pct']:+.2f}%")
                verdict = item.get("verdict")
                if verdict and verdict != "数据不足":
                    caption.append(verdict)
                st.caption(" · ".join(caption) if caption else (item.get("message") or "历史点位暂未取得"))
    frame = _normalized_frame(items)
    if not frame.empty:
        with st.container(border=True):
            st.markdown("**指数走势对比（起点归一为 100）**")
            chart = alt.Chart(frame).mark_line(strokeWidth=2).encode(
                x=alt.X("日期:T", title=None, axis=alt.Axis(format="%m/%d")),
                y=alt.Y("归一化:Q", title="归一化点位", scale=alt.Scale(zero=False)),
                color=alt.Color("指数:N", title=None),
                tooltip=[alt.Tooltip("日期:T", format="%Y-%m-%d"), alt.Tooltip("指数:N"),
                         alt.Tooltip("归一化:Q", format=".2f")],
            ).properties(height=280)
            render_chart(chart.configure_view(stroke=None), width="stretch", key=f"index_compare_{key}")
            st.caption("归一化只用于同图比较相对强弱，不代表收益率；各指数点位量级不同。")
    options = [item for item in items if (item.get("analysis") or {}).get("available")]
    if options:
        names = {item["name"]: item for item in options}
        selected = st.selectbox("查看单个指数的走势与判断", list(names), key=f"index_focus_{key}")
        item = names[selected]
        with st.container(border=True):
            st.markdown(f"**{item['name']} · {item.get('code')}**")
            pseudo = {
                "points": [{"date": day, "value": value}
                           for day, value in zip(item.get("dates") or [], item.get("points") or [])],
                "ma5": (item.get("analysis") or {}).get("ma5_series") or [],
                "metric_label": "收盘点位",
                "analysis": item.get("analysis") or {},
            }
            render_trend_panel(pseudo, key=f"index_{key}_{item.get('code')}", unit_label=" 点")
    for note in result.get("notes") or []:
        st.caption(note)
    st.caption("指数走势来自问财历史点位；判断只描述已发生的价格序列，不构成投资建议。")
