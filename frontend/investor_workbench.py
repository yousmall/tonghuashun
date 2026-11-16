"""面向新手和有交易经验用户的投资工作台。"""
import csv
from io import StringIO
from datetime import timedelta
import streamlit as st
from backend.app.risk_questionnaire import local_today, assessment_is_current
from backend.app.services.investor_workbench import Preferences, cost_scenario

LESSONS = [
    ("先判断这笔钱能否投资", "区分生活费、应急资金和投资资金；完成风险评估，核对用款时间和能够承受的损失。", "风险评估"),
    ("看懂股票与价格", "股票代表公司权益。价格会波动，便宜的股价不等于便宜的估值；先理解公司靠什么盈利。", "投资问答"),
    ("比较候选与证据", "比较盈利、估值、负债、行业与风险，核对数据日期和来源；评分不是获利概率。", "自选研究"),
    ("先练习再决定", "用下方教学沙盘练习数量、费用、可用资金与卖出限制；输入的是假设成交价格，不验证真实市场是否可成交。", None),
    ("记录理由并复盘", "记录购买理由、观察期限和失效条件。比较扣费后结果，检查是否因频繁换手增加成本。", None),
]

def records(text, mapping):
    reader=csv.DictReader(StringIO(text.strip()))
    if not reader.fieldnames or len(set(reader.fieldnames))!=len(reader.fieldnames):
        raise ValueError("请提供带唯一表头的CSV")
    if any(h not in mapping for h in reader.fieldnames): raise ValueError("表头与示例不一致，请核对")
    return [{mapping[k]:v.strip() for k,v in row.items() if k and v is not None and v.strip()} for row in reader]

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

def _remember_view():
    st.session_state.workbench_view=st.session_state.workbench_section

