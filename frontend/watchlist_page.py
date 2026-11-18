"""自选列表、单标的详情与结构化对比页面。"""
import re
from datetime import date
import altair as alt
import pandas as pd
import streamlit as st
from frontend.charts import render_chart
from streamlit.runtime.scriptrunner import get_script_run_ctx
from frontend.answer_report import chart_groups
from frontend.comparison import comparison_matrix
from frontend.decision_view import render_decision
from frontend.financial_view import period_info
from frontend.presentation import WATCHLIST_RESEARCH_PREFIX, RESEARCH_PREFIX, fact_time, render_empty_state
from frontend.research_board import entity_rows, format_fact, data_period
from frontend.snapshots import poll_snapshot, clear_snapshot
from frontend.conversation_logic import render_single_answer_chain
from frontend.result_views import render_advice, render_answer_charts
from backend.app.watchlist_policy import WATCHLIST_CAPACITY, WATCHLIST_PAGE_SIZES


def quote_rows(items, snapshot):
    snapshots = {item.get("watchlist_id"): item for item in snapshot.get("items", [])}
    rows = []
    for item in items:
        quote = snapshots.get(item["id"], {})
        facts = [fact for fact in quote.get("facts", []) if not period_info(fact.get("period")) or period_info(fact["period"])[1] <= date.today()]
        entities = entity_rows(facts)
        fields = entities[0]["fields"] if entities else {}
        primary = fields.get("fund_nav") if item.get("asset_type") == "基金" else fields.get("close_price")
        money_quote = fields.get("yield_7d") or fields.get("income_per_10000")
        # 名称优先用数据源核实结果，其次用添加自选时保存的名称，最后才回退到用户输入。
        name = quote.get("display_name") or item.get("name") or (entities[0]["name"] if entities else None) or item["target"]
        code = quote.get("entity_code") or (
            item["target"] if re.fullmatch(r"\d{5,6}(\.\w{2})?", str(item["target"]).strip()) else "未提供")
        row = {"名称": name, "代码": code, "类型": item.get("asset_type", "股票"), "标的": item["target"],
            "价格 / 净值": format_fact(primary), "涨跌幅": format_fact(fields.get("change") or fields.get("nav_change")),
            "数据日期": data_period(primary or money_quote), "状态": "更新中" if quote.get("status") == "loading" else "可查看" if primary or money_quote else "暂无行情"}
        if money_quote:
            row.update({"七日年化":format_fact(fields.get("yield_7d")),
                        "每万份收益":format_fact(fields.get("income_per_10000")),
                        "万份收益日期":data_period(fields.get("income_per_10000"))})
        rows.append(row)
    return rows


@st.fragment(run_every=1)
def _wait_quotes(signature, fetch):
    result = poll_snapshot("watchlist_quotes", signature, fetch)
    if result.get("status") != "loading" and not result.get("refreshing"):
        context = get_script_run_ctx()
        if context and getattr(context, "fragment_ids_this_run", None):
            st.rerun()
        return
    st.caption("正在取得自选行情，其他操作可继续使用。")


def _item_label(item: dict) -> str:
    """展示“名称 · 代码”，名称缺失时回退到用户输入。"""

    name = item.get("name") or item.get("target") or "—"
    target = str(item.get("target") or "")
    if target and target != name and re.fullmatch(r"\d{5,6}(\.\w{2})?", target.strip()):
        return f"{name} · {target}"
    return str(name)


def quote_table(api_base, items, factory):
    signature = tuple((item["id"], item["target"], item.get("asset_type")) for item in items)
    refresh = st.button("刷新自选行情", icon=":material/refresh:", key="watchlist_quote_refresh")
    fetch = factory(api_base, "/data/watchlist-quotes", {"wait": True, "ids": [int(item["id"]) for item in items]})
    result = poll_snapshot("watchlist_quotes", signature, fetch, refresh=refresh)
    if result.get("status") == "loading" or result.get("refreshing"):
        st.caption("行情正在后台更新，可先选择标的或发起研究。刷新期间显示的数据请以标注日期为准。")
    st.dataframe(quote_rows(items, result), hide_index=True, width="stretch", key="watchlist_quotes_table")
    st.caption("同花顺问财 · 点击列标题可排序 · 日期未提供的数据不能视为最新行情。")
    if result.get("status") == "loading" or result.get("refreshing"):
        _wait_quotes(signature, fetch)


