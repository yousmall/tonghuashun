"""普通日净值保留精度，事件日沿用披露增长率，按共同日期比较复利表现。"""
from __future__ import annotations

import asyncio
import math
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from backend.app.models.schemas import DataOverviewRequest
from backend.app.data_provider.fund_reference import _js_value, _numeric, _fact, TZ

CALCULATION_VERSION = "fund-nav-event-reinvestment-v2"


class FundAnalyticsRequest(BaseModel):
    targets: list[str] = Field(min_length=1, max_length=4)
    years: Literal[1, 3, 5] = 1
    risk_free_rate: float = Field(default=0, ge=0, le=20, allow_inf_nan=False)
    refresh: bool = False

    @field_validator("targets")
    @classmethod
    def clean_targets(cls, values):
        values = [value.strip() for value in values]
        if any(not value or len(value) > 60 for value in values) or len({value.casefold() for value in values}) != len(values):
            raise ValueError("请输入1–4个不同的基金名称或代码，每项最多60字")
        return values


def parse_daily_returns(content, code, *, now):
    if _js_value(content, "fS_code") != code:
        return []
    name, raw = _js_value(content, "fS_name"), _js_value(content, "Data_netWorthTrend")
    if not isinstance(name, str) or not isinstance(raw, list):
        return []
    values, conflicts = {}, set()
    today = now.astimezone(TZ).date()
    for row in raw[-2200:]:
        if not isinstance(row, dict):
            continue
        try:
            day = datetime.fromtimestamp(float(row["x"])/1000, TZ).date()
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
        nav, growth = _numeric(row.get("y")), _numeric(row.get("equityReturn"))
        if day > today or nav is None or nav <= 0 or growth is None or growth <= -100:
            continue
        item = {"date": day.isoformat(), "nav": nav, "growth_pct": growth,
                "event": str(row.get("unitMoney") or "")[:160]}
        if day in values and values[day] != item:
            conflicts.add(day)
        values[day] = item
    points = [item for day, item in sorted(values.items()) if day not in conflicts]
    if len(points) < 2:
        return []
    return [_fact(code, name, "fund_daily_returns", points, f"https://fund.eastmoney.com/{code}.html", now,
                  period=points[0]["date"] + "/" + points[-1]["date"])]


def _anniversary(day, years):
    try:
        return day.replace(year=day.year-years)
    except ValueError:
        return day.replace(year=day.year-years, day=28)


