"""基金重仓、同类表现及行业配置的公开原始资料解析。"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from html import unescape
from datetime import date

from backend.app.data_provider.fund_reference import _fact, _date, _numeric, TZ


class TableRows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell, self.links = [], [], None, []

    def finish_cell(self):
        if self.cell is not None:
            self.row.append({"text": " ".join(" ".join(self.cell).split()), "links": self.links})
        self.cell, self.links = None, []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.finish_cell()
            if self.row:
                self.rows.append(self.row)
            self.row = []
        elif tag in {"th", "td"}:
            self.finish_cell()
            self.cell = []
        elif tag == "a" and self.cell is not None:
            self.links.append(dict(attrs).get("href", ""))

    def handle_endtag(self, tag):
        if tag in {"th", "td", "tr", "table"}:
            self.finish_cell()
        if tag in {"tr", "table"} and self.row:
            self.rows.append(self.row)
            self.row = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def parse_fund_home(content, code, *, now):
    title = re.search(r"<title>(.*?)</title>", content, re.S | re.I)
    identity = re.search(r"^(.*?)\((\d{6})\)", title[1].strip()) if title else None
    if not identity or identity[2] != code:
        return []
    name = identity[1]
    url = f"https://fund.eastmoney.com/{code}.html"
    today = now.astimezone(TZ).date().isoformat()
    facts = []
    performance_date = re.search(r'id=["\x27]IncreaseAmount["\x27].{0,1600}?data-date=["\x27](\d{4}-\d{2}-\d{2})', content, re.S)
    for match in re.finditer(r"<table\b[^>]*>.*?</table>", content, re.S | re.I):
        parser = TableRows()
        parser.feed(match[0])
        rows = parser.rows
        if not rows:
            continue
        headers = [cell["text"] for cell in rows[0]]
        if "持仓占比" in headers and any(label in headers for label in ("股票名称", "债券名称")):
            period = _date(re.sub(r"<[^>]+>", " ", content[match.end():match.end()+1400]))
            if not period or period > today:
                continue
            weight_index = headers.index("持仓占比")
            positions = []
            for cells in rows[1:]:
                if len(cells) <= weight_index or not cells[0]["text"]:
                    continue
                weight = _numeric(cells[weight_index]["text"].replace("%", ""))
                if weight is None or not 0 <= weight <= 100:
                    continue
                codes = [re.search(r"(?:/r/\d+\.|(?:sh|sz))([A-Za-z0-9]{5,9})(?:[/?\".]|$)", link, re.I)
                         for link in cells[0]["links"]]
                symbol = next((m[1] for m in codes if m), None)
                positions.append({"证券代码": symbol or "未提供", "证券名称": cells[0]["text"], "占净值比例(%)": weight})
            if positions:
                field = "top_holdings" if "股票名称" in headers else "bond_holdings"
                fact = _fact(code, name, field, positions[:10], url, now, period=period)
                facts.append(fact)
                if field == "top_holdings" and len(positions) == 10:
                    facts.append(_fact(code, name, "top10_weight", sum(p["占净值比例(%)"] for p in positions),
                                       url, now, period=period, unit="percent").model_copy(update={
                        "derived_from": [fact.fact_id], "derivation_rule": "SUM_DISCLOSED_TOP10_WEIGHTS_V1"}))
        if "阶段涨幅" not in match[0] or "同类平均" not in match[0] or "同类排名" not in match[0] or not performance_date:
            continue
        period = _date(performance_date[1])
        if not period or period > today or not any("近1月" == header for header in headers):
            continue
        while headers and not headers[0]:
            headers = headers[1:]
        data = {}
        for cells in rows[1:]:
            if cells:
                raw_label = cells[0]["text"]
                label = next((key for key in ("阶段涨幅", "同类平均", "同类排名", "四分位排名")
                              if raw_label.startswith(key)), raw_label)
                data[label] = cells[1:]
        own, average, rank = data.get("阶段涨幅", []), data.get("同类平均", []), data.get("同类排名", [])
        benchmark_label = next((label for label in data if label not in {"阶段涨幅", "同类平均", "同类排名", "四分位排名"}), None)
        benchmark = data.get(benchmark_label, [])
        records = []
        for index, label in enumerate(headers):
            if index >= len(own):
                continue
            value = _numeric(own[index]["text"].replace("%", ""))
            if value is None:
                continue
            record = {"区间": label, "基金收益率(%)": value,
                      "同类平均(%)": _numeric(average[index]["text"].replace("%", "")) if index < len(average) else None}
            if index < len(benchmark):
                record["基准名称"] = benchmark_label
                record["基准收益率(%)"] = _numeric(benchmark[index]["text"].replace("%", ""))
            if index < len(rank):
                ranking = re.search(r"(\d+)\s*[/|]\s*(\d+)", rank[index]["text"])
                if ranking and 0 < int(ranking[1]) <= int(ranking[2]):
                    record.update({"同类名次": int(ranking[1]), "同类总数": int(ranking[2])})
            records.append(record)
            field = {"近1月": "return_1m", "近3月": "return_3m", "近6月": "return_6m",
                     "今年来": "return_ytd", "近1年": "return_1y", "近3年": "return_3y"}.get(label)
            if field:
                facts.append(_fact(code, name, "reference_" + field, value, url, now, period=period, unit="percent"))
        if records:
            facts.append(_fact(code, name, "fund_peer_comparison", records, url, now, period=period))
    return facts


def parse_industry_allocation(content, code, *, now):
    try:
        # 官方页面通过 JSONP 读取；只解包固定包装，不执行脚本。
        text = content.strip()
        if not text.startswith("{"):
            match = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\((.*)\);?", text, re.S)
            text = match[1] if match else ""
        payload = json.loads(text)
    except (ValueError, TypeError):
        return []
    data = payload.get("Data")
    if payload.get("ErrCode") != 0 or not isinstance(data, dict):
        return []
    actual = str(data.get("FundCode") or data.get("FCODE") or "")
    if actual and actual != code:
        return []
    records = []
    for quarter in data.get("QuarterInfos", []):
        if not isinstance(quarter, dict):
            continue
        year = str(data.get("Curyear") or "")
        q = str(quarter.get("Quarter") or "")
        try:
            month = int(q)*3
            if month not in {3, 6, 9, 12}:
                continue
            period = date(int(year), month, {3:31, 6:30, 9:30, 12:31}[month]).isoformat()
        except ValueError:
            continue
        if period > now.astimezone(TZ).date().isoformat():
            continue
        industries = []
        for item in quarter.get("HYPZInfo", []):
            value = _numeric(str(item.get("ZJZBLDesc") or "").replace("%", ""))
            if item.get("HYMC") and value is not None and 0 <= value <= 200:
                industries.append({"行业代码": str(item.get("HYDM") or "未提供"), "行业名称": item["HYMC"],
                                   "占净值比例(%)": value, "市值": str(item.get("SZDesc") or "未提供")})
        if industries:
            records.append((period, industries))
    if not records:
        return []
    period, industries = max(records, key=lambda item: item[0])
    return [_fact(code, str(data.get("FundName") or code), "industry_allocation", industries,
                  f"https://fundf10.eastmoney.com/hytz_{code}.html", now, period=period)]


class _IndustryList(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth, self.mode, self.buffer, self.name = 0, None, [], None
        self.records = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div":
            if attrs.get("id") == "industryList":
                self.depth = 1
            elif self.depth:
                self.depth += 1
        if self.depth and ((tag == "p" and "r-title" in attrs.get("class", "").split()) or
                           (tag == "span" and "percent" in attrs.get("class", "").split())):
            self.mode, self.buffer = tag, []

    def handle_data(self, data):
        if self.depth and self.mode:
            self.buffer.append(data)

    def handle_endtag(self, tag):
        if self.mode == tag:
            text = " ".join(" ".join(self.buffer).split())
            if tag == "p":
                self.name = text
            else:
                weight = _numeric(text.replace("%", ""))
                if self.name and weight is not None and 0 <= weight <= 200:
                    self.records.append({"行业名称": self.name, "占净值比例(%)": weight})
                self.name = None
            self.mode = None
        if tag == "div" and self.depth:
            self.depth -= 1


def parse_ths_portfolio(content, code, *, now):
    identity = re.search(r'mobileurl\s*=\s*["\x27]https://fund\.10jqka\.com\.cn/[^"\x27]+#/(\d{6})', content)
    title = re.search(r"<title>(.*?)</title>", content, re.S)
    if not identity or identity[1] != code or not title:
        return []
    name = unescape(title[1].strip())
    url = f"https://fund.10jqka.com.cn/{code}/portfolioindex.html"
    today = now.astimezone(TZ).date().isoformat()
    facts = []
    position = content.find("industryList")
    dates = re.findall(r"数据更新至\s*(\d{4}-\d{2}-\d{2})", content[max(0,position-5000):position]) if position >= 0 else []
    if dates and _date(dates[-1]) and dates[-1] <= today:
        parser = _IndustryList()
        parser.feed(content)
        if parser.records:
            facts.append(_fact(code, name, "industry_allocation", parser.records, url, now, period=dates[-1]))
    position = content.find("债券代码")
    if position >= 0:
        dates = re.findall(r"数据更新至\s*(\d{4}-\d{2}-\d{2})", content[max(0,position-2200):position])
        if dates and _date(dates[-1]) and dates[-1] <= today:
            block = content[position:]
            end = re.search(r'<div\b[^>]*class=["\x27]dbox', block)
            block = block[:end.start()] if end else block[:20000]
            bonds = []
            for raw in re.findall(r'<ul\b[^>]*class=["\x27]data["\x27][^>]*>(.*?)</ul>', block, re.S):
                cells = [unescape(re.sub(r"<[^>]+>", "", cell)).strip() for cell in re.findall(r"<li\b[^>]*>(.*?)</li>", raw, re.S)]
                if len(cells) >= 6 and re.fullmatch(r"[A-Za-z0-9.]{5,12}", cells[1]):
                    weight = _numeric(cells[5].replace("%", ""))
                    if weight is not None and 0 <= weight <= 100:
                        bonds.append({"证券代码": cells[1], "证券名称": cells[2], "占净值比例(%)": weight})
            if bonds:
                facts.append(_fact(code, name, "bond_holdings", bonds[:10], url, now, period=dates[-1]))
    return [fact.model_copy(update={"source_id": "PUBLIC_FUND_THS_V1"}) for fact in facts]
