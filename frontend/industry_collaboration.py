"""行业页的跨方向资料汇总；事实与既有研究观点分别传递。"""
from __future__ import annotations

import json
from typing import Any, Callable, Mapping

import streamlit as st


DIRECTIONS = {"市场解读": "market", "行业分析": "industry", "个股研究": "stock",
              "基金筛选": "fund", "可转债分析": "convertible"}


def collect_research_materials(state: Mapping[str, Any], api_base: str) -> dict[str, Any]:
    """只读当前账号的本次会话资料，不访问其他用户或另一个后端的数据缓存。"""
    target = str(state.get("board_target_industry") or "").strip() or None
    all_facts: dict[str, dict] = {}
    conflicts: set[str] = set()
    materials = []
    rows = []
    active = state.get("active_research_direction", "行业分析")
    sessions = state.get("research_sessions", {})
    cache = state.get("research_board_cache", {})

    for label, code in DIRECTIONS.items():
        # 活跃页的数据可能尚未写回 research_sessions，必须读取当前副本。
        session = state if label == active else sessions.get(label, {})
        session = session or {}
        facts = list(session.get("facts") or [])
        objects = set()
        for key, entry in sorted(cache.items(), key=lambda item: item[1].get("loaded_at", 0), reverse=True):
            if not isinstance(key, tuple) or len(key) != 3 or key[:2] != (api_base, code):
                continue
            if code == "industry" and key[2] != target:
                continue
            if key[2]:
                objects.add(str(key[2]))
            for section in (entry.get("result") or {}).get("sections", []):
                if section.get("status") == "ok":
                    facts.extend(section.get("facts") or [])

        ids = set()
        snapshots = []
        for fact in facts:
            if not isinstance(fact, dict) or not fact.get("fact_id"):
                continue
            fid = fact["fact_id"]
            ids.add(fid)
            if fact.get("entity"):
                objects.add(str(fact["entity"]))
            if fact.get("snapshot_time"):
                snapshots.append(str(fact["snapshot_time"]))
            if fid in all_facts and all_facts[fid] != fact:
                conflicts.add(fid)
            else:
                all_facts[fid] = dict(fact)

        # 既有结论只能当作参考材料，不能伪装成供应商事实重新进入证据包。
        turns = session.get("conversation") or []
        last_turn = next((turn for turn in reversed(turns)
                          if turn.get("role") == "assistant" and turn.get("payload")), {})
        advice = last_turn.get("payload") or session.get("advice") or {}
        conclusion = str(advice.get("conclusion") or "").strip()
        if conclusion:
            review = (advice.get("compliance") or {}).get("status", "待确认")
            summary = f"原研究状态：{review}。既有观点仅供重新核验：{conclusion}"
            materials.append({"source": label, "summary": summary[:1990] + ("（摘要已截取）" if len(summary) > 1990 else ""),
                              "as_of": str(last_turn.get("created_at") or "")[:64] or None})
        rows.append({"研究模块": label, "事实条数": len(ids), "研究摘要": "有（仅供参考）" if conclusion else "暂无",
                     "资料对象": "、".join(sorted(objects))[:160] or "待加载",
                     "最新快照": max(snapshots, default="暂无")})

    trend_entry = state.get("industry_trend_cache", {}).get(f"{api_base}|{target}", {})
    trend = trend_entry.get("result") or {}
    if isinstance(trend, dict) and trend.get("status") == "ok" and trend.get("analysis"):
        analysis = trend["analysis"]
        observation = {key: analysis.get(key) for key in (
            "verdict", "latest_date", "latest_close", "ma5", "ma5_slope_pct", "three_day", "neutralized", "notes")}
        materials.append({"source": "行业走势", "target": target,
                          "summary": ("仅为历史走势观察，不作为投资事实或收益预测：" + json.dumps(
                              {"analysis": observation, "source_url": trend.get("source_url")},
                              ensure_ascii=False, default=str))[:2000],
                          "as_of": str(trend.get("fetched_at") or "")[:64] or None})

    for fid in conflicts:
        all_facts.pop(fid, None)
    facts = list(all_facts.values())
    return {"facts": facts[:4000], "research_materials": materials, "sources": rows,
            "conflicting_ids": len(conflicts), "omitted_facts": max(0, len(facts) - 4000)}


@st.fragment(run_every=2)
def render_industry_collaboration(api_base: str, *, ready: bool, submit: Callable) -> None:
    """显式提交才执行深度协同；沿用聊天任务的进度、会话隔离与历史保存。"""
    bundle = collect_research_materials(st.session_state, api_base)
    target = str(st.session_state.get("board_target_industry") or "").strip() or None
    with st.container(border=True):
        st.subheader("多智能体协同", anchor=False)
        st.write(f"围绕「{target or '全部行业'}」，汇总市场、个股、基金、可转债的已加载资料和最近研究结果。")
        st.caption("市场、行业、证券（个股 / 转债）、基金智能体并行研判，随后统一核验事实与风险；结论保留分歧和待补证事项。")
        with st.expander("本次汇总的信息", expanded=False):
            st.dataframe(bundle["sources"], hide_index=True, width="stretch")
            st.caption("各模块事实将重新核验时效、来源和冲突；既有结论与行业走势仅供参考。其他标的与所选行业的关联须另有证据支持。")
            st.caption("行业走势：" + ("已纳入参考" if any(m["source"] == "行业走势" for m in bundle["research_materials"]) else "尚未加载"))
        if bundle["conflicting_ids"]:
            st.warning(f"发现 {bundle['conflicting_ids']} 个编号对应不同资料，已排除；请刷新相应模块后重试。")
        if bundle["omitted_facts"]:
            st.warning(f"本轮按输入上限纳入 4000 条事实，另有 {bundle['omitted_facts']} 条未纳入。")
        if not ready:
            st.caption("请先完成并确认风险评估，再启动协同分析。")
        busy = "行业分析" in st.session_state.get("analysis_jobs", {})
        invalid_target = bool(target and len(target) > 60)
        if st.button("开始多智能体协同分析", key="industry_collaboration_start", type="primary",
                     icon=":material/groups:", disabled=not ready or busy or invalid_target):
            query = (
                f"行业分析：请围绕{target or '全部行业的比较'}开展多智能体协同研究。"
                "综合已加载的市场、个股、基金和可转债资料，由市场、行业、证券和基金智能体分别研判，"
                "汇总行业景气、估值、资金和风险的支持证据、反面证据、观点分歧与待补证事项。"
                "其他标的只作有依据的关联研究；没有证据时说明无法确认关联。"
                "结论必须重新引用有效事实，既有研究摘要和历史走势用于定位复核线索。"
            )
            submit(api_base, "行业分析", query, collaboration=bundle)
            st.rerun()
        st.caption("使用深度研究处理已有资料，结果和各智能体明细显示在下方对话并保存到历史记录。缺项可先到相应模块加载或刷新，再重新协同分析。")
