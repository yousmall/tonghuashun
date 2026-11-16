"""自选横向比较的投资价值排序展示（确定性规则结果）。"""
from __future__ import annotations

from typing import Any

import altair as alt
import pandas as pd
import streamlit as st
from frontend.charts import render_chart


def _label(candidate: dict[str, Any]) -> str:
    name = candidate.get("name") or candidate.get("target") or "—"
    code = candidate.get("code")
    return f"{name} · {code}" if code and code not in str(name) else str(name)


def render_decision(decision: dict[str, Any] | None, *, key: str = "comparison") -> None:
    if not isinstance(decision, dict):
        return
    st.markdown("**投资价值比较与决策**")
    if decision.get("status") != "ok":
        st.info(decision.get("conclusion") or "现有资料不足以比较投资价值。",
                icon=":material/help:")
        for note in decision.get("missing") or []:
            st.caption("缺少可比较口径：" + str(note))
        return
    candidates = decision.get("candidates") or []
    preferred = decision.get("preferred_name") or decision.get("preferred") or "—"
    st.success(decision.get("conclusion") or f"综合比较后，{preferred} 相对更具投资价值。",
               icon=":material/insights:")
    frame = pd.DataFrame([{"标的": _label(candidate), "综合得分": float(candidate.get("score") or 0)}
                          for candidate in candidates])
    chart = alt.Chart(frame).mark_bar(cornerRadiusEnd=4, color="#ad384e").encode(
        y=alt.Y("标的:N", sort="-x", title=None, axis=alt.Axis(labelLimit=160)),
        x=alt.X("综合得分:Q", title="综合得分（0–100，仅组内相对）", scale=alt.Scale(domain=[0, 100])),
        tooltip=["标的:N", alt.Tooltip("综合得分:Q", format=".1f")],
    ).properties(height=max(150, len(frame) * 44))
    render_chart(chart.configure_view(stroke=None), width="stretch", key=f"decision_scores_{key}")

    dimensions = decision.get("dimensions") or []
    rows = []
    for candidate in candidates:
        row = {"标的": _label(candidate), "综合得分": round(float(candidate.get("score") or 0), 1),
               "覆盖度": f"{float(candidate.get('coverage') or 0):.0%}"}
        scores = candidate.get("dimension_scores") or {}
        for dimension in dimensions:
            row[dimension.get("label")] = round(float(scores.get(dimension.get("key")) or 0), 0)
        row["优势"] = "；".join(candidate.get("strengths") or []) or "—"
        row["不足"] = "；".join(candidate.get("weaknesses") or []) or "—"
        rows.append(row)
    st.dataframe(rows, hide_index=True, width="stretch", key=f"decision_table_{key}")
    if dimensions:
        st.caption("评分维度：" + "；".join(
            f"{item.get('label')}（{item.get('field_label') or item.get('field')}，权重 {float(item.get('weight') or 0):.0%}）"
            for item in dimensions))
    for note in decision.get("caveats") or []:
        st.caption(note)
    if decision.get("missing"):
        with st.expander("未纳入比较的维度", key=f"decision_missing_{key}", on_change="rerun") as panel:
            if panel.open:
                for note in decision["missing"]:
                    st.write("- " + str(note))
