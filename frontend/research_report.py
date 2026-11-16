"""Render the server's evidence dossier and reuse identical text in exports."""
import streamlit as st
from frontend.presentation import plain_language, fact_source, friendly_fact_rows, advice_facts

STANCES = {"favorable": "支持因素", "caution": "不利因素", "neutral": "中性与待核实"}
FINDING_STANCES = {"favorable": "有利", "caution": "不利", "neutral": "中性/待核实"}
REPORT_STATUS = {"reviewed": "已核验", "needs_review": "待复核", "limited": "范围受限"}


def report_sections(advice):
    if (advice.get("compliance") or {}).get("status") == "BLOCK":
        return []
    report = advice.get("research_report") or {}
    sections = []
    if not report:
        return sections
    sections.append(("研究证据对照", [report.get("summary"), report.get("method"), *report.get("limitations", [])]))
    for section in report.get("sections", []):
        lines = []
        for item in section.get("evidence", []):
            lines.append(f"{STANCES.get(item.get('stance'), '待核实')} · {item.get('observation', '')}"
                         f"（{item.get('period') or '统计期未注明'}）\n{item.get('interpretation', '')}"
                         f"\n来源：{fact_source({'source_id': item.get('source')})}")
        lines.extend("判断限制：" + value for value in section.get("limitations", []))
        lines.extend("重新评估条件：" + value for value in section.get("change_conditions", []))
        sections.append((section.get("entity", "") + " · " + section.get("title", ""), lines))
    return sections


def report_markdown(advice, question: str = "") -> str:
    """与后端导出接口共用同一份渲染实现。"""

    from backend.app.models.research_report import ResearchReport
    from backend.app.services.research_report import research_report_markdown

    report = ResearchReport.model_validate(advice.get("research_report") or {})
    return research_report_markdown(report, question=question or None)


def render_research_report(advice, *, key, compact=False, question: str = ""):
    report = advice.get("research_report") or {}
    if not report or not report_sections(advice):
        return
    title = report.get("title") or "研究证据对照"
    st.markdown(f"**结构化投资分析报告 · {title}**")
    coverage = report.get("coverage") or {}
    if coverage:
        status = REPORT_STATUS.get(report.get("status"), report.get("status") or "—")
        line = (f"状态：{status} · 引用 {coverage.get('cited', 0)} 条 / 本次可用 {coverage.get('available', 0)} 条"
                f" · 带日期 {coverage.get('dated', 0)} 条 · 覆盖 {len(coverage.get('dimensions') or [])} 个维度")
        st.caption(line)
        missing = coverage.get("missing_dimensions") or []
        if missing:
            st.caption("未覆盖维度：" + "、".join(missing) + "。这些维度没有可引用资料，不作为不支持或中性的结论。")
    findings = report.get("key_findings") or []
    if findings:
        st.markdown("**关键信息**")
        for finding in findings:
            stance = FINDING_STANCES.get(finding.get("stance"), "待核实")
            st.markdown(f"- **[{stance}] {plain_language(finding.get('title') or '')}**："
                        f"{plain_language(finding.get('detail') or '')}")
    st.markdown("**研究证据对照**")
    st.write(plain_language(report.get("summary")))
    st.caption(plain_language(report.get("method")))
    for limitation in report.get("limitations", []):
        st.caption(plain_language(limitation))
    with st.expander("展开支持因素、不利因素与重新评估条件", key=f"research_report_{key}",
                     expanded=not compact, on_change="rerun") as panel:
        if panel.open:
            raw_sections = report.get("sections", [])
            verified = {str(item) for item in advice.get("evidence", [])}
            facts_by_id = {str(fact.get("fact_id", "")): fact for fact in advice_facts(advice)}
            for index, (title_text, lines) in enumerate(report_sections(advice)[1:]):
                with st.container(border=True):
                    st.markdown("**" + plain_language(title_text).replace("*", "") + "**")
                    for line in lines:
                        st.write(plain_language(line))
                    raw = raw_sections[index] if index < len(raw_sections) else {}
                    items = raw.get("evidence", []) or []
                    linked_ids = [str(fid) for item in items for fid in (item.get("fact_ids") or [])]
                    linked = [facts_by_id[fid] for fid in dict.fromkeys(linked_ids)
                              if fid in verified and fid in facts_by_id]
                    urls = [(item.get("observation") or "来源原文", item.get("source_url"))
                            for item in items if isinstance(item.get("source_url"), str)
                            and item["source_url"].startswith("https://")]
                    if linked or urls:
                        with st.expander(f"查看该维度的原始数据与来源（{len(linked)} 条）",
                                         key=f"report_trace_{key}_{index}"):
                            if linked:
                                st.caption("下列资料通过了核验并被本报告引用；展开「投资逻辑链」可逐层查看它们如何支持结论。")
                                st.dataframe(friendly_fact_rows(linked), width="stretch", hide_index=True)
                            for url_index, (label, url) in enumerate(urls):
                                st.link_button(f"打开原文 · {str(label)[:24]}", url,
                                               key=f"report_url_{key}_{index}_{url_index}")
    try:
        markdown = report_markdown(advice, question)
    except Exception:  # 报告结构来自历史记录时可能缺少新字段，缺少就不提供下载
        markdown = ""
    if markdown:
        st.download_button("下载结构化投资分析报告（Markdown）", data=markdown,
                           file_name="问策智投_投资分析报告.md", mime="text/markdown",
                           key=f"report_markdown_{key}", icon=":material/description:",
                           on_click="ignore")
