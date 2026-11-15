"""基金详情：只展示供应商事实及其日期，不把短期净值曲线当作总回报。"""
from __future__ import annotations

import streamlit as st
from frontend.financial_view import period_info
from backend.app.fund_fields import FUND_RETURNS, FUND_PROFILE, FUND_FEES, FUND_RISK, FUND_ALLOCATION, FUND_MONEY


def render_fund_details(result, target, *, api_base="", api_request=None):
    from frontend.research_board import entity_rows, section_facts, format_fact, data_period, render_history, entity_label, target_rows

    facts = [fact for section in ("overview", "profile", "performance", "allocation")
             for fact in section_facts(result, section)]
    combined = target_rows(entity_rows(facts), target)
    references = section_facts(result, "reference")
    if not combined:
        combined = target_rows(entity_rows(references), target)
    if not target:
        st.caption("选择或输入基金名称 / 代码，可查看阶段收益、风险、概况费率与持仓配置。")
        return
    if not combined:
        return
    # 每个代码独立展示；名称查询有多个份额时不合并 A/C 或相似名称基金。
    for row in combined:
        with st.container(border=True):
            st.markdown("**" + entity_label(row) + "**")
            fields = row["fields"]
            money_query = {field:fields.get(field) for field in FUND_MONEY}
            reference_facts = [fact for fact in references if row["code"] and
                               str(fact.get("entity_code") or "").split(".")[0] == str(row["code"]).split(".")[0]]
            reference_rows = entity_rows(reference_facts)
            reference = reference_rows[0]["fields"] if len(reference_rows) == 1 else {}
            for field, fact in reference.items():
                if field not in fields and field not in row["conflicts"]:
                    fields[field] = fact
            for field in ("fund_nav", "nav_change", "accumulated_nav"):
                if field not in fields and field not in row["conflicts"] and reference.get("reference_" + field):
                    fields[field] = reference["reference_" + field]
            for field in FUND_MONEY:
                public, original = reference.get(field), money_query.get(field)
                public_period = period_info((public or {}).get("period"))
                original_period = period_info((original or {}).get("period"))
                if public and field not in row["conflicts"] and public_period and (not original_period or public_period[1] >= original_period[1]):
                    fields[field] = public
            if len(combined) > 1:
                st.caption("本次查询返回多个基金 / 份额，可在上方选择具体代码继续查看。")
            money = bool(fields.get("yield_7d") or fields.get("income_per_10000")) or "货币" in str(fields.get("fund_type", {}).get("value") or "")
            size_metric = ("reported_fund_size", "净资产规模（披露）") if fields.get("reported_fund_size") else ("fund_size", "基金规模")
            metrics = [*FUND_MONEY.items(), size_metric] if money else [
                ("fund_nav", "单位净值"), ("nav_change", "净值日涨跌幅"), ("accumulated_nav", "累计净值"), size_metric]
            with st.container(horizontal=True):
                for field, label in metrics:
                    st.metric(label, "数据待核对" if field in row["conflicts"] else format_fact(fields.get(field)), border=True)
            if money:
                st.caption("七日年化日期 " + data_period(fields.get("yield_7d")) + " · 万份收益日期 " + data_period(fields.get("income_per_10000")))
                st.caption("七日年化反映近期收益水平，不是过去一年的实际收益或未来收益承诺。")
            else:
                st.caption("净值日期 " + data_period(fields.get("fund_nav")) + " · 规模日期 " + data_period(fields.get("reported_fund_size") or fields.get("fund_size")))
            for field in ("fund_nav", "nav_change", "accumulated_nav"):
                original, other = fields.get(field), reference.get("reference_" + field)
                if original and other and data_period(original) == data_period(other):
                    from frontend.financial_view import fact_numeric_value
                    first, second = fact_numeric_value(original), fact_numeric_value(other)
                    if first is not None and second is not None and abs(first-second) > (.011 if field == "nav_change" else .00005):
                        st.warning(f"{ {'fund_nav': '单位净值', 'nav_change': '日涨幅', 'accumulated_nav': '累计净值'}[field]}同日来源不一致：问财 {format_fact(original)}，天天基金 {format_fact(other)}，请核对原始来源。")
            performance, profile, allocation, analytics = st.tabs(["收益与风险", "概况与费率", "持仓与配置", "长周期与对比"])

            def table(group):
                records = [{"指标": "最高" + label if "最高" in str(fields.get(field, {}).get("source_field") or "") else label,
                            "数值": "数据待核对" if field in row["conflicts"] else format_fact(fields.get(field)),
                            "数据日期": data_period(fields.get(field)),
                            "口径": "净值区间涨幅" if "区间涨跌幅" in str(fields.get(field, {}).get("source_field") or "") else "数据源公布值",
                            "来源": "天天基金公开档案" if fields.get(field, {}).get("source_id") == "PUBLIC_FUND_EASTMONEY_V1" else "同花顺问财" if fields.get(field) else "未取得"}
                           for field, label in group.items()]
                if group is FUND_RETURNS and any(reference.get("reference_" + field) for field in group):
                    for record, field in zip(records, group):
                        other = reference.get("reference_" + field)
                        record["问财区间值"] = record["数值"]
                        record["问财区间"] = record["数据日期"]
                        if other:
                            record.update({"数值": format_fact(other), "数据日期": data_period(other),
                                           "来源": "天天基金公布值", "口径": "网站公布的阶段收益"})
                if group is FUND_MONEY and any(money_query.values()):
                    for record, field in zip(records, group):
                        record.update({"问财值":format_fact(money_query[field]),"问财日期":data_period(money_query[field])})
                st.dataframe(records, hide_index=True, width="stretch", height="content")

            with performance:
                if money:
                    table(FUND_MONEY)
                st.markdown("**阶段收益**")
                table(FUND_RETURNS)
                if any(reference.get("reference_" + field) for field in FUND_RETURNS):
                    st.caption("同时保留问财区间值。网站阶段收益与问财日期窗口可能不同，不能将差值直接视为数据错误。")
                st.markdown("**风险指标**")
                table(FUND_RISK)
                st.caption("各阶段收益与风险指标沿用数据源的区间和计算口径；不同截止日或不同基金类型不宜直接比较。")
                peer = fields.get("fund_peer_comparison")
                st.markdown("**同类均值、排名与基准**")
                if peer and isinstance(peer.get("value"), list):
                    st.dataframe(peer["value"], hide_index=True, width="stretch", height="content")
                    st.caption("截至 " + data_period(peer) + " · 同类划分、样本数和基准沿用来源网站；不同区间的样本总数可能不同。")
                else:
                    st.caption("本次未取得可核对的同类排名与同类均值。")
            with profile:
                table(FUND_PROFILE)
                original_date, reported_date = fields.get("inception_date"), fields.get("reported_inception_date")
                if original_date and reported_date and format_fact(original_date) != format_fact(reported_date):
                    st.info(f"成立日期口径不一致：问财 {format_fact(original_date)}，公开档案 {format_fact(reported_date)}。请结合产品转型、合同变更记录核对。")
                st.markdown("**基金费率**")
                table(FUND_FEES)
                st.caption("管理费、托管费和销售服务费通常为年费率；申购、赎回费率以份额类别、持有期限及销售渠道公告为准。")
            with allocation:
                table({field: label for field, label in FUND_ALLOCATION.items() if field not in {"top_holdings", "bond_holdings", "industry_allocation"}})
                for field in ("top_holdings", "bond_holdings", "industry_allocation"):
                    fact = fields.get(field)
                    st.markdown("**" + FUND_ALLOCATION[field] + "**")
                    value = (fact or {}).get("value")
                    if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
                        st.dataframe(value, hide_index=True, width="stretch", height="content")
                    elif isinstance(value, dict):
                        st.dataframe([{"项目": key, "数据": str(item)} for key, item in value.items()],
                                     hide_index=True, width="stretch", height="content")
                    else:
                        st.write(format_fact(fact))
                    source = "同花顺基金公开组合" if (fact or {}).get("source_id") == "PUBLIC_FUND_THS_V1" else "天天基金 / 问财，见原始资料"
                    st.caption("披露期 " + data_period(fact) + " · " + source)
                st.caption("持仓为定期披露资料，存在披露时滞；缺失仓位不按零处理。")
            with analytics:
                if money:
                    st.caption("货币基金请查看七日年化、万份收益和来源公布的阶段收益；本页不将它们冒充普通基金净值序列计算长期风险。")
                else:
                    from frontend.fund_analytics import render_fund_analytics
                    render_fund_analytics(api_base, api_request, row)
            if reference:
                code = str(row["code"]).split(".")[0]
                st.link_button("查看天天基金原始档案", f"https://fundf10.eastmoney.com/jbgk_{code}.html", icon=":material/open_in_new:")
    with st.container(border=True):
        # 多个匹配基金时不把各自净值接成一条曲线。
        selected = combined[0] if len(combined) == 1 else None
        money_history = (selected or {}).get("fields", {}).get("money_income_history", {})
        if money_history.get("value"):
            import pandas as pd
            st.markdown("**近30期每万份收益（元）**")
            frame = pd.DataFrame(money_history["value"]).rename(columns={"date":"日期", "value":"每万份收益（元）"})
            frame["日期"] = pd.to_datetime(frame["日期"])
            st.line_chart(frame,x="日期",y="每万份收益（元）",width="stretch")
            st.caption("来源：天天基金公开序列 · 区间 " + data_period(money_history))
            return
        st.markdown("**近 30 期单位净值走势**")
        history = [fact for fact in section_facts(result, "history")
                   if selected and ((fact.get("entity_code") and
                       str(fact["entity_code"]).upper() == str(selected["code"] or "").upper()) or
                       (not fact.get("entity_code") and fact.get("entity") == target))]
        render_history(history)
        st.caption("曲线是单位净值，未据此推算含分红再投资的收益率、年化收益或一年风险指标。")
