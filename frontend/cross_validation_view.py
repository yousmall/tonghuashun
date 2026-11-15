"""展示后端记录的投资逻辑核验与分歧处置，不在前端重新裁决。"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import streamlit as st


AGENTS = {"market": "市场环境", "industry": "行业分析", "security": "证券研究（个股 / 转债）",
          "fund": "基金分析", "portfolio": "持仓分析"}
STATUSES = {"PASS": "已通过", "REVIEW": "待复核", "BLOCK": "已拦截"}
TASKS = {"completed": "已完成", "degraded": "已降级", "failed": "未完成", "unknown": "资料不足",
         "pending": "等待执行", "running": "执行中", "skipped": "已跳过"}
# 这里解释处理原则；实际是否执行与最终状态分别读取节点记录和审核记录。
POLICIES = {
    "INTERNAL_VALUE_CONFLICT": ("同源数据冲突", "保留冲突与待复核状态", "核对同一对象、统计期、单位及原文，刷新后重新分析"),
    "SOURCE_VALUE_CONFLICT": ("跨来源数据冲突", "保留不同来源记录，待复核", "逐项对照原始发布材料，不能按来源数量投票"),
    "AGENT_SCORE_DISPERSION": ("评分分歧", "保留各维度判断并要求复核", "区分研究维度与时间范围，再核对支持、反面证据"),
    "SEMANTIC_AGENT_CONFLICT": ("观点实质矛盾", "保留矛盾观点并要求复核", "核对具体前提、期限和证据；补证后重新分析"),
    "EVIDENCE_REFERENCE_REJECTED": ("引用未通过核验", "剔除无效引用并降级相关判断", "刷新有效资料，补齐原始引用与派生链后重新分析"),
    "NO_VERIFIED_EVIDENCE": ("缺少有效证据", "不形成确定判断", "先加载有来源、统计期与时间戳的资料"),
    "COMPLETED_WITHOUT_EVIDENCE": ("观点缺少引用", "标记为待复核", "为关键判断补齐可核验的原始依据"),
    "INCOMPLETE_ANALYSIS": ("分析资料不完整", "保留有限判断与资料缺口", "补齐该维度的必要指标后重新核验"),
    "AGENT_DATA_REQUIRED": ("仍需补充资料", "保留待补证事项", "取得所列必要资料后重新分析"),
}


def validation_overview(advice: dict[str, Any]) -> dict[str, Any]:
    cross = advice.get("cross_validation") or {}
    issues = [item for item in cross.get("issues") or [] if isinstance(item, dict)]
    count = cross.get("issue_count")
    count = max(count, len(issues)) if type(count) is int else len(issues)
    return {"cross": STATUSES.get(cross.get("status"), "未记录"),
            "risk": STATUSES.get((advice.get("compliance") or {}).get("status"), "未记录"),
            "issues": issues, "count": count}


def _names(ids) -> str:
    return "、".join(dict.fromkeys(AGENTS.get(item, "其他分析维度") for item in ids or [])) or "未记录"


def disagreement_agents(advice: dict[str, Any]) -> list[str]:
    cross = advice.get("cross_validation") or {}
    involved = set(cross.get("dissenting_agents") or [])
    for issue in validation_overview(advice)["issues"]:
        if issue.get("code") in {"AGENT_SCORE_DISPERSION", "SEMANTIC_AGENT_CONFLICT"}:
            involved.update(issue.get("agent_ids") or [])
    return sorted(involved)


def disagreement_rows(advice: dict[str, Any]) -> list[dict[str, str]]:
    from frontend.presentation import plain_language
    rows = []
    for number, issue in enumerate(validation_overview(advice)["issues"], 1):
        kind, policy, next_step = POLICIES.get(issue.get("code"), (
            "其他待核实事项", "按原始记录保留，处理方式待确认", "对照原始资料和审核说明进一步复核"))
        rows.append({"序号": str(number), "问题类型": kind,
                     "重要程度": {"info": "提示", "warning": "需复核", "critical": "重点核实"}.get(issue.get("severity"), "未记录"),
                     "涉及维度": _names(issue.get("agent_ids")),
                     "本次记录": plain_language(issue.get("message")) or "未保存问题说明",
                     "处理原则": policy, "后续动作": next_step})
    return rows


def conflict_evidence_rows(advice: dict[str, Any]) -> tuple[list[dict], int]:
    """冲突材料独立于认可证据展示，剔除资料也能复核，缺失历史不补造。"""
    from frontend.presentation import advice_facts, fact_label, fact_time
    facts = {str(f["fact_id"]): f for f in advice_facts(advice) if f.get("fact_id")}
    issues = validation_overview(advice)["issues"]
    for issue in issues:
        for fact in issue.get("evidence_details") or []:
            if isinstance(fact, dict) and fact.get("fact_id"):
                facts.setdefault(str(fact["fact_id"]), fact)
    rows = []
    missing = set()
    for number, issue in enumerate(issues, 1):
        for fid in dict.fromkeys(str(value) for value in issue.get("fact_ids") or []):
            fact = facts.get(fid)
            if not fact:
                missing.add(fid)
                continue
            url = fact.get("source_url")
            try:
                parsed = urlsplit(url or "")
                url = url if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password else None
            except ValueError:
                url = None
            rows.append({"问题序号": str(number), "资料编号": fid, "对象": fact.get("entity") or "未记录",
                         "指标": fact_label(str(fact.get("field") or "")), "记录数值": str(fact["value"]) if fact.get("value") is not None else "未记录",
                         "单位": fact.get("unit") or "未记录", "统计期": fact.get("period") or "未记录",
                         "获取时间": fact_time(fact.get("snapshot_time")),
                         "来源": fact.get("source_id") or "未记录", "原文": url})
    return rows, len(missing)


def render_validation_panel(advice: dict[str, Any], *, key: str) -> None:
    summary = validation_overview(advice)
    st.caption(f"交叉验证：{summary['cross']} · 风险检查：{summary['risk']} · 核验问题记录：{summary['count']} 项")
    with st.expander("交叉验证与分歧处理", key=f"cross_validation_{key}", on_change="rerun",
                     icon=":material/compare_arrows:") as panel:
        if panel.open:
            render_validation_details(advice)


def render_validation_details(advice: dict[str, Any]) -> None:
    from frontend.presentation import plain_language
    summary = validation_overview(advice)
    cross = advice.get("cross_validation") or {}
    compliance = advice.get("compliance") or {}
    nodes = {node.get("agent_id"): node for node in (advice.get("task_plan") or {}).get("nodes") or []}
    st.markdown("**本次实际核验结果**")
    with st.container(horizontal=True):
        st.badge(f"交叉验证：{summary['cross']}", color={"PASS": "green", "REVIEW": "orange", "BLOCK": "red"}.get(cross.get("status"), "gray"))
        st.badge(f"风险检查：{summary['risk']}", color={"PASS": "green", "REVIEW": "orange", "BLOCK": "red"}.get(compliance.get("status"), "gray"))
    if compliance.get("status") == "BLOCK":
        st.error("风险检查已拦截，本次未形成投资建议；以下保留核验与分歧记录供复核。")
    elif cross.get("status") == "PASS":
        st.write("本次交叉验证已通过，相关提示仍保留在下方记录中。" if summary["count"] else "本次检查未发现需单独提示的冲突；不代表投资判断必然正确。")
    elif summary["count"]:
        st.info("以下问题为本次返回的待核实记录；保留分歧，不能视为已经解决。")
    else:
        st.write("本次未提供具体问题记录，交叉验证状态仍以原始记录为准。")
    if compliance.get("reason"):
        st.caption("审核说明：" + plain_language(compliance["reason"]))

    involved = disagreement_agents(advice)
    has_disagreement = any(issue.get("code") in {"AGENT_SCORE_DISPERSION", "SEMANTIC_AGENT_CONFLICT"} for issue in summary["issues"])
    disagreement_note = (f"记录涉及 {len(involved)} 个分歧维度" if involved else
                         "有分歧，未保存涉及维度" if has_disagreement else
                         "未列出分歧维度" if "dissenting_agents" in cross else "未保存分歧维度名单")
    st.markdown("**核验机制与本次执行记录**")
    st.dataframe([
        {"环节": "引用与时效", "核对内容": "检查来源、有效期与派生引用链，剔除无效引用", "本次记录": TASKS.get(nodes.get("fact_verifier", {}).get("status"), "未记录执行状态")},
        {"环节": "数据交叉验证", "核对内容": "同一对象、指标和统计期的同源 / 跨来源记录是否一致", "本次记录": summary["cross"]},
        {"环节": "观点比较", "核对内容": "比较规则评分与观点前提，区分研究维度、期限差异和实质矛盾", "本次记录": disagreement_note},
        {"环节": "风险闸门", "核对内容": "保留待复核结论；硬性风险拦截时停止提供建议", "本次记录": summary["risk"]},
    ], hide_index=True, width="stretch")
    st.caption("机制说明与执行记录分别展示；未记录的环节不推断为已执行。多个智能体引用同一份数据、同源转载或派生评分，都不算独立来源验证。")

    rows = disagreement_rows(advice)
    if rows:
        st.markdown("**逐项分歧与处理**")
        st.dataframe(rows, hide_index=True, width="stretch")
        st.caption("处理原则解释系统如何处置这一类问题；实际完成情况以执行状态、引用剔除记录和最终审核状态为准。")
        if summary["count"] > len(rows):
            st.caption(f"当前仅保留 {len(rows)} / {summary['count']} 项问题明细，其余明细需重新分析后查看。")
    if "supporting_agents" in cross:
        st.caption("评分接近综合参考值的维度：" + (_names(cross["supporting_agents"]) if cross["supporting_agents"] else "未列出"))
    if involved:
        st.caption("记录涉及分歧、需重点复核的维度：" + _names(involved))

    if compliance.get("status") != "BLOCK" and advice.get("agent_results"):
        st.markdown("**各维度观点与引用**")
        approved = set(advice.get("evidence") or [])
        st.dataframe([{"分析维度": AGENTS.get(item.get("agent_id"), "其他分析维度"),
                       "本次观点": plain_language(item.get("opinion")) or "未保存观点",
                       "执行状态": TASKS.get(item.get("status"), "未记录"),
                       "已认可的引用数": len(set(item.get("facts_used") or []) & approved)}
                      for item in advice["agent_results"]], hide_index=True, width="stretch")
    rejected = [{"分析维度": AGENTS.get(item.get("agent_id"), "其他分析维度"),
                 "已剔除引用数": item["details"]["rejected_reference_count"],
                 "当前状态": TASKS.get(item.get("status"), "未记录")}
                for item in advice.get("agent_results") or []
                if type((item.get("details") or {}).get("rejected_reference_count")) is int
                and item["details"]["rejected_reference_count"] > 0]
    if rejected:
        st.markdown("**已记录的引用剔除与降级**")
        st.dataframe(rejected, hide_index=True, width="stretch")
    evidence, missing = conflict_evidence_rows(advice)
    if evidence:
        st.markdown("**相关原始记录对照**")
        st.caption("以下是用于复核的材料，包括冲突或已被排除的记录，不代表它们已获认可。")
        st.dataframe(evidence, hide_index=True, width="stretch",
                     column_config={"原文": st.column_config.LinkColumn(display_text="查看原文")})
    if missing:
        st.caption(f"有 {missing} 条相关原始记录未保存或未加载，无法在此还原数值，请补充资料后重新核验。")
    acquisition = advice.get("data_acquisition") or {}
    if (acquisition.get("recovery_rounds") or 0) > 0:
        st.caption(f"本次记录了 {acquisition['recovery_rounds']} 轮补查。" + (
            "补查后已重新分析，最终是否通过仍以上方核验状态为准。" if acquisition.get("recovery_reanalyzed") else "记录未标明补查后重新分析。"))
    with st.expander("评分与分歧判定规则"):
        st.write("规则评分按各维度可信度加权形成综合参考值；偏离参考值达到 25 分的维度会被标记。至少 3 个维度有评分、最高与最低相差达到 35 分时，触发评分分歧复核。")
        st.write("语义复核进一步检查无法用研究维度或期限差异解释的实质矛盾。评分接近不等于共同看多，也不代表多来源验证；前端展示后端记录，不重新投票或裁决。")
    st.caption("需要人工处理时，在「交叉检验与人工复核工作单」记录原文、口径和意见；补证后重新分析。人工意见不会直接更改系统审核状态。")
