"""长周期基金复利表现与同口径风险对比。"""
import pandas as pd
import streamlit as st
from frontend.charts import render_line
from backend.app.services.fund_analytics import CALCULATION_VERSION


def render_fund_analytics(api_base, api_request, row):
    from frontend.research_board import known_entities
    if api_request is None:
        st.caption("登录后可计算长期收益与同口径风险。")
        return
    code = str(row["code"] or "").split(".")[0]
    key = "fund_analytics_" + code
    options = {str(symbol).split(".")[0]: name for symbol, name in known_entities(api_base, "fund").items()}
    options[code] = row["name"]
    selected = st.multiselect("比较基金（1–4只）", list(options), default=[code], max_selections=4,
                             format_func=lambda value: f"{options.get(value, value)} · {value}" if value in options else value,
                             accept_new_options=True, key=key + "_targets", persist_state="session")
    with st.container(horizontal=True):
        years = st.selectbox("历史窗口", [1, 3, 5], format_func=lambda value: f"近{value}年", key=key + "_years")
        rf = st.number_input("假设年化无风险利率（%）", min_value=0.0, max_value=20.0, value=0.0, step=.1,
                             key=key + "_rf", help="统一用于夏普计算的参数，不代表当前市场利率。")
    signature = (CALCULATION_VERSION, api_base, tuple(selected), years, rf)
    cache = st.session_state.setdefault("fund_analytics_results", {})
    if st.button("计算长周期收益与风险", key=key + "_run", disabled=not selected, icon=":material/analytics:"):
        with st.spinner("正在核对历史序列与共同日期…"):
            result = api_request(api_base, "POST", "/data/fund-analytics",
                                 {"targets": selected, "years": years, "risk_free_rate": rf})
        if isinstance(result, dict):
            cache[signature] = result
            while len(cache) > 8:
                cache.pop(next(iter(cache)))
    result = cache.get(signature)
    if result is None:
        st.caption("点击后读取完整历史；普通日用相邻净值比保留精度，分红与折算日使用披露增长率。在线准备时间独立于3秒问答。")
        return
    for failure in result.get("failures", []):
        st.caption(str(failure.get("target", "")) + "：" + str(failure.get("message", "")))
    items = result.get("items") or []
    if not items:
        st.info(result.get("message") or "可核对的完整窗口数据不足，暂不计算风险指标。")
        return
    def display(value):
        return f"{value:.2f}" if value is not None else "—"
    st.dataframe([{"基金": f"{item['name']} · {item['code']}",
                   "区间收益(%)": display(item.get("total_return_pct")),
                   "年化收益(%)": display(item.get("annual_return_pct")),
                   "最大回撤(%)": display(item.get("max_drawdown_pct")),
                   "年化波动率(%)": display(item.get("volatility_pct")),
                   "夏普比率": display(item.get("sharpe_ratio")), "共同观测点": item.get("observations")}
                  for item in items], hide_index=True, width="stretch", height="content")
    records = [{"日期": point["date"], "基金": f"{item['name']} · {item['code']}", "复利指数": point["value"]}
               for item in items for point in item.get("curve", [])]
    if records:
        frame = pd.DataFrame(records)
        frame["日期"] = pd.to_datetime(frame["日期"])
        render_line(frame, x="日期", y="复利指数", color="基金", width="stretch")
    st.caption(f"共同区间 {result.get('start_date')} 至 {result.get('end_date')} · 年化基数 {result.get('annualization')} · 假设无风险利率 {result.get('risk_free_rate')}%")
    for note in result.get("notes", []):
        st.caption(note)
