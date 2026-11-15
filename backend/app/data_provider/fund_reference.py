"""有界读取公开基金档案；独立记录来源，不用另一网站悄悄覆盖问财事实。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

from backend.app.models import FactRecord
from backend.app.services.provider_errors import ProviderCallError

SOURCE = "PUBLIC_FUND_EASTMONEY_V1"
TZ = timezone(timedelta(hours=8))


def _numeric(value):
    try:
        result = float(str(value).replace(",", ""))
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _date(text):
    match = re.search(r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})", text)
    try:
        return datetime(*map(int, match.groups()), tzinfo=TZ).date().isoformat() if match else None
    except ValueError:
        return None


class _ProfileTable(HTMLParser):
    """兼容站点省略 </td> 的档案表格；不从页头或广告抽取基金身份。"""
    def __init__(self):
        super().__init__()
        self.active = False
        self.cell = None
        self.buffer = []
        self.label = None
        self.values = {}

    def flush(self):
        text = " ".join(" ".join(self.buffer).split())
        if self.cell == "th":
            self.label = text
        elif self.cell == "td" and self.label:
            self.values[self.label] = text
            self.label = None
        self.cell, self.buffer = None, []

    def handle_starttag(self, tag, attrs):
        if tag == "table" and "info" in dict(attrs).get("class", "").split():
            self.active = True
        if self.active and tag in {"th", "td"}:
            self.flush()
            self.cell = tag

    def handle_endtag(self, tag):
        if self.active and tag in {"th", "td", "tr", "table"}:
            self.flush()
            if tag == "table":
                self.active = False

    def handle_data(self, data):
        if self.active and self.cell:
            self.buffer.append(data)


def _fact(code, name, field, value, url, now, *, period=None, unit=None):
    key = json.dumps([code, field, period, value, url], ensure_ascii=False, sort_keys=True)
    return FactRecord(fact_id="FUND-PUBLIC-" + hashlib.sha256(key.encode()).hexdigest()[:24],
                      entity=name, entity_code=code, field=field, value=value, period=period,
                      unit=unit, source_field=field, source_id=SOURCE, source_url=url,
                      snapshot_time=now, quality=.9)


def parse_fund_profile(content, code, *, now):
    parser = _ProfileTable()
    parser.feed(content)
    values = parser.values
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", values.get("基金代码", ""))
    name = values.get("基金简称")
    if not match or match[1] != code or not name:
        return []
    url = f"https://fundf10.eastmoney.com/jbgk_{code}.html"
    facts = []
    for label, field in (("基金类型", "fund_type"), ("基金管理人", "fund_company"),
                         ("基金托管人", "fund_custodian"), ("基金经理人", "fund_manager"),
                         ("业绩比较基准", "performance_benchmark"), ("跟踪标的", "tracking_index")):
        value = values.get(label)
        if value and value not in {"---", "暂无数据", "该基金无跟踪标的"}:
            facts.append(_fact(code, name, field, value, url, now))
    inception = _date(values.get("成立日期/规模", ""))
    if inception:
        facts.append(_fact(code, name, "reported_inception_date", inception, url, now))
    raw_size = values.get("净资产规模", "")
    size = re.search(r"([\d,.]+)\s*亿元", raw_size)
    period = _date(raw_size)
    if size and period and period <= now.astimezone(TZ).date().isoformat():
        facts.append(_fact(code, name, "reported_fund_size", float(size[1].replace(",", "")) * 1e8,
                           url, now, period=period, unit="CNY"))
    return facts


def _js_value(content, variable):
    match = re.search(r"\bvar\s+" + re.escape(variable) + r"\s*=\s*", content)
    if not match:
        return None
    try:
        # 只解析 JSON 字面值，不执行网页 JavaScript。
        return json.JSONDecoder().raw_decode(content, match.end())[0]
    except (ValueError, json.JSONDecodeError):
        return None


def parse_fund_trend(content, code, *, now):
    if _js_value(content, "fS_code") != code:
        return []
    if _js_value(content, "ishb") is True:
        return parse_money_fund_trend(content, code, now=now)
    name = _js_value(content, "fS_name")
    points = _js_value(content, "Data_netWorthTrend")
    if not isinstance(name, str) or not isinstance(points, list) or not points:
        return []
    point = points[-1]
    try:
        period = datetime.fromtimestamp(float(point["x"]) / 1000, TZ).date().isoformat()
        nav = _numeric(point["y"])
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return []
    if period > now.astimezone(TZ).date().isoformat() or nav is None or nav <= 0:
        return []
    url = f"https://fund.eastmoney.com/{code}.html"
    facts = [_fact(code, name, "reference_fund_nav", nav, url, now, period=period)]
    change = _numeric(point.get("equityReturn"))
    if change is not None:
        facts.append(_fact(code, name, "reference_nav_change", change, url, now, period=period, unit="percent"))
    accumulated = _js_value(content, "Data_ACWorthTrend")
    if isinstance(accumulated, list) and accumulated and isinstance(accumulated[-1], list) and len(accumulated[-1]) == 2:
        stamp, amount = accumulated[-1]
        amount = _numeric(amount)
        if stamp == point["x"] and amount is not None and amount > 0:
            facts.append(_fact(code, name, "reference_accumulated_nav", amount, url, now, period=period))
    for variable, field in (("syl_1y", "return_1m"), ("syl_3y", "return_3m"),
                             ("syl_6y", "return_6m"), ("syl_1n", "return_1y")):
        value = _numeric(_js_value(content, variable))
        if value is not None:
            facts.append(_fact(code, name, "reference_" + field, value, url, now, period=period, unit="percent"))
    allocation = _js_value(content, "Data_assetAllocation")
    if isinstance(allocation, dict) and isinstance(allocation.get("categories"), list) and allocation["categories"]:
        period = _date(str(allocation["categories"][-1]))
        if period and period <= now.astimezone(TZ).date().isoformat():
            for series in allocation.get("series", []):
                if not isinstance(series, dict):
                    continue
                field = {"股票": "stock_position", "股票占净比": "stock_position",
                         "债券": "bond_position", "债券占净比": "bond_position",
                         "现金": "cash_position", "现金占净比": "cash_position"}.get(series.get("name"))
                values = series.get("data")
                value = _numeric(values[-1]) if isinstance(values, list) and len(values) == len(allocation["categories"]) else None
                if field and value is not None and value >= 0:
                    facts.append(_fact(code, name, field, value, url, now, period=period, unit="percent"))
    return facts


def parse_money_fund_trend(content, code, *, now):
    """货币基金用万份收益和七日年化，不构造净值或把短期年化当作全年回报。"""
    if _js_value(content, "fS_code") != code or _js_value(content, "ishb") is not True:
        return []
    name = _js_value(content, "fS_name")
    if not isinstance(name, str) or not name:
        return []
    today = now.astimezone(TZ).date().isoformat()
    url, facts, periods = f"https://fund.eastmoney.com/{code}.html", [], []
    for variable, field, history, unit in (
        ("Data_sevenDaysYearIncome", "yield_7d", "money_yield_history", "percent"),
        ("Data_millionCopiesIncome", "income_per_10000", "money_income_history", "CNY")):
        raw = _js_value(content, variable)
        values, conflicts = {}, set()
        for row in raw if isinstance(raw, list) else []:
            if not isinstance(row, list) or len(row) != 2:
                continue
            try:
                day = datetime.fromtimestamp(float(row[0])/1000, TZ).date().isoformat()
            except (TypeError, ValueError, OverflowError, OSError):
                continue
            value = _numeric(row[1])
            if day > today or value is None:
                continue
            if day in values and values[day] != value:
                conflicts.add(day)
            values[day] = value
        points = [{"date":day,"value":value} for day,value in sorted(values.items()) if day not in conflicts][-30:]
        if not points:
            continue
        period = points[-1]["date"]
        periods.append(period)
        facts.append(_fact(code,name,field,points[-1]["value"],url,now,period=period,unit=unit))
        facts.append(_fact(code,name,history,points,url,now,period=points[0]["date"]+"/"+period,unit=unit))
    # 不同指标日期仍分别保留；未取得收益序列时不替网站阶段收益编造日期。
    if periods:
        for variable, field in (("syl_1y","return_1m"),("syl_3y","return_3m"),
                                ("syl_6y","return_6m"),("syl_1n","return_1y")):
            value = _numeric(_js_value(content,variable))
            if value is not None:
                facts.append(_fact(code,name,"reference_"+field,value,url,now,period=max(periods),unit="percent"))
    return facts


async def fetch_fund_reference(recovery, target):
    match = re.fullmatch(r"(\d{6})(?:\.(?:SH|SZ|OF))?", target, re.I)
    if not match:
        return []
    code = match[1]
    now = recovery.now()
    urls = [f"https://fundf10.eastmoney.com/jbgk_{code}.html", f"https://fund.eastmoney.com/pingzhongdata/{code}.js",
            f"https://fund.eastmoney.com/{code}.html", f"https://fund.10jqka.com.cn/{code}/portfolioindex.html"]
    payloads = await asyncio.gather(*(recovery._get(url) for url in urls), return_exceptions=True)
    facts = []
    from backend.app.data_provider.fund_details import parse_fund_home, parse_ths_portfolio
    for parse, payload in zip((parse_fund_profile, parse_fund_trend, parse_fund_home, parse_ths_portfolio), payloads):
        if isinstance(payload, str):
            try:
                facts.extend(parse(payload, code, now=now))
            except (TypeError, KeyError, ValueError):
                continue
    if not facts and all(isinstance(payload, BaseException) for payload in payloads):
        raise ProviderCallError("公开基金档案暂不可用，问财资料仍可单独查看。", code="FUND_REFERENCE_UNAVAILABLE")
    # 另一个公开页面补充了债券代码时，保留带代码且日期不旧的完整记录。
    bonds = [fact for fact in facts if fact.field == "bond_holdings"]
    if len(bonds) > 1:
        chosen = max(bonds, key=lambda fact: (fact.period or "", all(row.get("证券代码") != "未提供" for row in fact.value)))
        facts = [fact for fact in facts if fact.field != "bond_holdings" or fact is chosen]
    return [fact.model_copy(update={"entity_code": target.upper()}) for fact in facts]
