"""回答展示、交互图表及完整 PDF 导出。"""
from __future__ import annotations
from functools import partial
from typing import Any
import re
from urllib.parse import urlsplit, urlunsplit
import streamlit as st
from frontend.charts import render_chart
from frontend.research_report import render_research_report, report_sections
from frontend.insight_panel import render_insight_panel
from frontend.cross_validation_view import render_validation_panel
from frontend.answer_report import answer_images, build_answer_pdf, chart_groups, cited_facts
from frontend.presentation import (plain_language, render_conclusion_panel, render_logic_chain, render_source_trace, source_trace_rows, friendly_fact_rows, TOPIC_LABELS)
from frontend.presentation import render_verification_records
from frontend.logic_drilldown import render_logic_drilldown
from frontend.investment_panel import render_investment_panel, investment_summary
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
        render_chart(chart.properties(height=height).interactive(), width="stretch", key=f"answer_chart_{key}_{index}")
        st.caption("数据期间 " + group["period"] + " · 仅展示本回答实际引用的数据，不代表收益预测。")


def _download_pdf(advice, question):
    # 下载回调在独立线程运行，只读取捕获的报告数据。
    return build_answer_pdf(answer_export_payload(advice, answer_images(advice), question))


def render_stock_recommendation(advice: dict[str, Any]) -> None:
    report = advice.get("stock_recommendation")
    if not report:
        return
    candidates = report.get("candidates") or []
    passed = (advice.get("compliance") or {}).get("status") == "PASS" and (
        advice.get("cross_validation") or {}).get("status") == "PASS"
    recommended = {item["symbol"] for item in report.get("recommendations") or []
                   if passed and item.get("status") == "recommended"}
    if not recommended:
        st.info("本次暂无通过核验的推荐股票，原因见分析结论。")
    if not candidates:
        return
    with st.expander("候选股票核验记录", icon=":material/fact_check:"):
        st.caption(f"本次核验 {len(candidates)} 只候选股票，其中 {len(recommended)} 只进入推荐名单。")
        st.dataframe([
            {
                "代码": candidate["symbol"],
                "名称": candidate["name"],
                "结果": "通过核验" if candidate["symbol"] in recommended else
                        "未入选" if candidate.get("status") == "excluded" else "待复核",
                "理由": "。".join(plain_language(reason) for reason in candidate.get("reasons") or []),
                "主要风险": "。".join(plain_language(risk) for risk in candidate.get("risks") or []),
                "资料日期": str(candidate.get("snapshot_time") or "")[:10],
            }
            for candidate in candidates
        ], hide_index=True, width="stretch")


def render_automatic_answer(advice: dict[str, Any]) -> None:
    st.write(plain_language(advice.get("conclusion")))
    ids = advice.get("answer_fact_ids") or advice.get("evidence") or []
    verified = set(advice.get("evidence") or [])
    facts = {fact["fact_id"]: fact for fact in advice["facts"] if fact.get("fact_id") in verified}
    rows = []
    for index, fact_id in enumerate(ids, start=1):
        if fact_id not in facts:
            continue
        fact = facts[fact_id]
        row = friendly_fact_rows([fact])[0]
        if fact.get("field") in {"revenue", "net_profit", "close_price"} and fact.get("unit"):
            unit = "元" if fact["unit"] == "CNY" else fact["unit"]
            row["内容 / 数值"] = f"{fact['value']} {unit}"
        if fact.get("entity_code"):
            row["对象"] = f"{row['对象']} · {fact['entity_code']}"
        page = re.search(r"第\s*(\d+)\s*页", str(fact.get("source_field") or ""))
        if page:
            row["原文页码"] = page[1]
        try:
            link = urlsplit(fact.get("source_url") or "")
            if link.scheme in {"http", "https"} and link.hostname and not link.username:
                if page and link.path.lower().endswith(".pdf"):
                    link = link._replace(fragment=f"page={page[1]}")
                row["原文"] = urlunsplit(link)
        except ValueError:
            pass
        rows.append(row if advice.get("source_reference") else {"编号": index, **row})
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch",
                     column_config={"原文": st.column_config.LinkColumn(display_text="打开来源")})
    if (advice.get("cross_validation") or {}).get("issues"):
        st.caption("存在相同口径下数值不一致的资料，相关指标已撤下。")
    acquisition = advice.get("data_acquisition") or {}
    if acquisition.get("mode") == "unavailable" or acquisition.get("reason_code") == "PROVIDER_QUOTA_EXHAUSTED":
        st.caption(acquisition.get("message") or "部分资料暂不可用，可稍后重试。")


def render_advice(advice: dict[str, Any], *, export_key: str = "answer", question: str = "", compact: bool = False) -> None:
    """Conversation bubbles contain only the reviewed conclusion and material risks."""
    compliance = advice.get("compliance") or {}
    st.markdown("**分析结论**")
    st.write(plain_language(advice.get("conclusion")) or "现有资料不足，暂时无法作出判断。")
    st.markdown("**风险结论**")
    risks = [visible_risk_conclusion(advice), *visible_risks(advice),
             *compliance.get("required_disclosures", []), compliance.get("risk_notice")]
    if compliance.get("status") == "BLOCK":
        risks.insert(0, "本次风险检查未通过，暂不形成可执行的投资建议。")
    elif compliance.get("status") != "PASS" and ("LIMITED_CONSULTATION" not in (compliance.get("matched_rules") or [])
                                                  or (advice.get("cross_validation") or {}).get("issues")):
        risks.insert(0, "这份分析仍需核实。")
    if (advice.get("data_acquisition") or {}).get("mode") == "demo" or any(
            fact.get("source_id") == "DEMO_SNAPSHOT" for fact in advice.get("facts") or []):
        risks.append("本结果含示例数据，仅用于演示。")
    reason = visible_compliance_reason(advice)
    if compliance.get("status") != "PASS" and reason:
        risks.append(reason)
    risks = list(dict.fromkeys(text for value in risks if (text := plain_language(value))))
    if not risks:
        risks = ["标的价格可能波动，持有期间可能出现亏损。"]
    for value in risks:
        st.write(value)


