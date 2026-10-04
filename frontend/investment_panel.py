"""收益情景、仓位区间的统一展示，供回答、历史详情与导出复用。"""
from __future__ import annotations

import math
from typing import Any

import streamlit as st


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (ValueError, TypeError):
        return None
    return numeric if math.isfinite(numeric) else None


def return_scenario_rows(advice: dict) -> list[dict]:
    if (advice.get("compliance") or {}).get("status") == "BLOCK":
        return []
    accepted = set(advice.get("evidence") or [])
    facts = {fact.get("fact_id"): fact for fact in advice.get("facts") or []}
    expectation = advice.get("return_expectation") or {}
    if expectation.get("status") != "scenario":
        return []
    rows = []
    for scenario in expectation.get("scenarios") or []:
        references = scenario.get("evidence") or []
        if len(references) != 2 or not set(references) <= (accepted & facts.keys()):
            continue
        current, target, change = (_number(scenario.get(key)) for key in ("current_price", "target_price", "price_return"))
        if current is None or target is None or change is None or min(current, target) <= 0:
            continue
        if not math.isclose(change, target / current - 1, abs_tol=1e-8):
            continue
        source = facts[references[1]]
        rows.append({"标的": scenario.get("entity"), "参考价": current, "目标价": target,
                     "币种": scenario.get("currency"), "情景空间（%）": change * 100,
                     "参考价日期": scenario.get("price_date"), "目标价资料日期": scenario.get("target_date"),
                     "目标期限": scenario.get("horizon") or "未提供，不作年化",
                     "来源": source.get("source_id"), "原文": source.get("source_url")})
    return rows


def allocation_rows(advice: dict) -> list[dict]:
    if (advice.get("compliance") or {}).get("status") == "BLOCK":
        return []
    accepted = set(advice.get("evidence") or [])
    rows = []
    for item in advice.get("allocation") or []:
        low, high = _number(item.get("min_weight")), _number(item.get("max_weight"))
        if low is None or high is None or not 0 <= low <= high <= 1:
            continue
        # 旧版目标区间没有逐项引用，保留当时的画像依据；新版剔除不被认可的引用。
        if "evidence" in item and (not item["evidence"] or not set(item["evidence"]) <= accepted):
            continue
        rows.append({"资产类别": item.get("asset_class") or "未记录",
                     "下限（%）": low * 100, "上限（%）": high * 100, "依据": item.get("basis") or "未记录"})
    return rows


def investment_summary(advice: dict) -> list[tuple[str, list[str]]]:
    """报告与历史简版使用与完整面板相同的口径。"""
    blocked = (advice.get("compliance") or {}).get("status") == "BLOCK"
    expectation = advice.get("return_expectation") or {}
    scenarios = return_scenario_rows(advice)
    returns = ["风险检查未通过，暂不提供收益情景。" if blocked else
               expectation.get("summary") or "尚未形成可核验的收益预测。"]
    if not blocked and expectation.get("status") == "scenario" and not scenarios:
        returns = ["这条记录缺少完整的收益情景依据，请重新分析后查看。"]
    if not blocked:
        goal = _number(expectation.get("user_goal_annual"))
        if goal is not None:
            returns.append(f"您的期望年化收益：{goal:.1%}（个人目标，不是系统预测）。")
        if expectation.get("investment_horizon_months"):
            returns.append(f"已确认投资期限：{expectation['investment_horizon_months']} 个月；与目标价期限分别核对。")
        for row in scenarios:
            returns.append(f"{row['标的']}：参考价 {row['参考价']:g}，目标价 {row['目标价']:g} {row['币种']}，"
                           f"情景空间 {row['情景空间（%）']:+.2f}%；{row['目标期限']}。")
        if scenarios:
            returns.extend(expectation.get("assumptions") or [])
    allocations = allocation_rows(advice)
    positions = [f"{row['资产类别']}：{row['下限（%）']:g}%–{row['上限（%）']:g}%；{row['依据']}。" for row in allocations]
    positions = positions or ["暂不提供仓位建议。" if blocked else "本次未形成仓位区间；可在持仓分析中提交持仓并核对风险评估。"]
    if allocations:
        positions.append("各区间不应直接相加；选定比例合计应为 100%，并复核期限、流动性及集中度约束。")
        positions.append("资产类别目标区间仅供研究参考；不构成具体标的买卖或自动交易指令。")
    return [("收益预期", returns), ("仓位建议", positions)]


def render_investment_panel(advice: dict, *, key: str, compact: bool = False) -> None:
    sections = investment_summary(advice)
    if compact:
        for title, texts in sections:
            st.markdown(f"**{title}**")
            st.write(texts[0])
        return
    import altair as alt
    import pandas as pd

    with st.expander("收益预期", expanded=False, key=f"return_expectation_{key}"):
        returns = return_scenario_rows(advice)
        for text in sections[0][1]:
            st.write(text)
        if returns:
            frame = pd.DataFrame(returns)
            frame["情景"] = [f"{row['标的']} · 资料 {index + 1}" for index, row in enumerate(returns)]
            chart = alt.Chart(frame).mark_bar().encode(
                x=alt.X("情景空间（%）:Q", title="目标价情景空间（%，非收益预测）"),
                y=alt.Y("情景:N", title=None, sort=None),
                color=alt.condition(alt.datum['情景空间（%）'] >= 0, alt.value("#ad384e"), alt.value("#18794e")),
                tooltip=["标的:N", "参考价:Q", "目标价:Q", "币种:N", alt.Tooltip("情景空间（%）:Q", format="+.2f"), "目标期限:N"],
            )
            st.altair_chart(chart.properties(height=max(130, 45 * len(returns))), width="stretch", key=f"returns_{key}")
            st.dataframe(returns, hide_index=True, width="stretch", column_config={
                "情景空间（%）": st.column_config.NumberColumn(format="%.2f"),
                "原文": st.column_config.LinkColumn(display_text="查看原文"),
            })
    with st.container(border=True):
        st.markdown("**仓位建议**")
        positions = allocation_rows(advice)
        if positions:
            chart = alt.Chart(pd.DataFrame(positions)).mark_bar(size=18).encode(
                x=alt.X("下限（%）:Q", title="资产类别目标区间（%）", scale=alt.Scale(domain=[0, 100])),
                x2="上限（%）:Q", y=alt.Y("资产类别:N", title=None, sort=None),
                tooltip=["资产类别:N", "下限（%）:Q", "上限（%）:Q", "依据:N"],
            )
            st.altair_chart(chart.properties(height=max(140, 45 * len(positions))), width="stretch", key=f"allocation_{key}")
            st.dataframe(positions, hide_index=True, width="stretch")
        for text in sections[1][1]:
            st.write(text)
        if positions and (advice.get("compliance") or {}).get("status") != "PASS":
            st.warning("这份分析仍待复核，区间仅用于说明配置思路，请勿直接据此调整持仓。")