def render_workbench(api_base, *, api_request, go_to):
    st.title("投资工作台")
    result=api_request(api_base,"GET","/investor/workspace")
    if not result: return
    profile=result["profile"]
    has_risk_draft=bool(st.session_state.get("profile_draft"))
    if not has_risk_draft:
        st.session_state.profile=profile
    workspace=result["workspace"]; pref=Preferences.model_validate(workspace.get("preferences",{}))
    views=["新手学习","偏好与当前持仓","交易复盘","教学模拟","决策记录"]
    section=st.radio("工作区域",views,index=views.index(st.session_state.get("workbench_view","新手学习")),horizontal=True,
        key="workbench_section",on_change=_remember_view,persist_state="session")
    if has_risk_draft and section in {"偏好与当前持仓","教学模拟","决策记录"}:
        st.info("有尚未确认的风险问卷，请先返回风险评估核对并确认保存，再修改账号工作台。学习和只读复盘仍可使用。")
        return
    if section=="新手学习":
        for i,(title,text,page) in enumerate(LESSONS):
            with st.container(border=True):
                st.markdown(f"**{i+1}. {title}**"); st.write(text)
                if i==0: st.caption("测评已确认且有效" if assessment_is_current(profile) else "请先完成并确认风险测评")
                if page: st.button("打开"+page,key=f"workbench_go_{i}",on_click=go_to,args=(page,))
        quiz=st.radio("练习：过去频繁交易是否说明能够承受更高风险？",["请选择","不能，仍需核对资金用途和损失承受能力","能，交易越多风险承受能力越高"],key="workbench_quiz")
        if quiz!="请选择": (st.success if quiz.startswith("不能") else st.warning)("交易经验与风险承受能力应分别评估。")
    if section=="偏好与当前持仓":
        suggestion=result["history_suggestions"]
        st.write("历史报告待确认线索："+"、".join(suggestion["industries"] or ["尚无已确认的题材报告"]))
        st.caption(suggestion["notice"]+" 历史持有期线索："+suggestion["style"])
        if suggestion["industries"] and st.button("将历史线索填入待确认草稿"):
            st.session_state.workbench_draft_industries=",".join(suggestion["industries"])
        with st.form("workbench_preferences"):
            sectors=st.text_input("希望研究的行业（逗号分隔）",value=st.session_state.get("workbench_draft_industries",",".join(pref.industries)))
            avoid=st.text_input("排除的行业（逗号分隔）",value=",".join(pref.avoid_industries))
            mode=st.selectbox("行业筛选方式",["prefer","restrict"],index=["prefer","restrict"].index(pref.mode),format_func=lambda x:"优先研究，保留其他候选" if x=="prefer" else "仅研究这些行业")
            style=st.selectbox("研究周期",["未设定","短期观察","中长期研究"],index=["未设定","短期观察","中长期研究"].index(pref.style))
            amount=st.number_input("计划单次新增金额（元，0表示暂未明确）",min_value=0.,max_value=1e8,value=float(pref.planned_trade_amount or 0))
            commission=st.number_input("佣金假设（%）",min_value=0.,max_value=5.,value=float(pref.commission_rate)*100,format="%.4f")
            minimum=st.number_input("每边最低佣金假设（元）",min_value=0.,max_value=1000.,value=float(pref.minimum_commission))
            tax=st.number_input("卖出税费假设（%）",min_value=0.,max_value=5.,value=float(pref.sell_tax_rate)*100,format="%.4f")
            slip=st.number_input("每边滑点假设（%）",min_value=0.,max_value=5.,value=float(pref.slippage_rate)*100,format="%.4f")
            maxcost=st.number_input("可接受往返成本预算（%）",min_value=0.,max_value=20.,value=pref.max_round_trip_cost_ratio*100)
            st.caption("以上均为可修改的情景假设；不会自动提高风险等级。行业分类应与候选使用同一口径。")
            text=st.text_area("当前真实持仓（CSV，市值为人民币元）",value="代码,行业,市值\n"+"\n".join(f"{h['code']},{h['industry']},{h['value']}" for h in workspace.get("holdings",[])))
            cash=st.number_input("当前可用现金（元）",min_value=0.,max_value=1e12,value=float(workspace.get("cash",0)))
            day=st.date_input("持仓估值日期",value=local_today(),max_value=local_today())
            complete=st.checkbox("我已录入本次用于核对的全部投资资产与现金",value=workspace.get("holdings_complete",False))
            if st.form_submit_button("确认保存偏好与持仓",type="primary"):
                try:
                    payload={"expected_version":profile["version"],"preferences":{"industries":[s.strip() for s in sectors.replace("，",",").split(",") if s.strip()],
                        "avoid_industries":[s.strip() for s in avoid.replace("，",",").split(",") if s.strip()],"style":style,"mode":mode,
                        "commission_rate":str(commission/100),"minimum_commission":str(minimum),"sell_tax_rate":str(tax/100),"slippage_rate":str(slip/100),
                        "planned_trade_amount":amount or None,"max_round_trip_cost_ratio":maxcost/100},
                        "holdings":records(text,{"代码":"code","行业":"industry","市值":"value"}),"cash":cash,"holdings_as_of":day.isoformat(),"holdings_complete":complete}
                    saved=api_request(api_base,"POST","/investor/workspace",payload)
                    if saved:
                        st.session_state.profile=saved
                        st.session_state.pop("workbench_draft_industries",None)
                        st.rerun()
                except ValueError as exc: st.error(str(exc))
        if pref.planned_trade_amount:
            scenario=cost_scenario(pref.planned_trade_amount,pref.model_dump())
            st.write(f"当前金额往返成本情景：{scenario['round_trip_cost']:,.2f}元，费用门槛{scenario['break_even_return']:.2%}。")
            st.caption(scenario["notice"])
        st.caption("在个股研究中发起“按我的投资偏好推荐股票”，候选会经过现有风险与证据核验，再使用已确认行业偏好、成本预算及持仓额度筛选排序。")
    if section=="交易复盘":
        st.caption("可先在风险评估导入Excel交易报告。只有汇总报告时无法独立验证净收益；这里可补充带费用的逐笔成交及资产估值。复盘输入只用于本次计算，不保存原始成交。")
        with st.form("workbench_review"):
            trades=st.text_area("逐笔成交CSV",value="日期,代码,方向,数量,价格,费用,行业\n",help="方向填buy或sell；费用为该笔全部实际费用。缺费用留空，不能当作0。")
            opening=st.text_area("可选期初持仓CSV",value="代码,数量,含历史买入费用的单位成本,取得日期,行业\n",help="只填第一笔成交之前已持有的股票；有不同取得日期或成本时分行录入。")
            nav=st.text_area("可选资产估值CSV",value="日期,资产总值,期末净入金\n",help="资产总值包含全部持仓和现金。净入金必须发生在该观察期末；首行基准净入金填0。没有逐日估值时，回撤仅反映观察点。")
            if st.form_submit_button("计算扣费复盘",type="primary"):
                st.session_state.pop("workbench_review_result",None)
                try:
                    payload={"trades":records(trades,{"日期":"date","代码":"code","方向":"side","数量":"quantity","价格":"price","费用":"fee","行业":"industry"}),
                        "nav":records(nav,{"日期":"date","资产总值":"value","期末净入金":"flow"}),
                        "opening_positions":records(opening,{"代码":"code","数量":"quantity","含历史买入费用的单位成本":"unit_cost","取得日期":"acquired_on","行业":"industry"})}
                    calculated=api_request(api_base,"POST","/investor/review",payload)
                    if calculated: st.session_state.workbench_review_result=calculated
                except ValueError as exc: st.error(str(exc))
        if st.session_state.get("workbench_review_result"): render_review(st.session_state.workbench_review_result)
    if section=="教学模拟":
        st.info("教学沙盘：初始虚拟资金10万元；使用手工情景价格，不连接券商。练习假设为100股整手、买入次日可卖，不模拟真实交易日历、涨跌停、停牌或排队成交。")
        state=workspace.get("paper",{})
        st.metric("模拟可用现金",f"{float(state.get('cash',100000)):,.2f}元")
        with st.form("workbench_paper"):
            code=st.text_input("练习证券代码",value="600000",max_chars=6)
            side=st.selectbox("练习方向",["buy","sell"],format_func=lambda x:"模拟买入" if x=="buy" else "模拟卖出")
            quantity=st.number_input("练习股数",min_value=100,max_value=1000000,value=100,step=100)
            price=st.number_input("假设成交价格（元）",min_value=.0001,max_value=1e6,value=10.,format="%.4f")
            fee=st.number_input("假设该笔总费用（元）",min_value=0.,max_value=1e6,value=5.)
            day=st.date_input("练习日期",value=local_today()-timedelta(days=1),max_value=local_today())
            if st.form_submit_button("记录教学模拟",type="primary"):
                saved=api_request(api_base,"POST","/investor/paper-order",{"expected_version":profile["version"],"code":code,"side":side,"quantity":quantity,"price":price,"fee":fee,"date":day.isoformat()})
                if saved: st.session_state.profile=saved; st.rerun()
        if state.get("trades"): st.dataframe(state["trades"],hide_index=True); render_review(state["review"])
    if section=="决策记录":
        st.caption("为同一股票持续追加观察和复盘记录，保留当时理由。记录与当前账号保存，记录本身不触发真实交易。")
        with st.form("workbench_journal"):
            code=st.text_input("记录证券代码",max_chars=6)
            thesis=st.text_area("研究或购买理由")
            invalidation=st.text_area("什么情况说明原判断失效")
            day=st.date_input("下一次复核日期",value=local_today()+timedelta(days=7))
            outcome=st.text_area("本次观察或复盘结果（可留空）")
            if st.form_submit_button("保存决策记录",type="primary"):
                saved=api_request(api_base,"POST","/investor/journal",{"expected_version":profile["version"],"code":code,
                    "thesis":thesis,"invalidation":invalidation,"review_date":day.isoformat(),"outcome":outcome})
                if saved: st.session_state.profile=saved; st.rerun()
        entries=workspace.get("journal",[])
        if entries:
            st.dataframe([{"股票":e["code"],"记录日":e["recorded_on"],"复核日":e["review_date"],"到期需复核":e["review_date"]<=local_today().isoformat(),
                "当时理由":e["thesis"],"失效条件":e["invalidation"],"结果":e["outcome"]} for e in reversed(entries)],hide_index=True)