def _reset_watchlist_page():
    st.session_state.watchlist_page_number = 1


def _move_watchlist_page(offset):
    st.session_state.watchlist_page_number += offset


def watchlist_page(items):
    """搜索后的列表分页；筛选/页长变化回首页，删除末页时自动回到有效页。"""
    page_size = st.selectbox("每页显示", WATCHLIST_PAGE_SIZES, index=1,
        format_func=lambda value: f"{value}条", key="watchlist_page_size",
        on_change=_reset_watchlist_page, persist_state="session")
    page_count = max(1, (len(items)+page_size-1)//page_size)
    current = min(page_count, max(1, st.session_state.get("watchlist_page_number", 1)))
    st.session_state.watchlist_page_number = current
    with st.container(horizontal=True, vertical_alignment="bottom"):
        st.button("上一页", key="watchlist_page_previous", disabled=current == 1,
                  on_click=_move_watchlist_page, args=(-1,))
        st.selectbox("跳转页码", range(1, page_count+1), key="watchlist_page_number",
                     format_func=lambda value: f"第{value}页", persist_state="session")
        st.button("下一页", key="watchlist_page_next", disabled=current == page_count,
                  on_click=_move_watchlist_page, args=(1,))
    start = (current-1)*page_size
    st.caption(f"共{len(items)}个标的 · 第{current}/{page_count}页 · 显示第{start+1}–{min(start+page_size,len(items))}个")
    return items[start:start+page_size]


def comparison_result(api_base, actions):
    if not st.session_state.get("watchlist_comparison_request"):
        return
    value = st.session_state.get("snapshot_comparison_value")
    polling = value is None or value.get("status") == "loading" or "snapshot_comparison_task" in st.session_state
    st.session_state.comparison_polling = polling
    st.fragment(run_every=1 if polling else None)(_comparison_result)(api_base, actions)


def _comparison_result(api_base, actions):
    request = st.session_state.get("watchlist_comparison_request")
    if not request:
        return
    current_ids = {item["id"] for item in st.session_state.get("watchlist", [])}
    if not set(request["ids"]) <= current_ids:
        clear_snapshot("comparison")
        st.session_state.pop("watchlist_comparison_request", None)
        st.session_state.pop("watchlist_comparison_advice", None)
        st.info("对比标的已被移除，请重新选择。")
        return
    refresh = st.button("刷新这组对比", key="comparison_refresh", icon=":material/refresh:")
    signature = (request["asset_type"], tuple(request["targets"]))
    payload = {key: request[key] for key in ["asset_type", "targets"]}
    result = poll_snapshot("comparison", signature, actions["factory"](api_base, "/data/compare", payload), refresh=refresh)
    if refresh:
        st.session_state.pop("watchlist_comparison_advice", None)
    loading = result.get("status") == "loading" or result.get("refreshing")
    context = get_script_run_ctx()
    if bool(loading) != st.session_state.get("comparison_polling", False) and context and getattr(context, "fragment_ids_this_run", None):
        st.rerun()
    if result.get("status") == "loading":
        st.info("正在逐一核对对比标的，您可以继续查看自选。")
        return
    if result.get("refreshing"):
        st.caption("对比正在更新，当前显示更新前资料；更新完成后可重新解读。")
    items = result.get("items", [])
    rows = comparison_matrix(items)
    for item in items:
        if item.get("message"):
            st.caption(item["target"] + "：" + item["message"])
    if not rows:
        st.warning("未取得足够的可核对指标，暂不生成比较结论。")
        return
    st.dataframe(rows, hide_index=True, width="stretch", key="comparison_matrix")
    st.caption("优先选择各标的共有的最新期间；期间、单位不一致或缺失的指标明确标为不可直接比较。")
    render_decision(result.get("decision"), key="watchlist")
    eligible = [row for row in rows if row["可比性"] == "同期间、同单位"]
    facts = [fact for item in items for fact in item.get("facts", [])]
    chart_advice = {"facts": facts, "evidence": [fact.get("fact_id") for fact in facts]}
    # 图形只在全部标的拥有同口径数值时出现，缺失值不按零填充。
    groups = [group for group in chart_groups(chart_advice) if group["kind"] == "bar" and len(group["rows"]) == len(items)]
    if groups:
        options = {group["title"] + " · " + group["period"]: group for group in groups}
        choice = st.selectbox("查看对比图", list(options), key="comparison_chart_choice")
        group = options[choice]
        frame = pd.DataFrame(group["rows"])
        color = alt.condition(alt.datum.value >= 0, alt.value("#ad384e"), alt.value("#18794e")) if "涨跌" in group["title"] else alt.value("#246b89")
        render_chart(alt.Chart(frame).mark_bar(cornerRadiusEnd=4).encode(
            color=color,
            x=alt.X("value:Q", title=group["unit"]), y=alt.Y("name:N", title=None),
            tooltip=[alt.Tooltip("name:N", title="标的"), alt.Tooltip("value:Q", title="数值", format=",.4f")]).properties(height=240), width="stretch", key="comparison_chart")
    if not eligible:
        st.warning("目前没有所有标的共有的同期间指标，需补齐资料后再比较。")
    targets = [item.get("entity_code") or item.get("entity") or item["target"] for item in items]
    distinct_targets = len({target.casefold() for target in targets}) == len(targets)
    if st.button("解读这组对比", type="primary", key="interpret_comparison",
                 disabled=not eligible or not distinct_targets or result.get("refreshing", False) or not actions["ready"]()):
        prefix = WATCHLIST_RESEARCH_PREFIX[request["asset_type"]]
        direction = next(kind for kind, value in RESEARCH_PREFIX.items() if value == prefix)
        actions["activate"](direction)
        st.session_state.research_direction = direction
        for fact in facts:
            actions["add_fact"](fact)
        names = "、".join(item["target"] + ("（" + item["entity_code"] + "）" if item.get("entity_code") else "") for item in items)
        advice = actions["analysis"](
            api_base, f"{prefix}请横向比较{names}，解读同期间、同单位指标的主要差异与风险",
            comparison_targets=[item["target"] for item in items],
            comparison_facts=list({fact["fact_id"]: fact for fact in facts if fact.get("fact_id")}.values()),
        )
        if advice:
            st.session_state.watchlist_comparison_advice = advice
            actions["save"]()
    if st.session_state.get("watchlist_comparison_advice"):
        render_advice(st.session_state.watchlist_comparison_advice, export_key="watchlist_comparison", question="、".join(request["targets"]) + "横向对比")
        render_single_answer_chain(st.session_state.watchlist_comparison_advice, question="、".join(request["targets"]) + "横向对比", key="watchlist_comparison")
        st.button("进入本方向继续追问", on_click=actions["go_to"], args=("投资问答",), key="continue_comparison")


def render_watchlist(api_base, actions):
    st.title("自选研究")
    actions["header"]("自选 · 关注与对比研究")
    st.caption("查看关注标的的行情、走势与风险，或选择同类型标的进行对比。")
    st.caption(f"自选最多保存{WATCHLIST_CAPACITY}个标的，支持30、50及更多标的分页查看。")
    items = st.session_state.get("watchlist", [])
    with st.expander("添加自选标的", expanded=not items, icon=":material/star:"):
        with st.form("watchlist_add", border=False):
            with st.container(horizontal=True, vertical_alignment="bottom"):
                asset_type = st.selectbox("标的类型", list(WATCHLIST_RESEARCH_PREFIX), key="watchlist_asset_type")
                target = st.text_input("名称或代码", placeholder="例如：贵州茅台 / 600519", max_chars=60, key="watchlist_target")
                submitted = st.form_submit_button("加入自选", type="primary", icon=":material/star:")
        if submitted:
            if not target.strip():
                st.warning("请输入股票、基金、行业、可转债或指数的名称或代码。")
            else:
                result = actions["api"](api_base, "POST", "/watchlist", {"target": target.strip(), "asset_type": asset_type})
                if isinstance(result, dict) and result.get("id") is not None:
                    st.session_state.watchlist = [result, *[item for item in items if item.get("id") != result["id"]]]
                    _reset_watchlist_page()
                    st.toast("已加入自选", icon=":material/check_circle:")
                    items = st.session_state.watchlist
    if not items:
        render_empty_state("暂无自选标的", "添加关注标的后，可查看行情、发起研究或开展横向比较。", ":material/star:")
        return
    st.subheader(f"我的自选 · {len(items)}")
    with st.container(horizontal=True):
        search = st.text_input("搜索自选", placeholder="按名称或代码查找", icon=":material/search:",
                               key="watchlist_search", on_change=_reset_watchlist_page)
        asset_filter = st.selectbox("筛选类型", ["全部", *dict.fromkeys(item.get("asset_type", "股票") for item in items)],
                                    key="watchlist_filter", on_change=_reset_watchlist_page)
    visible = [item for item in items if search.strip().casefold() in
               (str(item.get("target", "")) + " " + str(item.get("name") or "")).casefold()
               and (asset_filter == "全部" or item.get("asset_type", "股票") == asset_filter)]
    if not visible:
        st.info("没有找到匹配的自选标的，可以调整搜索词或类型。")
    else:
        visible = watchlist_page(visible)
        quote_table(api_base, visible, actions["factory"])
        lookup = {int(item["id"]): item for item in visible}
        previous = st.session_state.get("watchlist_focus")
        if previous not in lookup:
            st.session_state.watchlist_focus = next(iter(lookup))
        focused_id = st.selectbox("查看标的详情", list(lookup),
                                  format_func=lambda item_id: _item_label(lookup[item_id]), key="watchlist_focus")
        item = lookup[focused_id]
        with st.container(border=True):
            st.markdown("**" + _item_label(item) + "** · " + item.get("asset_type", "股票"))
            st.caption("加入时间 " + fact_time(item.get("created_at")))
            with st.container(horizontal=True):
                if st.button("开始研究", key=f"watchlist_research_{focused_id}", icon=":material/travel_explore:", disabled=not actions["ready"]()):
                    prefix = WATCHLIST_RESEARCH_PREFIX[item.get("asset_type", "股票")]
                    direction = next(kind for kind, value in RESEARCH_PREFIX.items() if value == prefix)
                    actions["activate"](direction)
                    st.session_state.research_direction = direction
                    if actions["analysis"](api_base, f"{prefix}请分析{item['target']}的主要机会与风险"):
                        actions["save"]()
                        st.session_state.pending_navigation = "投资问答"
                        st.rerun()
                if st.button("移除", key=f"watchlist_remove_{focused_id}", icon=":material/delete:"):
                    if actions["api"](api_base, "DELETE", f"/watchlist/{focused_id}") is not None:
                        st.session_state.watchlist = [saved for saved in items if int(saved["id"]) != focused_id]
                        clear_snapshot("comparison")
                        st.session_state.pop("watchlist_comparison_request", None)
                        st.session_state.pop("watchlist_comparison_advice", None)
                        st.rerun()
            actions["history"](api_base, [item])
    if len(items) >= 2:
        st.subheader("横向比较")
        labels = {int(item["id"]): f"{_item_label(item)} · {item.get('asset_type', '股票')}" for item in items}
        before = st.session_state.get("watchlist_compare_ids", [])
        valid = [item_id for item_id in before if item_id in labels][:4]
        if valid != before:
            st.session_state.watchlist_compare_ids = valid
        st.caption("选择 2–4 个同类型标的；先核对数据，再解读差异与风险。")
        with st.container(key="watchlist_compare", border=True):
            selected_ids = st.multiselect("选择 2–4 个标的", list(labels), format_func=lambda item_id: labels[item_id], max_selections=4, key="watchlist_compare_ids", persist_state="session")
            compare = st.button("生成对比研究", key="watchlist_compare_run", icon=":material/compare_arrows:", disabled=not actions["ready"]())
        if compare:
            selected = [item for item in items if int(item["id"]) in selected_ids]
            if not 2 <= len(selected) <= 4:
                st.warning("请选择 2–4 个标的进行比较。")
            elif len({item.get("asset_type", "股票") for item in selected}) != 1:
                st.warning("横向比较需选择相同类型的标的，请按股票、基金、行业、可转债或指数分别比较。")
            else:
                clear_snapshot("comparison")
                st.session_state.pop("watchlist_comparison_advice", None)
                st.session_state.watchlist_comparison_request = {"asset_type": selected[0].get("asset_type", "股票"), "targets": [item["target"] for item in selected], "ids": selected_ids}
        comparison_result(api_base, actions)