def calculate_comparison(sources, *, years, risk_free_rate):
    """普通日用相邻净值比保留精度，事件日沿用源增长率，再对齐共同日期。"""
    usable, failures = [], []
    for source in sources:
        points = source.get("points") or []
        if not points:
            failures.append({"target": source["target"], "message": source.get("message") or "未取得可复核日增长率。"})
            continue
        usable.append(source)
    if not usable:
        return {"status": "unavailable", "items": [], "failures": failures}
    # 先排除自身不满足完整年窗口的基金，不能把5年对比偷偷缩短为新基金成立以来。
    latest = min(date.fromisoformat(source["points"][-1]["date"]) for source in usable)
    wanted = _anniversary(latest, years)
    selected = []
    for source in usable:
        if date.fromisoformat(source["points"][0]["date"]) > wanted:
            failures.append({"target": source["target"], "message": f"不足完整{years}年历史。"})
        else:
            selected.append(source)
    if not selected:
        return {"status": "empty", "items": [], "failures": failures}
    common = set.intersection(*(set(point["date"] for point in source["points"]) for source in selected))
    if not common:
        return {"status": "empty", "items": [], "failures": failures, "message": "没有共同观测日期。"}
    end = date.fromisoformat(max(common))
    wanted = _anniversary(end, years)
    anchors = [day for day in common if date.fromisoformat(day) <= wanted]
    if not anchors:
        return {"status": "empty", "items": [], "failures": failures, "message": "没有完整窗口的共同起点。"}
    start = max(anchors)
    dates = sorted(day for day in common if start <= day <= end.isoformat())
    if (wanted-date.fromisoformat(start)).days > 14 or len(dates) < 200*years:
        return {"status": "empty", "items": [], "failures": failures, "message": "共同日序列不足，未推算年化风险。"}
    if any((date.fromisoformat(b)-date.fromisoformat(a)).days > 14 for a,b in zip(dates, dates[1:])):
        return {"status": "empty", "items": [], "failures": failures, "message": "共同序列存在过长缺口，未按日年化。"}
    items = []
    rf_daily = (1+risk_free_rate/100)**(1/252)-1
    for source in selected:
        series = [point for point in source["points"] if start <= point["date"] <= end.isoformat()]
        wealth, index, previous, valid = {}, 1.0, None, True
        for point in series:
            growth = point["growth_pct"]/100
            if previous:
                # 非分红/折算日出现无法解释的价差，宁缺数据也不连出虚假的复利曲线。
                nav_return = point["nav"]/previous["nav"]-1
                if not point.get("event") and abs(nav_return-growth) > .003:
                    valid = False
                    break
                if not point.get("event"):
                    # 源日涨幅通常仅保留百分号内2位。普通日用同源净值之比，
                    # 避免多年的逐日舍入累积；分红/折算日仍使用披露增长率。
                    growth = nav_return
                index *= 1+growth
            if not math.isfinite(index) or index <= 0:
                valid = False
                break
            wealth[point["date"]] = index
            previous = point
        if not valid or any(day not in wealth for day in dates):
            failures.append({"target": source["target"], "message": "日增长率与净值序列存在未说明差异，未输出复利风险指标。"})
            continue
        values = [wealth[day] for day in dates]
        returns = [b/a-1 for a,b in zip(values, values[1:])]
        deviation = statistics.stdev(returns)
        peak, drawdown = values[0], 0.0
        for value in values:
            peak = max(peak, value)
            drawdown = max(drawdown, 1-value/peak)
        total = values[-1]/values[0]
        days = (end-date.fromisoformat(start)).days
        items.append({"target": source["target"], "code": source["code"], "name": source["name"],
                      "start_date": start, "end_date": end.isoformat(), "observations": len(values),
                      "total_return_pct": (total-1)*100,
                      "annual_return_pct": (total**(365.2425/days)-1)*100,
                      "max_drawdown_pct": drawdown*100, "volatility_pct": deviation*math.sqrt(252)*100,
                      "sharpe_ratio": (statistics.mean(returns)-rf_daily)/deviation*math.sqrt(252) if deviation > 1e-12 else None,
                      "source_url": source["source_url"],
                      "curve": [{"date": day, "value": value/values[0]*100} for day,value in zip(dates,values)]})
    return {"status": "ok" if items and not failures else "partial" if items else "empty",
            "items": items, "failures": failures, "years": years, "risk_free_rate": risk_free_rate,
            "start_date": start, "end_date": end.isoformat(), "annualization": 252,
            "calculation_version": CALCULATION_VERSION,
            "basis": "普通日按同源相邻净值比连乘；分红及份额折算日沿用披露增长率；共同日期取样",
            "notes": ["理论分红再投资口径，不计申购赎回费用和税费；事件日源增长率仍可能存在舍入误差。",
                      "收益率按各份额原计价币种，未做汇率换算；比较时应选择相同币种份额。",
                      "夏普对每252个共同观测收益年化，使用同一无风险利率；最大回撤按共同观测日期计算，不与网站未知口径直接混排。"]}


async def fetch_fund_analytics(cache, provider, request):
    async def load(target):
        if provider is None or not callable(getattr(provider, "get_fund_return_series", None)):
            return {"target": target, "message": "数据源暂不支持基金长期日增长率。"}
        result = await cache.get(provider, DataOverviewRequest(direction="fund", target=target, refresh=request.refresh),
            jobs=[("series", "长期日增长率", lambda: provider.get_fund_return_series(target))], namespace="fund_analytics")
        facts = [fact for section in result.sections if section.status == "ok" for fact in section.facts if fact.field == "fund_daily_returns"]
        if len(facts) != 1:
            return {"target": target, "message": "未取得唯一可核对的基金日增长率序列。"}
        fact = facts[0]
        return {"target": target, "code": fact.entity_code, "name": fact.entity, "points": fact.value,
                "source_url": fact.source_url}
    sources = await asyncio.gather(*(load(target) for target in request.targets))
    codes = set()
    for source in sources:
        code = str(source.get("code") or "").split(".")[0]
        if code and code in codes:
            source["points"], source["message"] = [], "解析后与另一基金为同一份额，请选择不同代码。"
        codes.add(code)
    return await asyncio.to_thread(calculate_comparison, sources, years=request.years, risk_free_rate=request.risk_free_rate)
