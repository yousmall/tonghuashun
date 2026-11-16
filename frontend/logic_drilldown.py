"""投资逻辑链逐级展开与数据溯源。

层级：① 综合结论 / 关键信息 → ② 分项观点 → ③ 数据点 → ④ 原始来源（含派生父链）。
只展示最终建议包认可的 evidence，避免展开视图把已剔除的资料重新挂回结论；
界面沿用 presentation 的业务化字段，不向用户暴露内部 fact_id。
"""
from __future__ import annotations

from typing import Any

import streamlit as st

from frontend.presentation import (
    TOPIC_LABELS,
    advice_facts,
    friendly_fact_rows,
    plain_language,
    render_points,
    render_source_trace,
)

MAX_FACTS_PER_NODE = 8


def has_traceable_evidence(advice: dict[str, Any] | None) -> bool:
    """判断一条回答是否真的能逐级下钻。

    资料不足、被风险检查拦截、或只有结论没有认可引用的回答，渲染出来只会是四层
    空态；调用方应先据此筛选，或改用演示数据展示机制。
    """

    if not isinstance(advice, dict):
        return False
    if (advice.get("compliance") or {}).get("status") == "BLOCK":
        return False
    used = {str(item) for item in advice.get("evidence", [])}
    facts = {str(fact.get("fact_id")) for fact in advice_facts(advice) if fact.get("fact_id")}
    if not used & facts:
        return False
    has_views = bool(advice.get("agent_results") or
                     (advice.get("research_report") or {}).get("key_findings"))
    return has_views


def _fact_maps(advice: dict[str, Any]):
    all_facts = {str(fact.get("fact_id", "")): fact for fact in advice_facts(advice)}
    verified = {key: all_facts[key] for key in
                (str(item) for item in advice.get("evidence", [])) if key in all_facts}
    return verified, all_facts


def _fact_label(fact: dict[str, Any]) -> str:
    row = friendly_fact_rows([fact])[0]
    return f"{row['对象']} · {row['指标']}：{row['内容 / 数值']}（{row['来源']}）"


def _public_url(value: Any) -> str | None:
    return value if isinstance(value, str) and value.startswith("https://") else None


def _render_source(fact: dict[str, Any], all_facts: dict[str, dict], *, key: str) -> None:
    st.markdown("**④ 原始来源**")
    st.dataframe([friendly_fact_rows([fact])[0]], width="stretch", hide_index=True)
    url = _public_url(fact.get("source_url"))
    if url:
        st.link_button("打开原文", url, key=f"{key}_url")
    else:
        st.caption("该资料没有可打开的公开原文地址；来源、数据时间与报告期见表内记录。")
    if fact.get("derivation_rule"):
        st.caption("计算依据：" + str(fact["derivation_rule"]))
    parents = [all_facts[str(item)] for item in (fact.get("derived_from") or [])
               if str(item) in all_facts]
    if parents:
        with st.expander(f"上游原始资料（{len(parents)} 条）", key=f"{key}_parents"):
            st.caption("这条数据由下列原始资料计算得到，可继续回溯到最初来源。")
            st.dataframe(friendly_fact_rows(parents), width="stretch", hide_index=True)
            for index, parent in enumerate(parents):
                parent_url = _public_url(parent.get("source_url"))
                if parent_url:
                    st.link_button(f"打开上游原文 · {str(parent.get('entity', ''))[:20]}",
                                   parent_url, key=f"{key}_parent_{index}")


def _render_facts(facts: list[dict[str, Any]], all_facts: dict[str, dict], *, key: str) -> None:
    if not facts:
        st.caption("这一层没有通过核验的数据引用，只能作为有限参考。")
        return
    st.markdown("**③ 数据点**（逐条展开可查看原始来源）")
    for index, fact in enumerate(facts[:MAX_FACTS_PER_NODE], 1):
        item_key = f"{key}_fact_{index}"
        with st.expander(f"{index}. {_fact_label(fact)}", key=item_key):
            _render_source(fact, all_facts, key=item_key)
    if len(facts) > MAX_FACTS_PER_NODE:
        st.caption(f"还有 {len(facts) - MAX_FACTS_PER_NODE} 条数据点，可在下方「完整数据溯源」查看。")


def _render_finding(finding: dict[str, Any], verified: dict[str, dict],
                    all_facts: dict[str, dict], *, key: str) -> None:
    stance = {"favorable": "有利", "caution": "不利", "neutral": "中性/待核实"}.get(
        finding.get("stance"), "待核实")
    linked = [verified[str(item)] for item in finding.get("fact_ids", []) if str(item) in verified]
    title = plain_language(finding.get("title") or "")[:60]
    with st.expander(f"[{stance}] {title} · {len(linked)} 条依据", key=key):
        st.write(plain_language(finding.get("detail") or "") or "—")
        _render_facts(linked, all_facts, key=key)


