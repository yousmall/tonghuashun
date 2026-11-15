"""热门板块：把问财的板块涨跌幅、成交额与换手率整理成量能／走势描述。

只做数据描述与分档，不构成推荐、评级或买卖建议。供应商返回的板块集合由
问财「今日热门板块」口径决定，本模块不自行挑选或补造标的。
"""
from __future__ import annotations

from time import monotonic
from typing import Any, Callable
import math

import streamlit as st
from streamlit.runtime.scriptrunner import get_script_run_ctx
from frontend.financial_view import fact_numeric_value, period_info
from frontend.snapshots import poll_snapshot
from backend.app.risk_questionnaire import local_today

CACHE_TTL = 60
HOT_SECTOR_QUERY = "今日热门板块 涨跌幅 成交额 换手率"
MAX_SECTORS = 6

# 平台分档口径：涨跌幅描述本期表现，换手率描述活跃度，不推断相对量能。
TREND_BANDS = ((2.0, "强势"), (0.5, "偏强"), (-0.5, "震荡"), (-2.0, "偏弱"))
VOLUME_BANDS = ((6.0, "高换手"), (3.0, "中等换手"), (0.0, "低换手"))

def _as_float(value: Any) -> float | None:
    """问财的数值有时是字符串或带千分位，统一转成浮点。"""

    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").strip())
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def format_amount(value: float | None) -> str:
    """成交额按中文习惯显示为亿／万亿，缺失时用破折号。"""

    if value is None:
        return "—"
    if value >= 1e12:
        return f"{value / 1e12:.2f} 万亿"
    if value >= 1e8:
        return f"{value / 1e8:.1f} 亿"
    if value >= 1e4:
        return f"{value / 1e4:.0f} 万"
    return f"{value:.0f}"


def trend_label(change: float | None) -> str:
    if change is None or not math.isfinite(change):
        return "数据不足"
    for threshold, label in TREND_BANDS:
        if change >= threshold:
            return label
    return "弱势"


def volume_label(rate: float | None) -> str:
    if rate is None or not math.isfinite(rate) or rate < 0:
        return "数据不足"
    for threshold, label in VOLUME_BANDS:
        if rate >= threshold:
            return label
    return "低换手"


