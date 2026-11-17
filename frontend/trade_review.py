"""交易画像使用的费用复盘展示。"""
import streamlit as st


def render_review(result):
    st.caption(result["notice"])
    st.write(f"已知费用：{result['known_fees']:,.2f}元；缺费用记录：{result['missing_fee_rows']}笔；无法完整匹配卖出：{result['unmatched_sells']}笔。")
    if result["matched_net"] is not None: st.metric("可完整匹配卖出净损益",f"{result['matched_net']:,.2f}元")
    else: st.info("净损益暂不完整，请补齐逐笔费用及期初成本；不能据此判断账户盈利能力。")
    if result["by_security"]:
        st.dataframe([{"证券":r["code"],"匹配卖出笔数":r["closed_sells"],"毛损益":r["gross"],"扣费净损益":r["net"],
            "样本胜率":f"{r['win_rate']:.1%}" if r["win_rate"] is not None else "缺费用"} for r in result["by_security"]],hide_index=True)
    if result.get("performance"):
        p=result["performance"]; st.caption(p["basis"])
        if p["period_return"] is not None:
            st.write(f"期间时间加权收益：{p['period_return']:.2%}；观察点最大回撤：{p['max_drawdown']:.2%}。")

