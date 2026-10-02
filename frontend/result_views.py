"""回答展示、交互图表及完整 PDF 导出。"""
from __future__ import annotations
from functools import partial
from typing import Any
import streamlit as st
from frontend.answer_report import answer_images, build_answer_pdf, chart_groups, cited_facts
from frontend.presentation import (plain_language, render_conclusion_panel, render_logic_chain, render_source_trace, source_trace_rows, TOPIC_LABELS)
from frontend.presentation import render_verification_records
from frontend.answer_visibility import coverage_notices, verification_notes, visible_risks, visible_next_steps, visible_risk_conclusion, visible_compliance_reason


def show_status(status: str) -> None:
    if status == "REVIEW":
        st.warning("这份分析还需要进一步确认，请勿直接据此买卖。", icon=":material/fact_check:")
    elif status != "PASS":
        st.error("暂时无法提供投资建议，请先查看下方原因。", icon=":material/report:")


def render_answer_charts(advice, *, key="answer"):
    groups = chart_groups(advice)
    if not groups:
        return
    import altair as alt
    import pandas as pd
    for index, group in enumerate(groups[:2]):
        frame = pd.DataFrame(group["rows"]).rename(columns={"name": "标的", "value": "数值", "date": "期间"})
        st.markdown("**" + group["title"] + "**")
        chart = alt.Chart(frame)
        if group["kind"] == "bar":
            color = alt.condition(alt.datum['数值'] >= 0, alt.value("#ad384e"), alt.value("#18794e")) if "涨跌" in group["title"] else alt.value("#246b89")
            chart = chart.mark_bar(cornerRadiusEnd=4).encode(
                x=alt.X("数值:Q", title=group["unit"] or "数值"),
                y=alt.Y("标的:N", title=None, sort=None,
                        scale=alt.Scale(paddingInner=0.48, paddingOuter=0.25)), color=color,
                tooltip=["标的:N", alt.Tooltip("数值:Q", format=",.4f"), "期间:N"])
        else:
            chart = chart.mark_line(point=True, color="#ad384e").encode(
                x=alt.X("期间:T", title="日期", axis=alt.Axis(format="%m/%d")),
                y=alt.Y("数值:Q", title=group["unit"] or "数值", scale=alt.Scale(zero=False)),
                tooltip=[alt.Tooltip("期间:T", format="%Y-%m-%d"), alt.Tooltip("数值:Q", format=",.4f")])
        height = max(230, len(frame) * 54) if group["kind"] == "bar" else 230
        st.altair_chart(chart.properties(height=height).interactive(), width="stretch", key=f"answer_chart_{key}_{index}")
        st.caption("数据期间 " + group["period"] + " · 仅展示本回答实际引用的数据，不代表收益预测。")


def _download_pdf(advice, question):
    # 下载回调在独立线程运行，只读取捕获的报告数据。
    return build_answer_pdf(answer_export_payload(advice, answer_images(advice), question))


