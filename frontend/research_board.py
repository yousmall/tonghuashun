"""研究页数据展示。概览只用于浏览，独立于投资问答的证据与结论。"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
import re
from datetime import datetime
from time import monotonic
from typing import Any, Callable

import streamlit as st
from frontend.financial_view import fact_unit, fact_numeric_value
from streamlit.runtime.scriptrunner import get_script_run_ctx

DIRECTIONS = {"市场解读": "market", "行业分析": "industry", "个股研究": "stock", "基金筛选": "fund", "可转债分析": "convertible"}
STOCKS = {"600519": "贵州茅台", "300750": "宁德时代", "600036": "招商银行", "601318": "中国平安", "000333": "美的集团", "600900": "长江电力"}
FUNDS = {"510300": "沪深300ETF", "510500": "中证500ETF", "588000": "科创50ETF", "159915": "创业板ETF"}
LABELS = {
    "close_price": "最新价", "change": "涨跌幅", "turnover_rate": "换手率", "turnover_value": "成交额",
    "volume": "成交量", "market_cap": "总市值", "float_market_cap": "流通市值", "pe_ttm": "市盈率 TTM",
    "pb": "市净率", "roe": "净资产收益率", "roe_weighted": "加权净资产收益率", "revenue_growth": "营收同比增长",
    "net_profit_growth": "归母净利润同比增长", "industry": "所属行业", "main_business": "主营业务",
    "company_name": "公司全称", "listing_date": "上市日期", "fund_nav": "单位净值", "nav_change": "净值涨跌幅",
    "fund_size": "基金规模", "fee_rate": "管理费率", "fund_manager": "基金经理", "tracking_error": "跟踪误差",
    "capital_flow": "主力资金净流入", "conversion_premium_rate": "转股溢价率", "pure_bond_premium_rate": "纯债溢价率",
    "yield_to_maturity": "到期收益率", "remaining_size": "剩余规模", "bond_rating": "债券评级", "conversion_price": "转股价",
    "gdp": "GDP 同比增长", "cpi": "CPI", "ppi": "PPI", "pmi": "制造业 PMI", "social_financing": "社会融资", "interest_rate": "利率",
}
PERCENT_FIELDS = {"change", "nav_change", "turnover_rate", "roe", "roe_weighted", "revenue_growth", "net_profit_growth",
                  "fee_rate", "tracking_error", "conversion_premium_rate", "pure_bond_premium_rate", "yield_to_maturity"}
MONEY_FIELDS = {"market_cap", "float_market_cap", "turnover_value", "fund_size", "capital_flow", "remaining_size", "social_financing"}
TABLE_FIELDS = {
    "market": ["close_price", "change", "turnover_value"],
    "industry": ["change", "capital_flow", "turnover_value", "pe_ttm", "pb"],
    "stock": ["close_price", "change", "market_cap", "pe_ttm", "pb", "industry"],
    "fund": ["fund_nav", "nav_change", "fund_size", "fee_rate", "fund_manager"],
    "convertible": ["close_price", "change", "conversion_premium_rate", "yield_to_maturity", "remaining_size", "bond_rating"],
}


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(str(value).replace(",", "").replace("%", "").strip())
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def format_fact(fact: dict | None, *, index: bool = False) -> str:
    if not fact:
        return "—"
    value = fact.get("value")
    field = fact.get("field")
    numeric = number(value)
    if field in {"listing_date", "inception_date"}:
        return data_period({"period": value})
    if numeric is None:
        return str(value) if value not in (None, "") else "—"
    if field in PERCENT_FIELDS:
        numeric = fact_numeric_value(fact)
        if numeric is None:
            return f"{value}（单位待确认）"
        return f"{numeric:+.2f}%" if field in {"change", "nav_change", "revenue_growth", "net_profit_growth"} else f"{numeric:.2f}%"
    if field in MONEY_FIELDS:
        raw = str(fact.get("source_field") or "")
        if "万亿元" in raw:
            return f"{numeric:,.2f} 万亿元"
        if "亿元" in raw:
            return f"{numeric:,.2f} 亿元"
        if "万元" in raw:
            return f"{numeric:,.2f} 万元"
        if abs(numeric) >= 1e12:
            return f"{numeric / 1e12:,.2f} 万亿元"
        if abs(numeric) >= 1e8:
            return f"{numeric / 1e8:,.2f} 亿元"
        if abs(numeric) >= 1e4:
            return f"{numeric / 1e4:,.2f} 万元"
        return f"{numeric:,.2f} 元"
    if field in {"fund_nav"}:
        return f"{numeric:.4f}"
    if field in {"close_price", "conversion_price"}:
        return f"{numeric:,.2f}" + (" 点" if index else " " + fact_unit(fact))
    if field in {"pe_ttm", "pb"}:
        return f"{numeric:.2f} 倍"
    return f"{numeric:,.2f}"


def data_period(fact: dict | None) -> str:
    period = str((fact or {}).get("period") or "")
    if not period or period.startswith("REC-"):
        return "日期未提供"
    if re.fullmatch(r"\d{8}", period):
        return f"{period[:4]}-{period[4:6]}-{period[6:]}"
    return period


def entity_rows(facts: list[dict]) -> list[dict]:
    """按证券代码分组，保留每项指标的日期；同日冲突不任意选一个数。"""
    groups = {}
    for fact in facts:
        if fact.get("field") == "provider_response":
            continue
        name = str(fact.get("entity") or "")
        identity = fact.get("entity_code") or name
        if not identity:
            continue
        row = groups.setdefault(identity, {"name": name, "code": fact.get("entity_code"), "fields": {}, "conflicts": set()})
        field = fact.get("field")
        previous = row["fields"].get(field)
        if previous:
            new_period, old_period = data_period(fact), data_period(previous)
            new_date = "" if new_period == "日期未提供" else new_period
            old_date = "" if old_period == "日期未提供" else old_period
            if new_date < old_date:
                continue
            if new_date == old_date:
                first, second = fact_numeric_value(previous), fact_numeric_value(fact)
                same = math.isclose(first, second, rel_tol=1e-8, abs_tol=5e-5 if field in PERCENT_FIELDS else 0) if first is not None and second is not None else previous.get("value") == fact.get("value")
                if not same:
                    row["conflicts"].add(field)
                    continue
            else:
                row["conflicts"].discard(field)
        row["fields"][field] = fact
    for row in groups.values():
        for field in row["conflicts"]:
            row["fields"].pop(field, None)
    return list(groups.values())[:8]


@st.cache_resource(show_spinner=False)
def board_prefetch_executor() -> ThreadPoolExecutor:
    """全站共用有上限的取数线程；任务不读写 Streamlit 会话。"""
    return ThreadPoolExecutor(max_workers=8, thread_name_prefix="research-board-prefetch")


def cancel_board_prefetch() -> None:
    for future in st.session_state.pop("research_board_prefetch", {}).values():
        future.cancel()


def start_board_prefetch(api_base: str, fetch: Callable) -> None:
    """登录后预取五个方向的默认页面，不阻塞首页。"""
    cancel_board_prefetch()
    executor = board_prefetch_executor()

    def load(direction: str, target: str | None) -> tuple[float, Any]:
        try:
            result = fetch(api_base, direction, target)
        except Exception:
            result = None
        return monotonic(), result

    st.session_state.research_board_prefetch = {
        (api_base, direction, target): executor.submit(load, direction, target)
        for direction, target in ((direction, "600519" if direction == "stock" else None)
                                  for direction in DIRECTIONS.values())
    }


def board_cache_ttl(result: dict) -> float:
    if result.get("status") == "loading":
        return 0.8
    return 60 if result.get("status") in {"ok", "partial"} else 30


def _fetch_snapshot(fetch, api_base, direction, target, refresh=False):
    try:
        if getattr(fetch, "supports_refresh", False):
            result = fetch(api_base, direction, target, refresh=refresh)
        else:
            result = fetch(api_base, direction, target)
        return monotonic(), result
    except Exception:
        return monotonic(), None


def load_board(api_base: str, direction: str, target: str | None, fetch: Callable, *, refresh: bool = False, nonblocking: bool = False) -> dict:
    cache = st.session_state.setdefault("research_board_cache", {})
    pending = st.session_state.setdefault("research_board_prefetch", {})
    key = (api_base, direction, target)
    entry = cache.get(key)
    if refresh:
        future = pending.get(key)
        if future and not nonblocking:
            pending.pop(key, None)
            future.cancel()
            future = None
    elif entry and monotonic() - entry["loaded_at"] < board_cache_ttl(entry["result"]):
        return entry["result"]
    future = pending.get(key)
    if nonblocking:
        if future is None:
            future = board_prefetch_executor().submit(_fetch_snapshot, fetch, api_base, direction, target, refresh)
            pending[key] = future
        if not future.done():
            if entry:
                return {**entry["result"], "refreshing": True}
            return {"direction": direction, "target": target, "status": "loading", "sections": [], "fetched_at": None}
    if future is not None:
        pending.pop(key, None)
        try:
            loaded_at, result = future.result()
        except Exception:
            loaded_at, result = monotonic(), None
        ttl = board_cache_ttl(result) if isinstance(result, dict) else 30
        if not nonblocking and monotonic() - loaded_at >= ttl:
            loaded_at, result = _fetch_snapshot(fetch, api_base, direction, target, refresh)
    else:
        loaded_at, result = _fetch_snapshot(fetch, api_base, direction, target, refresh)
    if not isinstance(result, dict) or result.get("direction") != direction or not isinstance(result.get("sections"), list):
        result = {"direction": direction, "target": target, "status": "unavailable", "sections": [], "fetched_at": None}
    # 刷新时保留旧栏，但明确标示为旧资料；概览绝不写入投资分析的 facts。
    if result.get("status") == "loading" and entry:
        old_sections = {section["key"]: section for section in entry["result"].get("sections", [])}
        for section in result["sections"]:
            if section.get("status") == "loading" and old_sections.get(section["key"], {}).get("status") == "ok":
                section["previous_facts"] = old_sections[section["key"]].get("facts", [])
    cache[key] = {"loaded_at": loaded_at, "result": result}
    while len(cache) > 16:
        cache.pop(next(iter(cache)))
    return result


def section_facts(result: dict, key: str) -> list[dict]:
    return next((section.get("facts", []) for section in result.get("sections", []) if section.get("key") == key and section.get("status") == "ok"), [])


def known_entities(api_base: str, direction: str) -> dict[str, str]:
    names = dict(STOCKS if direction == "stock" else FUNDS if direction == "fund" else {})
    for key, entry in st.session_state.get("research_board_cache", {}).items():
        if key[:2] != (api_base, direction):
            continue
        for row in entity_rows(section_facts(entry["result"], "overview")):
            if row["code"]:
                names[row["code"].split(".")[0]] = row["name"]
    if direction == "stock":
        for item in st.session_state.get("watchlist", []):
            if item.get("asset_type") == "股票":
                names[str(item["target"])] = str(item["target"])
    return names


def render_table(rows: list[dict], direction: str) -> None:
    import pandas as pd
    records = []
    for row in rows:
        record = {"名称": row["name"]}
        if row["code"]:
            record["代码"] = row["code"]
        for field in TABLE_FIELDS[direction]:
            record[LABELS[field]] = "数据待核对" if field in row["conflicts"] else format_fact(row["fields"].get(field), index=direction == "market")
        date_fields = ("conversion_premium_rate", "yield_to_maturity") if direction == "convertible" else ("close_price", "change", "fund_nav", "nav_change")
        dated_field = next((row["fields"].get(field) for field in date_fields if row["fields"].get(field) and data_period(row["fields"][field]) != "日期未提供"), None)
        record["净值日期" if direction == "fund" else "溢价率日期" if direction == "convertible" else "行情日期"] = data_period(dated_field)
        records.append(record)
    st.dataframe(pd.DataFrame(records), hide_index=True, width="stretch", height="content")


def render_comparison(rows: list[dict], direction: str) -> None:
    import altair as alt
    import pandas as pd
    field = "fund_size" if direction == "fund" else "conversion_premium_rate" if direction == "convertible" else "change"
    points = []
    periods = set()
    for row in rows:
        fact = row["fields"].get(field)
        if not fact or number(fact.get("value")) is None:
            continue
        period = data_period(fact)
        if field in {"change", "conversion_premium_rate"} and period == "日期未提供":
            continue
        periods.add(period)
        value = number(fact["value"])
        if field == "fund_size":
            raw = str(fact.get("source_field") or "")
            scale = 1e12 if "万亿元" in raw else 1e8 if "亿元" in raw else 1e4 if "万元" in raw else 1
            value = value * scale / 1e8
        points.append({"名称": row["name"], "数值": value, "日期": period})
    if len(points) < 2 or (field == "change" and len(periods) > 1):
        st.caption("可比较的同口径数据不足，暂不绘制对比图。")
        return
    label = LABELS[field]
    title = label + ("（亿元）" if field == "fund_size" else "（%）")
    roomy = direction in {"market", "industry"}
    chart = alt.Chart(pd.DataFrame(points)).mark_bar(cornerRadiusEnd=5).encode(
        y=alt.Y("名称:N", sort=None, title=None, axis=alt.Axis(labelLimit=135),
                scale=alt.Scale(paddingInner=0.48, paddingOuter=0.25) if roomy else alt.Scale()),
        x=alt.X("数值:Q", title=title),
        color=alt.condition(alt.datum.数值 >= 0, alt.value("#ad384e"), alt.value("#18794e")),
        tooltip=[alt.Tooltip("名称:N"), alt.Tooltip("数值:Q", title=title, format=",.2f"), alt.Tooltip("日期:N")],
    ).properties(height=max(240, len(points)*56) if roomy else max(180, len(points)*35)).configure_view(stroke=None)
    st.altair_chart(chart, width="stretch")


def render_history(facts: list[dict]) -> None:
    import altair as alt
    import pandas as pd
    values, conflicts = {}, set()
    metric = None
    for fact in facts:
        if fact.get("field") not in {"close_price", "fund_nav"}:
            continue
        try:
            observed = datetime.strptime(str(fact.get("period")), "%Y-%m-%d").date()
        except ValueError:
            continue
        value = number(fact.get("value"))
        if value is None or value <= 0 or observed > datetime.now().date():
            continue
        if metric and metric != fact["field"]:
            st.caption("价格与净值口径不同，暂不绘制走势。")
            return
        metric = fact["field"]
        if observed in values and not math.isclose(values[observed], value, rel_tol=1e-8):
            conflicts.add(observed)
        values[observed] = value
    records = [{"日期": day.isoformat(), "数值": value} for day, value in sorted(values.items()) if day not in conflicts]
    if len(records) < 2:
        st.caption("带真实日期的数值不足，暂时无法绘制走势。")
        return
    label = "单位净值" if metric == "fund_nav" else "收盘价（元）"
    frame = pd.DataFrame(records)
    base = alt.Chart(frame).encode(x=alt.X("日期:T", title=None, axis=alt.Axis(format="%m/%d")),
                                  y=alt.Y("数值:Q", title=label, scale=alt.Scale(zero=False)))
    chart = base.mark_line(color="#ad384e", strokeWidth=2.5).encode(
        tooltip=[alt.Tooltip("日期:T", format="%Y-%m-%d"), alt.Tooltip("数值:Q", title=label, format=".4f" if metric == "fund_nav" else ".2f")])
    st.altair_chart(chart.properties(height=235).configure_view(stroke=None), width="stretch")
    st.caption(f"数据区间 {records[0]['日期']} 至 {records[-1]['日期']} · {len(records)} 期 · 同花顺问财")


def render_news(facts: list[dict]) -> None:
    seen = set()
    titles = []
    for fact in facts:
        if fact.get("field") != "news" or str(fact.get("value")) in seen:
            continue
        seen.add(str(fact.get("value")))
        titles.append(fact)
        if len(titles) == 6:
            break
    if not titles:
        st.caption("本次未取得相关资讯。")
        return
    for fact in titles:
        title = str(fact.get("value", "")).replace("[", "\\[").replace("]", "\\]")
        if fact.get("source_url"):
            st.markdown(f"[{title}]({fact['source_url']})")
        else:
            st.write(str(fact.get("value", "")))
        related = [row for row in facts if row.get("period") == fact.get("period") and row.get("entity") == fact.get("entity")]
        published = next((row.get("value") for row in related if row.get("field") == "publish_date"), None)
        if published is None:
            timestamp = next((number(row.get("value")) for row in related if row.get("field") == "publish_time"), None)
            if timestamp:
                published = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
        if published:
            st.caption(str(published))


def render_research_board(api_base: str, page_direction: str, fetch: Callable) -> None:
    direction = DIRECTIONS[page_direction]
    target = st.session_state.get(f"board_target_{direction}", "600519" if direction == "stock" else None)
    key = (api_base, direction, target)
    entry = st.session_state.get("research_board_cache", {}).get(key)
    pending = key in st.session_state.get("research_board_prefetch", {})
    polling = pending or not entry or monotonic()-entry["loaded_at"] >= board_cache_ttl(entry["result"]) or entry["result"].get("status") == "loading"
    st.session_state[f"board_poll_{direction}"] = polling
    st.fragment(run_every=1 if polling else None)(_render_research_board)(api_base, page_direction, fetch)


def _render_research_board(api_base: str, page_direction: str, fetch: Callable) -> None:
    direction = DIRECTIONS[page_direction]
    with st.container(key=f"research-board-{direction}"):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.subheader({"market": "市场数据", "industry": "行业数据", "stock": "股票数据", "fund": "基金数据", "convertible": "可转债数据"}[direction])
            st.space("stretch")
            refresh = st.button("刷新数据", key=f"board_refresh_{direction}", icon=":material/refresh:")
        target = None
        if direction in {"stock", "fund", "convertible"}:
            names = known_entities(api_base, direction)
            options = list(names) if direction == "stock" else [None, *names]
            st.session_state.setdefault(f"board_target_{direction}", "600519" if direction == "stock" else None)
            target = st.selectbox("查看股票" if direction == "stock" else "查看基金" if direction == "fund" else "查看可转债",
                                  options, format_func=lambda code: "全部概览" if code is None else f"{names.get(code, code)} · {code}",
                                  key=f"board_target_{direction}", accept_new_options=True, persist_state="session",
                                  placeholder="选择或输入名称 / 代码" if direction == "stock" else "全部概览 · 可选择或输入名称 / 代码",
                                  help="可选择标的，也可输入名称或代码；投资问题请使用页面底部输入框。")
            if target is not None:
                target = str(target).strip()[:60]
        result = load_board(api_base, direction, target, fetch, refresh=refresh, nonblocking=True)
        loading = result.get("status") == "loading" or result.get("refreshing")
        polling = st.session_state.get(f"board_poll_{direction}", False)
        context = get_script_run_ctx()
        if bool(loading) != polling and context and getattr(context, "fragment_ids_this_run", None):
            # 仅由独立轮询触发重跑；完整页面必须先处理底部提交，不能丢掉提问。
            st.rerun()
        if loading:
            st.caption(":material/sync: 数据正在后台更新，已取得的栏目会陆续显示；下方可立即提问。")
        for section in result.get("sections", []):
            if section.get("previous_facts"):
                st.caption(section["title"] + "正在更新，旧资料可在下方查看。")
                with st.expander(section["title"] + " · 更新前资料"):
                    render_table(entity_rows(section["previous_facts"]), direction)
        fetched = result.get("fetched_at")
        if fetched:
            try:
                fetched = datetime.fromisoformat(str(fetched).replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
            except ValueError:
                fetched = str(fetched)[:16]
        st.caption(f"同花顺问财 · 获取时间 {fetched or '未取得'} · 数据日期见各项指标 · 仅供研究参考")
        rows = entity_rows(section_facts(result, "overview"))
        matching_overview = [fact for fact in section_facts(result, "overview") if target and str(fact.get("entity_code") or "").split(".")[0] == target.split(".")[0]]
        detail_rows = entity_rows(matching_overview + section_facts(result, "detail") + section_facts(result, "company"))
        macro = section_facts(result, "macro")
        news = section_facts(result, "news")
        if not rows and not detail_rows and not macro and not news:
            if result.get("status") == "loading":
                with st.container(horizontal=True):
                    for label in ["行情", "指标", "资料"]:
                        st.metric(label, "更新中", border=True)
                return
            with st.container(border=True):
                st.markdown(":material/cloud_off: **本页数据暂未取得**" if result.get("status") == "unavailable" else ":material/inbox: **本次没有可展示的数据**")
                st.caption("请稍后点击“刷新数据”。下方提问仍可使用，分析会再次尝试获取所需资料。")
                messages = [section.get("message") for section in result.get("sections", []) if section.get("message")]
                if messages:
                    st.caption(messages[0])
            return
        missing = [section["title"] for section in result.get("sections", []) if section.get("status") in {"empty", "unavailable"}]
        if missing:
            st.caption("部分数据暂未取得：" + "、".join(missing))
        if direction == "stock" and detail_rows:
            selected = next((row for row in detail_rows if target and target.split('.')[0] in str(row["code"] or "")), detail_rows[0])
            fields = selected["fields"]
            st.markdown(f"**{selected['name']}** · {selected['code'] or target or ''}")
            metrics = ["close_price", "change", "market_cap", "pe_ttm"]
            with st.container(horizontal=True):
                for field in metrics:
                    st.metric(LABELS[field], format_fact(fields.get(field)), border=True)
            st.caption("行情日期 " + data_period(fields.get("close_price")) + " · 财务指标报告期见下方")
            left, right = st.columns([1.45, 1], gap="medium")
            with left, st.container(border=True):
                st.markdown("**近 30 期价格走势**")
                render_history(section_facts(result, "history"))
            with right, st.container(border=True):
                st.markdown("**公司与财务指标**")
                for field in ["industry", "pb", "roe", "revenue_growth", "net_profit_growth", "listing_date"]:
                    fact = fields.get(field)
                    st.write(f"{LABELS[field]}：{format_fact(fact)}")
                    if field in {"roe", "revenue_growth", "net_profit_growth"} and fact:
                        st.caption("报告期 " + data_period(fact))
                business = fields.get("main_business") or fields.get("经营范围")
                if business:
                    st.caption(("主营业务 · " if fields.get("main_business") else "经营范围 · ") + str(business["value"]))
        else:
            with st.container(horizontal=True):
                for row in rows[:4]:
                    fields = row["fields"]
                    primary = "fund_nav" if direction == "fund" else "change" if direction == "industry" else "close_price"
                    value = format_fact(fields.get(primary), index=direction == "market")
                    secondary = fields.get("change") or fields.get("nav_change")
                    if direction == "industry":
                        secondary = fields.get("capital_flow")
                    st.metric(row["name"], value, format_fact(secondary) if secondary else None, delta_color="inverse", border=True)
            if result.get("target") and direction in {"fund", "convertible"}:
                with st.container(border=True):
                    st.markdown("**近 30 期走势**")
                    render_history(section_facts(result, "history"))
            else:
                with st.container(border=True):
                    st.markdown("**" + {"market": "指数涨跌对比", "industry": "行业涨跌对比", "fund": "基金规模对比", "convertible": "转股溢价率对比"}[direction] + "**")
                    render_comparison(rows, direction)
        if rows:
            with st.container(border=True):
                st.markdown("**" + {"market": "指数明细", "industry": "行业明细", "stock": "股票行情一览", "fund": "基金指标一览", "convertible": "转债指标一览"}[direction] + "**")
                render_table(rows, direction)
        macro = section_facts(result, "macro")
        news = section_facts(result, "news")
        if macro or news:
            columns = st.columns([1, 1.35]) if macro and news else [st.container()]
            if macro:
                with columns[0], st.container(border=True):
                    st.markdown("**宏观指标**")
                    records = [{"指标": str(fact.get("source_field")) if re.search(r"[\u4e00-\u9fff]", str(fact.get("source_field") or "")) else LABELS.get(fact["field"], "其他指标"), "数值": format_fact(fact), "统计期": data_period(fact)}
                               for fact in macro if fact.get("field") in {"cpi", "ppi", "pmi", "social_financing", "interest_rate", "gdp"}][:12]
                    if records:
                        import pandas as pd
                        st.dataframe(pd.DataFrame(records), hide_index=True, width="stretch", height="content")
                    else:
                        st.caption("供应商本次未提供可展示的宏观数值。")
            if news:
                with columns[-1], st.container(border=True):
                    st.markdown("**相关资讯**")
                    render_news(news)
        if any(row["conflicts"] for row in [*rows, *detail_rows]):
            st.caption("部分同日期指标存在不一致，已暂时隐藏相关数值，待进一步核对。")
        st.caption("列表为数据浏览范围，不代表投资推荐。百分比按问财原值展示；缺失指标以 — 标注。")


def restore_board_scroll(page_direction: str, *, has_conversation: bool) -> None:
    """进入空白研究页时展示顶部数据；不干扰后续阅读和聊天滚动。"""
    # token 仅取固定方向枚举，不将供应商内容、股票名称或用户输入拼入脚本。
    token = "chat" if has_conversation else DIRECTIONS[page_direction]
    script = """<script>
    (() => {
      const root = document.querySelector('[data-testid="stAppScrollToBottomContainer"]');
      const token = '__BOARD_TOKEN__';
      if (!root || root.dataset.researchView === token) return;
      root.dataset.researchView = token;
      if (token === 'chat') {
        root.scrollTo({top: root.scrollHeight, behavior: 'instant'});
        return;
      }
      let cancelled = false;
      const events = ['wheel', 'touchstart', 'pointerdown', 'keydown'];
      const cancel = event => { if (event.isTrusted) { cancelled = true; cleanup(); } };
      const cleanup = () => events.forEach(event => root.removeEventListener(event, cancel));
      events.forEach(event => root.addEventListener(event, cancel, {passive: true}));
      // 等待原生聊天布局完成初始化；用户一旦操作页面立即停止。
      [0, 150, 450, 900].forEach(delay => setTimeout(() => {
        if (!cancelled && root.isConnected && root.dataset.researchView === token) {
          // 原生底部聊天布局以 wheel 停止自动滚动，再定位到数据页顶部。
          root.dispatchEvent(new WheelEvent('wheel', {deltaY: -1}));
          root.scrollTo({top: 0, behavior: 'instant'});
        }
      }, delay));
      setTimeout(cleanup, 1000);
    })();
    </script>"""
    st.html(script.replace("__BOARD_TOKEN__", token), unsafe_allow_javascript=True)