def parse_sector_facts(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把扁平的事实列表按板块聚合成一行一个板块。"""

    grouped = {}
    for fact in facts:
        entity = fact.get("entity")
        field = fact.get("field")
        if not entity or not field:
            continue
        if field not in {"change", "turnover_value", "turnover_rate"}:
            continue
        observed = period_info(fact.get("period"))
        if observed and observed[1] > local_today():
            continue
        value = fact_numeric_value(fact)
        if value is None or field != "change" and value < 0:
            continue
        if field == "turnover_value":
            declared = str(fact.get("unit") or "")
            source = str(fact.get("source_field") or "")
            value *= 1e8 if declared == "亿元" or not declared and "亿元" in source else 1e4 if declared == "万元" or not declared and "万元" in source else 1
        identity = fact.get("entity_code") or str(entity)
        row = grouped.setdefault(identity, {"name":str(entity),"code":fact.get("entity_code"),"values":{}})
        row["values"].setdefault(field,[]).append((observed[1].isoformat() if observed else None,value))
    rows = []
    for entry in grouped.values():
        changes = entry["values"].get("change",[])
        if not changes:
            continue
        day = max((d or "" for d,_ in changes),default="") or None
        row = {"name":entry["name"],"code":entry["code"],"date":day}
        for field,label in (("change","change"),("turnover_value","turnover"),("turnover_rate","rate")):
            values = [v for d,v in entry["values"].get(field,[]) if d == day]
            row[label] = values[0] if values and all(math.isclose(v,values[0],rel_tol=1e-8,abs_tol=1e-8) for v in values) else None
        if row["change"] is not None:
            rows.append(row)
    rows.sort(key=lambda row: row.get("turnover") or 0.0, reverse=True)
    return rows[:MAX_SECTORS]


def describe(row: dict[str, Any]) -> tuple[str, str, str]:
    trend = trend_label(row.get("change"))
    volume = volume_label(row.get("rate"))
    return trend, volume, "按本期涨跌幅与换手率绝对值分档；没有前期成交基准，不能据此判断放量、缩量或趋势延续。"


def _sector_result(raw):
    if not isinstance(raw,dict):
        return {"status":"unavailable","rows":[],"date":None,"message":"热门板块暂不可用。"}
    rows = parse_sector_facts(raw.get("facts") or [])
    return {"status":raw.get("status") or "empty","rows":rows,
            "date":next((row["date"] for row in rows if row.get("date")),None),"message":raw.get("message")}


def load_hot_sectors(api_base: str, api_request: Callable, *, refresh: bool = False) -> dict[str, Any]:
    """会话内缓存 60 秒；刷新按钮显式跳过缓存。"""

    cache = st.session_state.setdefault("hot_sector_cache", {})
    entry = cache.get(api_base)
    if entry and not refresh and monotonic() - entry["loaded_at"] < CACHE_TTL:
        return entry["result"]
    raw = api_request(api_base, "POST", "/data/fetch",
                      {"kind": "sector_screen", "target": HOT_SECTOR_QUERY})
    if not isinstance(raw, dict):
        result = {"status": "unavailable", "rows": [], "date": None,
                  "message": "热门板块暂不可用，请确认已登录且数据源已配置。"}
    else:
        rows = parse_sector_facts(raw.get("facts") or [])
        result = {
            "status": raw.get("status") or "empty",
            "rows": rows,
            "date": next((row["date"] for row in rows if row.get("date")), None),
            "message": raw.get("message"),
        }
    cache[api_base] = {"loaded_at": monotonic(), "result": result}
    while len(cache) > 4:
        cache.pop(next(iter(cache)))
    return result


def _render_sector(row: dict[str, Any]) -> None:
    change = row.get("change")
    trend, volume, sentence = describe(row)
    color = "red" if (change or 0) >= 0 else "green"
    code = f" `{row['code']}`" if row.get("code") else ""
    quote = f":{color}[{change:+.2f}%]" if change is not None else "—"
    parts = [quote, f"成交额 {format_amount(row.get('turnover'))}"]
    if row.get("rate") is not None:
        parts.append(f"换手率 {row['rate']:.2f}%")
    st.markdown(f"**{row['name']}**{code}")
    st.markdown("　".join(parts))
    st.markdown(f"换手活跃度：**{volume}** ｜ 走势：**{trend}**")
    st.caption(sentence)


@st.fragment(run_every=1)
def _wait_hot_sectors(signature, fetch):
    raw = poll_snapshot("hot_sectors",signature,fetch)
    if raw.get("status") != "loading" and not raw.get("refreshing"):
        context = get_script_run_ctx(suppress_warning=True)
        if context and getattr(context,"fragment_ids_this_run",None):
            st.rerun()


def render_hot_sectors(api_base: str, api_request: Callable, *, key: str = "market", factory=None) -> None:
    """在「相关资讯」之后展示问财口径的热门板块观察列表。"""

    with st.container(border=True, key="hot-sectors-box"):
        header = st.container(horizontal=True, vertical_alignment="center")
        with header:
            st.markdown("**热门板块 · 量能与走势**")
            st.space("stretch")
            refresh = st.button("刷新板块", key=f"hot_sectors_refresh_{key}", icon=":material/refresh:")
        if factory:
            signature = (api_base,HOT_SECTOR_QUERY)
            fetch = factory(api_base,"/data/fetch",{"kind":"sector_screen","target":HOT_SECTOR_QUERY})
            raw = poll_snapshot("hot_sectors",signature,fetch,refresh=refresh)
            result = _sector_result(raw)
            if raw.get("status") == "loading" or raw.get("refreshing"):
                st.caption("热门板块正在后台更新，可继续选择研究模式或提问。")
                _wait_hot_sectors(signature,fetch)
        else:
            result = load_hot_sectors(api_base, api_request, refresh=refresh)
        rows = result.get("rows") or []
        if not rows:
            if result.get("status") != "loading":
                st.caption(result.get("message") or "问财本次未返回热门板块数据。")
            return
        ups = sum(1 for row in rows if (row.get("change") or 0) > 0)
        downs = sum(1 for row in rows if (row.get("change") or 0) < 0)
        heavy = sum(1 for row in rows if volume_label(row.get("rate")) in {"高换手","中等换手"})
        date = f" · 数据日期 {result['date']}" if result.get("date") else ""
        st.caption(f"共 {len(rows)} 个板块，{ups} 涨 {downs} 跌，量能活跃 {heavy} 个{date}。按成交额从高到低排列。")
        columns = st.columns(3)
        for index, row in enumerate(rows):
            with columns[index % 3]:
                _render_sector(row)
        st.caption("活跃度按板块换手率绝对值分档（≥6% 高换手、3%–6% 中等换手、<3% 低换手），走势按本期涨跌幅分档"
                   "（≥2% 强势、0.5%–2% 偏强、±0.5% 内震荡、-2%–-0.5% 偏弱、≤-2% 弱势）。"
                   "板块集合由问财「今日热门板块」口径给出，本页不构成推荐或买卖建议。")
