"""分析过程与结论的可视化面板。

把一次分析拆成两屏：左边是“过程”（阶段耗时、取数能力、任务节点、模型调用），
右边是“结论”（置信度、分项评分、风险与来源分布）。全部数据都来自响应里已
有的审计字段，界面不推算、不补造任何数值。
"""
from __future__ import annotations

from typing import Any

import streamlit as st
from frontend.charts import render_chart

from frontend.presentation import plain_language

STAGE_ORDER = ("核对投资偏好", "理解问题", "自动取数", "专业研判", "事实核验", "合规审核", "保存对话", "已完成")
STATUS_LABELS = {"PASS": "通过", "REVIEW": "待复核", "BLOCK": "已拦截"}
AGENT_LABELS = {"market": "市场环境", "industry": "行业分析", "security": "个股研究",
                "fund": "基金筛选", "convertible": "可转债", "portfolio": "组合诊断",
                "macro": "宏观", "risk": "风险", "compliance": "合规"}


def _agent_label(agent_id: str | None) -> str:
    return AGENT_LABELS.get(str(agent_id or ""), str(agent_id or "—"))


def _stage_rows(advice: dict[str, Any]) -> list[dict[str, Any]]:
    timings = advice.get("timings_ms") or {}
    known = [stage for stage in STAGE_ORDER if stage in timings]
    rest = [stage for stage in timings if stage not in STAGE_ORDER and stage != "total"]
    rows = [{"阶段": stage, "耗时（毫秒）": round(float(timings[stage]), 1)}
            for stage in [*known, *rest] if float(timings[stage] or 0) > 0]
    return rows


def render_process_panel(advice: dict[str, Any], *, key: str) -> None:
    import altair as alt
    import pandas as pd

    acquisition = advice.get("data_acquisition") or {}
    timings = advice.get("timings_ms") or {}
    calls = advice.get("model_calls") or []
    facts = advice.get("facts") or []
    cited = set(advice.get("evidence") or [])
    columns = st.columns(4)
    with columns[0]:
        st.metric("完整分析耗时", f"{float(timings.get('total') or 0):,.0f} 毫秒", border=True)
    with columns[1]:
        st.metric("参与核验资料", f"{len(facts)} 条", border=True)
    with columns[2]:
        st.metric("实际引用", f"{len(cited)} 条", border=True)
    with columns[3]:
        st.metric("模型调用", f"{len(calls)} 次", border=True)
    rows = _stage_rows(advice)
    if rows:
        frame = pd.DataFrame(rows)
        chart = alt.Chart(frame).mark_bar(color="#ad384e", cornerRadiusEnd=4).encode(
            y=alt.Y("阶段:N", sort=[row["阶段"] for row in rows], title=None,
                    axis=alt.Axis(labelLimit=140)),
            x=alt.X("耗时（毫秒）:Q", title="阶段耗时（毫秒）"),
            tooltip=["阶段:N", alt.Tooltip("耗时（毫秒）:Q", format=",.0f")],
        ).properties(height=max(180, len(rows) * 34))
        render_chart(chart.configure_view(stroke=None), width="stretch", key=f"insight_stages_{key}")
        st.caption("阶段耗时来自服务端计时；并行阶段按实际等待时间记录，总和可能小于完整耗时。")
    else:
        st.caption("本次响应没有阶段耗时记录（可能来自历史精简记录）。")

    st.markdown("**取数与补证**")
    mode = acquisition.get("mode") or acquisition.get("assessment_status") or "未记录"
    requested = acquisition.get("requested_capabilities") or []
    successful = acquisition.get("successful_capabilities") or []
    reused = acquisition.get("reused_capabilities") or []
    failed = acquisition.get("failed_capabilities") or []
    st.caption(f"取数状态：{mode} · 计划能力 {len(requested)} 项 · 成功 {len(successful)} 项 · "
               f"沿用 {len(reused)} 项 · 失败 {len(failed)} 项 · 补证 {acquisition.get('recovery_rounds', 0)} 轮")
    if requested:
        chips = []
        for capability in requested:
            mark = "✓" if capability in successful else "↺" if capability in reused else "✗" if capability in failed else "·"
            chips.append(f"{mark} {capability}")
        st.caption("　".join(chips))
    if acquisition.get("message"):
        st.caption(plain_language(acquisition["message"]))

    nodes = (advice.get("task_plan") or {}).get("nodes") or []
    if nodes:
        st.markdown("**任务节点**")
        st.dataframe([
            {"节点": node.get("task_id"), "智能体": _agent_label(node.get("agent_id")),
             "状态": node.get("status"), "依赖": "、".join(node.get("depends_on") or []) or "无",
             "超时（秒）": node.get("timeout_seconds")}
            for node in nodes
        ], hide_index=True, width="stretch", key=f"insight_nodes_{key}")
    if calls:
        with st.expander("模型调用明细", key=f"insight_calls_{key}", on_change="rerun") as panel:
            if panel.open:
                st.dataframe([
                    {"用途": call.get("purpose") or call.get("label") or "—",
                     "模型": call.get("model") or call.get("engine") or "—",
                     "状态": {"completed": "已完成", "failed": "失败", "timeout": "超时",
                              "cancelled": "已取消"}.get(call.get("status"), call.get("status") or "—"),
                     "输入字符": call.get("input_chars"),
                     "耗时（毫秒）": call.get("total_ms") if call.get("total_ms") is not None else "—"}
                    for call in calls
                ], hide_index=True, width="stretch", key=f"calls_table_{key}")