def _render_agent(index: int, result: dict[str, Any], verified: dict[str, dict],
                  all_facts: dict[str, dict], *, key: str) -> None:
    topic = TOPIC_LABELS.get(result.get("agent_id"), "相关分析")
    linked = [verified[str(item)] for item in result.get("facts_used", []) if str(item) in verified]
    with st.expander(f"{index:02d} · {topic} · {len(linked)} 条数据点",
                     icon=":material/account_tree:", key=key):
        confidence = result.get("confidence")
        if isinstance(confidence, (int, float)) and 0 <= confidence <= 1:
            st.caption(f"该分项判断的可信度：{confidence:.0%}")
        st.write(plain_language(result.get("opinion")) or "资料不足，暂未形成结论。")
        render_points("判断把握受哪些因素影响", result.get("confidence_reasons", []), visible=99)
        render_points("相关风险", result.get("risk_flags", []), visible=99)
        render_points("哪些变化需要重新判断", result.get("invalidation_conditions", []), visible=99)
        _render_facts(linked, all_facts, key=key)


def render_logic_chain_guide() -> None:
    """四层机制说明：问答页入口与独立页面共用。"""

    st.caption("每条结论都可沿「结论 → 分项观点 → 数据点 → 原始来源」逐层点开；"
               "只展示通过核验、且被最终结论引用的资料。")
    st.markdown(
        "**① 综合结论与关键信息** → **② 分项观点**（判断、可信度、风险条件）→ "
        "**③ 数据点**（数值、数据时间、报告期）→ **④ 原始来源**（来源、抓取时间、"
        "原文链接；派生指标可继续回溯上游资料与计算规则）。"
    )
    st.caption("引用编号由系统内部维护并按派生关系核验；过期、低质量或断链的资料不会挂回结论。")


def render_logic_chain_entry(advice: dict[str, Any] | None = None, *,
                             key: str = "logic_entry", expanded: bool = False) -> None:
    """问答页常驻入口：默认收起，点开后无分析结果时说明机制、有结果时逐层下钻。

    不依赖风险测评状态，也不要求先展开回答详情，避免用户在空白页找不到溯源能力。
    """
    with st.expander("投资逻辑链 · 逐级展开与数据溯源", icon=":material/account_tree:",
                     key=key, expanded=expanded, on_change="rerun") as panel:
        if not panel.open:
            return
        render_logic_chain_guide()
        if not advice:
            st.info("完成风险测评并发起一次咨询后，这里会直接展开本次回答的四层逻辑链；"
                    "页面顶部的同一面板在任何页面都能查看。", icon=":material/lightbulb:")
            return
        st.markdown("**本次回答的逻辑链**")
        render_logic_drilldown(advice, key=f"{key}_drill")


def render_logic_drilldown(advice: dict[str, Any], *, key: str = "logic") -> None:
    """把已有证据图渲染成可逐层点开的逻辑链；结论被拦截时不提供下钻。"""

    if (advice.get("compliance") or {}).get("status") == "BLOCK":
        st.caption("风险检查已拦截本次结论，暂不提供逻辑链下钻。")
        return
    if not has_traceable_evidence(advice):
        st.info("这条回答没有通过核验的数据引用，暂时无法逐级下钻；"
                "请补充标的资料后重新分析，或查看页面上的演示逻辑链。",
                icon=":material/lightbulb:")
        return
    st.caption("逐级展开：① 结论 → ② 分项观点 → ③ 数据点 → ④ 原始来源。"
               "仅显示通过核验并被最终结论引用的资料。")
    verified, all_facts = _fact_maps(advice)
    findings = (advice.get("research_report") or {}).get("key_findings") or []
    with st.expander("① 综合结论与关键信息", icon=":material/lightbulb:", key=f"{key}_conclusion"):
        st.write(plain_language(advice.get("conclusion")) or "现有资料不足，暂时无法作出判断。")
        if findings:
            st.markdown("**关键信息**（逐条展开可查看它引用的数据点）")
            for index, finding in enumerate(findings, 1):
                _render_finding(finding, verified, all_facts, key=f"{key}_finding_{index}")
        else:
            st.caption("本次没有单独提炼的关键信息，可直接从下方分项观点下钻。")
    results = advice.get("agent_results") or []
    st.markdown("**② 分项观点**（逐项展开可查看判断依据与风险条件）")
    if not results:
        st.caption("本次没有形成可展开的分项分析。")
    for index, result in enumerate(results, 1):
        _render_agent(index, result, verified, all_facts, key=f"{key}_agent_{index}")
    st.markdown("**完整数据溯源**")
    render_source_trace(advice, collapsed=True, key=f"{key}_sources")
