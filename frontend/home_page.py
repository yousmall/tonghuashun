"""首页：首次使用指引、继续研究与自选概览。"""
from time import monotonic
import streamlit as st
from frontend.presentation import QUICK_ASKS, plain_language, display_value
from frontend.research_board import load_board, section_facts, entity_rows, format_fact


def render_guide():
    steps = [
        ("01 · 填写投资偏好", "打开左侧“投资偏好”，填写资金用途、投资期限、可接受亏损、资金使用需求和投资经历。"),
        ("02 · 评估并确认风险", "在“风险评估”中回答风险问题，生成评估后核对结果并确认投资偏好。情况发生变化时，可到“风险调整”修改并重新确认。"),
        ("03 · 选择研究方向", "打开“投资问答”，选择市场解读、行业分析、个股研究、基金筛选或可转债分析。五个页面分别保留各自的聊天和研究资料。"),
        ("04 · 在底部提出问题", "在页面底部输入名称或代码、关注的时间范围和具体问题，然后发送。系统会按需获取资料、核对观点，并结合您的投资偏好分析。"),
        ("05 · 核对结果并继续追问", "查看结论、风险、资料来源和日期。资料不足或观点不一致时，先核实待确认事项；继续在同一方向追问可沿用该页上下文。"),
        ("06 · 管理自选、持仓与咨询记录", "在“自选研究”添加关注标的、查看走势或比较标的；在“持仓分析”录入比例并检查组合风险。左侧“最近咨询”可恢复历史聊天，“发起咨询”会开启当前方向的新对话。"),
    ]
    for title, description in steps:
        with st.container(border=True):
            st.markdown(f"**{title}**")
            st.write(description)


@st.fragment(run_every=2)
def market_glance(api_base, fetch):
    key = (api_base, "market", None)
    entry = st.session_state.get("research_board_cache", {}).get(key)
    due = entry is not None and monotonic() - entry["loaded_at"] >= 15
    result = load_board(api_base, "market", None, fetch, refresh=due, nonblocking=True)
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
        st.caption("按下面的流程完成投资偏好确认，再选择方向开始研究。")
        render_guide()
        with st.container(horizontal=True):
            st.button("填写投资偏好", type="primary", icon=":material/person_edit:", on_click=go_to, args=("投资偏好",))
            st.button("进入投资问答", icon=":material/chat:", on_click=go_to, args=("投资问答",))
    else:
        st.title("继续您的投资研究")
        st.caption("投资偏好已确认，查看市场、自选或继续上次的问题。")
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