def render_conclusion_panel(advice: dict[str, Any], *, key: str) -> None:
    import altair as alt
    import pandas as pd

    compliance = (advice.get("compliance") or {}).get("status") or "—"
    confidence = float(advice.get("confidence") or 0)
    risks = advice.get("risks") or []
    columns = st.columns(3)
    with columns[0]:
        st.metric("合规状态", STATUS_LABELS.get(compliance, compliance), border=True)
    with columns[1]:
        st.metric("综合置信度", f"{confidence:.0%}", border=True)
    with columns[2]:
        st.metric("风险提示", f"{len(risks)} 条", border=True)
    st.progress(min(1.0, max(0.0, confidence)), text="结论置信度（由证据完整度、一致性与核验结果聚合）")

    results = advice.get("agent_results") or []
    rows = [{"智能体": _agent_label(result.get("agent_id")),
             "规则分": result.get("rule_score"), "模型分": result.get("model_score"),
             "采用分": result.get("score"), "置信度": result.get("confidence")}
            for result in results if result.get("score") is not None or result.get("rule_score") is not None]
    if rows:
        st.markdown("**分项评分**")
        frame = pd.DataFrame(rows)
        melted = frame.melt(id_vars=["智能体"], value_vars=["规则分", "模型分"], var_name="来源", value_name="分数").dropna()
        if not melted.empty:
            chart = alt.Chart(melted).mark_bar(cornerRadiusEnd=4).encode(
                y=alt.Y("智能体:N", title=None, axis=alt.Axis(labelLimit=120)),
                x=alt.X("分数:Q", title="评分（0–100，规则分可复算）", scale=alt.Scale(domain=[0, 100])),
                color=alt.Color("来源:N", scale=alt.Scale(domain=["规则分", "模型分"], range=["#ad384e", "#246b89"])),
                xOffset="来源:N",
                tooltip=["智能体:N", "来源:N", alt.Tooltip("分数:Q", format=".1f")],
            ).properties(height=max(180, len(frame) * 40))
            render_chart(chart.configure_view(stroke=None), width="stretch", key=f"insight_scores_{key}")
        st.caption("规则分由确定性规则计算，模型分只用于解释与对照；分数不代表预期收益。")

    sources: dict[str, int] = {}
    for fact in advice.get("facts") or []:
        source = str(fact.get("source_id") or "未知来源")
        sources[source] = sources.get(source, 0) + 1
    if sources:
        st.markdown("**资料来源分布**")
        frame = pd.DataFrame([{"来源": name, "资料条数": count} for name, count in sources.items()])
        chart = alt.Chart(frame).mark_bar(cornerRadiusEnd=4, color="#246b89").encode(
            y=alt.Y("来源:N", sort="-x", title=None),
            x=alt.X("资料条数:Q", title="条"),
            tooltip=["来源:N", "资料条数:Q"],
        ).properties(height=max(140, len(frame) * 32))
        render_chart(chart.configure_view(stroke=None), width="stretch", key=f"insight_sources_{key}")
    if risks:
        st.markdown("**风险与待核实事项**")
        for risk in risks[:8]:
            st.write("- " + plain_language(risk))
    issues = (advice.get("cross_validation") or {}).get("issues") or []
    if issues:
        st.markdown("**交叉核验发现**")
        st.dataframe([{"级别": issue.get("severity"), "说明": plain_language(issue.get("message"))}
                      for issue in issues], hide_index=True, width="stretch", key=f"insight_issues_{key}")


def render_insight_panel(advice: dict[str, Any], *, key: str, compact: bool = False) -> None:
    """两页签可视化面板；历史回答默认折叠，结论页签始终可展开核对。"""

    with st.expander("分析过程与结论可视化面板", key=f"insight_panel_{key}", on_change="rerun",
                     icon=":material/dashboard:") as panel:
        if not panel.open:
            return
        process, conclusion = st.tabs(["分析过程", "结论可视化"])
        with process:
            render_process_panel(advice, key=key)
        with conclusion:
            render_conclusion_panel(advice, key=key)