def render_advice_details(advice: dict[str, Any], *, export_key: str = "answer", question: str = "", compact: bool = False) -> None:
    """最近回答优先展示结论与图表；历史回答折叠细节，重要风险始终可见。"""
    advice = {**advice, "facts": list(advice.get("facts") or [])}
    compliance = advice.get("compliance") or {}
    acquisition = advice.get("data_acquisition") or {}
    if (advice.get("prepared_report") or {}).get("reused"):
        generated = advice["prepared_report"].get("generated_at", "")
        st.caption(f"完整深入报告已于 {generated} 完成分析及审核；本次核对资料与投资偏好后复用。")
    if advice.get("scope_notice"):
        st.caption(advice["scope_notice"])
    if advice.get("history_status") == "pending":
        st.caption("咨询记录稍后可在历史记录中查看。")
    if (advice.get("research_mode") == "auto" and compliance.get("status") != "BLOCK"
            and (advice.get("answer_fact_ids") or advice.get("source_reference") or advice.get("response_scope") == "clarification")):
        render_automatic_answer(advice)
        render_validation_panel(advice, key=export_key)
        render_research_report(advice, key=export_key, compact=compact, question=question)
        render_insight_panel(advice, key=export_key, compact=compact)
        if advice.get("evidence"):
            _render_pdf_download(advice, question, export_key)
        return
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
        risks = list(dict.fromkeys(text for value in risks if (text := plain_language(value))))
        if risks:
            st.markdown("**风险与待核实事项**")
            for value in risks:
                st.write("- " + value)
        render_investment_panel(advice, key=export_key, compact=True)
        render_validation_panel(advice, key=export_key)
        with st.expander("展开这条回答的图表与完整分析", key=f"answer_details_{export_key}", on_change="rerun") as details:
            if details.open:
                render_investment_panel(advice, key=export_key)
                render_answer_charts(advice, key=export_key)
                by_dimension, drilldown = st.tabs(
                    ["按维度展开", "逐级下钻与溯源"],
                    key=f"logic_view_compact_{export_key}", on_change="rerun",
                )
                if by_dimension.open:
                    with by_dimension:
                        render_logic_chain(advice)
                if drilldown.open:
                    with drilldown:
                        render_logic_drilldown(advice, key=f"drill_compact_{export_key}")
                render_source_trace(advice, key=f"sources_{export_key}")
        render_verification_records(advice, key=export_key)
    else:
        render_conclusion_panel(advice, key=f"analysis-conclusion-{export_key}")
        render_validation_panel(advice, key=export_key)
        if advice.get("prepared_report"):
            with st.expander("查看完整报告图表", key=f"prepared_charts_{export_key}", on_change="rerun") as charts:
                if charts.open:
                    render_answer_charts(advice, key=export_key)
        elif advice.get("response_scope", "full") == "full":
            render_answer_charts(advice, key=export_key)
        else:
            with st.expander("查看已有资料图表", key=f"lite_charts_{export_key}", on_change="rerun") as charts:
                if charts.open:
                    render_answer_charts(advice, key=export_key)
        with st.expander("分析过程与分项依据", key=f"answer_analysis_{export_key}", on_change="rerun", icon=":material/account_tree:") as details:
            if details.open:
                by_dimension, drilldown = st.tabs(
                    ["按维度展开", "逐级下钻与溯源"],
                    key=f"logic_view_{export_key}", on_change="rerun",
                )
                if by_dimension.open:
                    with by_dimension:
                        render_logic_chain(advice)
                if drilldown.open:
                    with drilldown:
                        render_logic_drilldown(advice, key=f"drill_{export_key}")
        render_source_trace(advice, key=f"sources_{export_key}")
    render_research_report(advice, key=export_key, compact=compact, question=question)
    render_stock_recommendation(advice)
    render_insight_panel(advice, key=export_key, compact=compact)
    if acquisition.get("reused_capabilities"):
        st.caption("本次沿用了仍在有效期内的已有资料。")
    if acquisition.get("reason_code") == "MODEL_UNAVAILABLE":
        st.caption("智能研判暂不可用，本次采用可复算的规则结果。")
    _render_pdf_download(advice, question, export_key)


def _render_pdf_download(advice, question, export_key):
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
    sections.extend(report_sections(advice))
    sections.extend(investment_summary(advice))
    for result in advice.get("agent_results", []):
        sections.append((TOPIC_LABELS.get(result.get("agent_id"), "相关分析"),
                         [result.get("opinion"), *result.get("confidence_reasons", []), *result.get("risk_flags", []), *result.get("invalidation_conditions", [])]))
    if verification_notes(advice):
        sections.append(("核验记录（附录）", verification_notes(advice)))
    cleaned = [(title, [value for text in texts if (value := plain_language(text))]) for title, texts in sections]
    return {"question": question, "images": images,
            "sections": [(title, values) for title, values in cleaned if values],
            "sources": source_trace_rows(advice)}
