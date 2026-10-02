"""首页：首次使用指引、继续研究与自选概览。"""
import streamlit as st
from frontend.presentation import QUICK_ASKS, plain_language, display_value
from frontend.research_board import load_board, section_facts, entity_rows, format_fact
from streamlit.runtime.scriptrunner import get_script_run_ctx


def render_guide():
    steps = [
        ("01 · 完成风险评估", "打开左侧“风险评估”，填写资金用途、投资期限、可接受亏损、资金使用需求和投资经历。"),
        ("02 · 核对并确认结果", "完成问卷后核对评估结果并确认保存。可随时点击“重新评估”修改答案，原有选项会保留。"),
        ("03 · 选择研究方向", "进入“投资问答”，选择市场解读、行业分析、个股研究、基金筛选或可转债分析。各研究方向独立保存咨询记录与研究资料。"),
        ("04 · 在底部提出问题", "在页面底部输入名称或代码、关注的时间范围和具体问题，然后发送。系统会按需获取资料、核对观点，并结合您的投资偏好分析。"),
        ("05 · 复核结果并深化研究", "核对研究结论、风险提示、数据来源与时点。证据不足或观点存在分歧时，应复核待确认事项；同一方向的后续咨询可沿用已有研究上下文。"),
        ("06 · 管理自选、持仓与咨询记录", "在“自选研究”中添加关注标的、查看走势与横向比较；在“持仓分析”中录入权重并评估组合风险。“最近咨询”可恢复历史记录，“发起咨询”可创建当前研究方向的新会话。"),
    ]
    for title, description in steps:
        with st.container(border=True):
            st.markdown(f"**{title}**")
            st.write(description)


def market_glance(api_base, fetch):
    key = (api_base, "market", None)
    entry = st.session_state.get("research_board_cache", {}).get(key)
    polling = key in st.session_state.get("research_board_prefetch", {}) or not entry or entry["result"].get("status") == "loading"
    st.fragment(run_every=1 if polling else 30)(_market_glance)(api_base, fetch, polling)


def _market_glance(api_base, fetch, polling):
    result = load_board(api_base, "market", None, fetch, nonblocking=True)
    loading = result.get("status") == "loading" or bool(result.get("refreshing"))
    context = get_script_run_ctx(suppress_warning=True)
    if bool(loading) != polling and context and getattr(context, "fragment_ids_this_run", None):
        st.rerun()
    rows = entity_rows(section_facts(result, "overview"))
    if not rows:
        overview = next((item for item in result.get("sections", []) if item.get("key") == "overview"), {})
        rows = entity_rows(overview.get("previous_facts") or [])
    if rows:
        with st.container(horizontal=True):
            for row in rows[:4]:
                st.metric(row["name"], format_fact(row["fields"].get("close_price"), index=True),
                    format_fact(row["fields"].get("change")), delta_color="inverse", border=True)
        update_state = "正在获取最新行情 · " if result.get("refreshing") else ""
        st.caption(update_state + "行情仅供浏览 · 日期见研究页 · 获取时间 " + str(result.get("fetched_at") or "未提供")[:19])
    else:
        st.caption("市场概览正在后台更新。" if result.get("status") == "loading" else "市场概览暂未取得，可直接进入研究页提问。")


def render_home(api_base, *, ready, header, go_to, recent, fetch, resume):
    header("主页")
    if not ready:
        st.title("开始使用问策智投")
        st.caption("完成投资偏好评估与确认后，选择研究方向并发起咨询。")
        render_guide()
        with st.container(horizontal=True):
            st.button("进行风险评估", type="primary", icon=":material/person_edit:", on_click=go_to, args=("风险评估",))
            st.button("进入投资问答", icon=":material/chat:", on_click=go_to, args=("投资问答",))
    else:
        st.title("继续您的投资研究")
        st.caption("投资偏好已确认，可查看市场概览、自选标的或继续既有研究。")
        with st.container(horizontal=True):
            st.button("继续上次研究", type="primary", icon=":material/chat:", on_click=resume, args=(api_base,))
            st.button("查看我的自选", icon=":material/star:", on_click=go_to, args=("自选研究",))
            st.button("检查持仓风险", icon=":material/pie_chart:", on_click=go_to, args=("持仓分析",))
        with st.container(horizontal=True):
            st.metric("关注标的", str(len(st.session_state.get("watchlist", []))) if st.session_state.get("watchlist_loaded") or st.session_state.get("watchlist") else "未取得", border=True)
            st.metric("研究方向", st.session_state.get("active_research_direction", QUICK_ASKS[0]), border=True)
            st.metric("投资风格", display_value("risk_level", st.session_state.profile.get("risk_level")), border=True)
        advice = st.session_state.get("advice") or {}
        if advice.get("compliance", {}).get("status") in {"REVIEW", "BLOCK"}:
            st.warning("上次研究仍有待核实事项：" + plain_language(advice.get("risk_conclusion") or advice.get("conclusion")))
        st.subheader("市场概览")
        market_glance(api_base, fetch)
        if recent:
            with st.expander("最近咨询 · 可在左侧恢复", expanded=True):
                for item in recent[:3]:
                    st.write("• " + str(item.get("title") or "未命名咨询"))
        with st.expander("使用流程与操作说明", key="home_guide", on_change="rerun", icon=":material/help:") as guide:
            if guide.open:
                render_guide()
    st.caption("分析仅供参考，不保证收益。投资前，请结合自己的情况判断。")