def render_advice(advice: dict[str, Any], *, export_key: str = "answer", question: str = "", compact: bool = False) -> None:
    """最近回答优先展示结论与图表；历史回答折叠细节，重要风险始终可见。"""
    advice = {**advice, "facts": list(advice.get("facts") or [])}
    compliance = advice.get("compliance") or {}
    acquisition = advice.get("data_acquisition") or {}
    notices = []
    if compliance.get("status") != "PASS":
        notices.append("这份分析仍需核实，请勿直接据此买卖。")
    if acquisition.get("mode") == "demo" or any(f.get("source_id") == "DEMO_SNAPSHOT" for f in advice["facts"]):
        notices.append("本结果含示例数据，仅用于演示。")
    if notices:
        st.warning(" ".join(notices), icon=":material/fact_check:")
    if advice.get("snapshot_time"):
        st.caption("资料日期：" + str(advice["snapshot_time"])[:10])
    if "facts_truncated" in advice and not advice.get("history_version"):
        st.caption("早期记录未保存完整分项引用；当时结论与可用来源仍可查看，完整依据请重新分析。")
    if advice.get("facts_truncated"):
        st.caption("历史记录保留了部分引用资料；未保存的依据无法在此恢复，请重新核验后再使用。")
    if compact:
        st.markdown("**分析结论**")
        st.write(plain_language(advice.get("conclusion")) or "资料不足，暂未形成结论。")
        risks = [visible_risk_conclusion(advice), *visible_risks(advice),
                 *compliance.get("required_disclosures", []), compliance.get("risk_notice")]
        risks = list(dict.fromkeys(plain_language(value) for value in risks if value))
        if risks:
            st.markdown("**风险与待核实事项**")
            for value in risks:
                st.write("- " + value)
        with st.expander("展开这条回答的图表与完整分析", key=f"answer_details_{export_key}", on_change="rerun") as details:
            if details.open:
                render_answer_charts(advice, key=export_key)
                render_logic_chain(advice)
                render_source_trace(advice)
        render_verification_records(advice, key=export_key)
    else:
        render_conclusion_panel(advice, key=f"analysis-conclusion-{export_key}")
        render_answer_charts(advice, key=export_key)
        with st.expander("分析过程与分项依据", key=f"answer_analysis_{export_key}", on_change="rerun", icon=":material/account_tree:") as details:
            if details.open:
                render_logic_chain(advice)
        render_source_trace(advice)
    if acquisition.get("reused_capabilities"):
        st.caption("本次沿用了仍在有效期内的已有资料。")
    if acquisition.get("reason_code") == "MODEL_UNAVAILABLE":
        st.caption("智能研判暂不可用，本次采用可复算的规则结果。")
    st.download_button("导出这条回答为 PDF", data=partial(_download_pdf, advice, question),
        file_name="问策智投_投资研究回答.pdf", mime="application/pdf", key=f"answer_pdf_{export_key}",
        icon=":material/picture_as_pdf:", on_click="ignore")


@st.cache_data(show_spinner=False, max_entries=64)
def cached_answer_images(advice: dict[str, Any]) -> list[tuple[str, bytes]]:
    return answer_images(advice)


def answer_export_payload(advice: dict[str, Any], images: list, question: str = "") -> dict:
    compliance = advice.get("compliance", {})
    acquisition = advice.get("data_acquisition", {})
    notices = ["风险检查：" + {"PASS": "可供参考，仍有投资风险", "BLOCK": "暂不提供投资建议"}.get(compliance.get("status"), "仍需进一步核实")]
    if acquisition.get("mode") == "demo" or any(fact.get("source_id") == "DEMO_SNAPSHOT" for fact in advice.get("facts", [])):
        notices.append("本结果含示例数据，仅用于演示。")
    sections = [
        ("分析结论", [advice.get("conclusion") or "资料不足，暂未形成结论。"]),
        ("风险结论", [visible_risk_conclusion(advice) or "请结合资料完整性及个人承受能力进一步核实。", *notices, visible_compliance_reason(advice), compliance.get("risk_notice")]),
        ("需要注意", [*visible_risks(advice), *compliance.get("required_disclosures", [])]),
        ("后续研究建议", visible_next_steps(advice)),
        ("与您的投资偏好是否匹配", [advice.get("user_fit")]),
    ]
    for result in advice.get("agent_results", []):
        sections.append((TOPIC_LABELS.get(result.get("agent_id"), "相关分析"),
                         [result.get("opinion"), *result.get("confidence_reasons", []), *result.get("risk_flags", []), *result.get("invalidation_conditions", [])]))
    if verification_notes(advice):
        sections.append(("核验记录（附录）", verification_notes(advice)))
    return {"question": question, "images": images,
            "sections": [(title, [plain_language(text) for text in texts if text]) for title, texts in sections],
            "sources": source_trace_rows(advice)}
