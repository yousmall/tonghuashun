"""Focused automatic Q&A over authorized, timestamped research facts."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import partial
import re
from math import isfinite
from uuid import uuid4
from zoneinfo import ZoneInfo

from backend.app.agents.coordinator import verify_facts
from backend.app.fact_taxonomy import FIELD_LABELS
from backend.app.models import (
    AdvicePackage, AgentResult, ComplianceResult, ComplianceStatus, CrossValidationIssue,
    CrossValidationResult, DataAcquisitionResult, FactRecord, Intent, TaskPlan, TaskStatus,
)
from backend.app.research_routing import user_question
from backend.app.risk_questionnaire import assessment_is_current
from backend.app.semantic import RequestUnderstanding
from backend.app.services.comparison import matching_fact
from backend.app.services.disclosure_reader import allowed_disclosure_url, canonical_disclosure_url, extract_pdf, read_disclosure
from backend.app.services.official_disclosures import resolve_official_mirror
from backend.app.services.provider_errors import ProviderCallError
from backend.app.services.local_consultation import observation_value, select_scope
from backend.app.services.research import DataCall


from backend.app.services.security_identity import STOCK_NAMES, STOCK_CODE, _market_suffix, _stock_symbols

COMPARISON = re.compile(r"比较|对比|相比|(?:和|跟|与).{1,35}比")
REFERENCE = re.compile(r"它|该股|这只|这家公司|当前(?:股票|标的)|^(?:那|再看|只看)(?:现在|估值|市盈率|财务|营收|归母|利润|去年|上年)")
ANNUAL = ("revenue", "net_profit", "revenue_growth", "net_profit_growth")
DEFAULT_FIELDS = ("close_price", "pe_ttm", "revenue_growth")
QUOTES = {"close_price", "change", "turnover_rate"}
MONEY = {"revenue", "net_profit", "close_price"}
CNY_UNITS = {"CNY": 1, "元": 1, "人民币": 1, "万元": 10_000, "亿元": 100_000_000}
METRICS = (
    (r"市盈率(?:\s*TTM)?|估值|\bPE\b", "pe_ttm"), (r"市净率|\bPB\b", "pb"),
    (r"加权(?:净资产收益率|ROE)", "roe_weighted"), (r"(?<!加权)(?:净资产收益率|\bROE\b)", "roe"),
    (r"营收同比|营业收入同比", "revenue_growth"), (r"(?:归母)?净利润同比", "net_profit_growth"),
    (r"(?:营收|营业收入)(?!同比)", "revenue"), (r"(?:归母)?净利润(?!同比)", "net_profit"),
    (r"股价|最新价|收盘价|行情", "close_price"), (r"涨跌幅", "change"), (r"换手率", "turnover_rate"),
)
SOURCES = re.compile(r"第\s*([1-9]\d?|[一二三四五六七八九十])\s*(?:项|条|行)(?:数据|数字|指标)?\s*(?:的)?(?:来源|出自|来自|出处|在哪|哪一页|哪里|原文)")
CHINESE_NUMBERS = "一二三四五六七八九十"
SCREEN = re.compile(r"(?:请)?(?:筛选|找出|查找)\s*(?:A股中?\s*)?(?:ROE|净资产收益率)\s*(?:超过|大于|高于)\s*(\d{1,3}(?:\.\d{1,2})?)\s*[%％]\s*(?:的)?\s*非\s*ST\s*股票[。？！?!]?", re.I)
HISTORY_REQUEST = re.compile(r"历史|季度|中报|半年报|一季报|三季报|20\d{2}年?\d{1,2}月|20\d{2}[-/]\d{2}|近[一二三四五六七八九十\d]+(?:年|月|日|天)|截至|当时|去年|上年|20\d{2}\s*年")


@dataclass(frozen=True)
class StockQuestion:
    symbols: tuple[str, ...]
    fields: tuple[str, ...]
    year: int | None = None
    clarification: str = ""


def stock_question(request, now: datetime) -> StockQuestion | None:
    question = user_question(request.query)
    if re.search(r"基金|ETF|可转债|转债|指数|行业板块", question, re.I):
        return None
    stock_page = request.research_direction == "个股研究"
    explicit = _stock_symbols(question, request.facts, stock_page=stock_page)
    compare = bool(COMPARISON.search(question))
    previous = ()
    turns = request.context_messages
    if turns and turns[-1].role == "user" and user_question(turns[-1].content) == question:
        turns = turns[:-1]
    for turn in turns:
        if turn.role == "user":
            text = user_question(turn.content)
            symbols = _stock_symbols(text, request.facts, stock_page=stock_page)
            if symbols:
                previous = (*previous, *symbols) if COMPARISON.search(text) and len(symbols) == 1 else symbols
                previous = tuple(dict.fromkeys(previous))
    symbols = explicit
    if compare and len(explicit) == 1 and len(previous) == 1:
        symbols = tuple(dict.fromkeys((*previous, *explicit)))
    elif not explicit and REFERENCE.search(question):
        symbols = (_stock_symbols(request.research_target or "", request.facts, stock_page=True)
                   if stock_page and "当前" in question else previous)
        if not compare and len(symbols) > 1:
            return StockQuestion((), (), clarification="上轮涉及多只股票，请明确这次要查看的名称或代码。")
    if not symbols:
        return None
    if any(match[2] and match[2].upper() != _market_suffix(match[1]) for match in STOCK_CODE.finditer(question)):
        return StockQuestion((), (), clarification="股票代码与交易所后缀不一致，请核对代码。")
    if len(symbols) > 4 or (compare and len(symbols) < 2):
        return StockQuestion((), (), clarification="请给出 2 至 4 个不同股票的名称或代码，再比较同一口径的资料。")
    if "公告" in question and not re.search(r"推荐|买入|卖出|目标价|收益预测", question):
        if len(symbols) == 1 and not HISTORY_REQUEST.search(question):
            return StockQuestion(symbols, ("announcement",))
        return StockQuestion(symbols, (), clarification="目前可核对单只股票近期公告，请明确一只股票的名称或代码。")
    if re.search(r"推荐|买入|卖出|目标价|收益预测|研报|主营|治理|原因|为什么|走势|分红|现金流|毛利率|净利率|动态市盈率|静态市盈率", question):
        return StockQuestion(symbols, (), clarification="本轮自动问答支持行情、常见估值及年度财务指标。涉及公告、原因或投资判断时，请使用个股页的完整深入报告或推荐入口。")
    current_year = now.astimezone(ZoneInfo("Asia/Shanghai")).year
    years = set(re.findall(r"(?<!\d)(20\d{2})(?!\d)", question))
    relative_year = current_year if "今年" in question else current_year - (2 if "前年" in question else 1)
    year = int(next(iter(years))) if len(years) == 1 else relative_year
    annual = bool(re.search(r"年报|年度|全年|去年|上年|前年", question))
    if (len(years) > 1 or (annual and year >= current_year) or (HISTORY_REQUEST.search(question) and not annual)
            or (annual and re.search(r"近[一二三四五六七八九十\d]+年|季度|中报|半年报|20\d{2}[-/]\d{2}", question))):
        return StockQuestion(symbols, (), clarification="请指定一个已结束的年度和要核对的财务指标，例如「2025 年报的营业收入」。")
    fields = tuple(field for pattern, field in METRICS if re.search(pattern, question, re.I))
    if annual:
        if set(fields) - {*ANNUAL, "roe", "roe_weighted"}:
            return StockQuestion(symbols, (), clarification="年度财报支持营业收入、归母净利润、同比增长及净资产收益率。请明确所需指标。")
        return StockQuestion(symbols, fields or ANNUAL[:2], year)
    if any(field in {"revenue", "net_profit"} for field in fields):
        return StockQuestion(symbols, (), clarification="营业收入和归母净利润需要明确报告期，例如「2025 年报的营业收入」。")
    return StockQuestion(symbols, fields or DEFAULT_FIELDS)


def _value(fact: FactRecord):
    if fact.field not in MONEY:
        return observation_value(fact)
    value = observation_value(fact)
    if value is None or fact.unit not in CNY_UNITS:
        return None
    normalized = value * CNY_UNITS[fact.unit]
    return normalized if isfinite(normalized) else None


def _period(fact: FactRecord, year: int | None) -> str | None:
    if year:
        text = str(fact.period or "").strip().upper()
        return str(year) if text in {
            f"{year}1231", f"{year}-12-31", f"{year}FY", f"FY{year}", f"{year}年报", f"{year}年度",
        } else None
    period = (fact.observation_date or fact.period) if fact.field in QUOTES else (fact.period or fact.observation_date)
    text = str(period or "").strip()
    return f"{text[:4]}-{text[4:6]}-{text[6:]}" if re.fullmatch(r"\d{8}", text) else text or None


async def _verified_facts(facts, now):
    probe = AgentResult(agent_id="security", status=TaskStatus.COMPLETED, opinion="问答数据核验",
                        confidence=0, facts_used=[f.fact_id for f in facts])
    verified, = await verify_facts([probe], facts, now=now)
    valid = set(verified.facts_used)
    return [fact for fact in facts if fact.fact_id in valid]


async def _select_facts(facts, plan: StockQuestion, now: datetime):
    buckets = {}
    for fact in await _verified_facts(facts, now):
        code = str(fact.entity_code or "").upper()
        symbol = code.split(".")[0]
        if (fact.field not in plan.fields or symbol not in plan.symbols
                or not matching_fact(symbol, fact) or code not in {symbol, f"{symbol}.{_market_suffix(symbol)}"}):
            continue
        period, value = _period(fact, plan.year), _value(fact)
        if value is not None and (period or not plan.year):
            buckets.setdefault((symbol, fact.field, period), []).append((value, fact))

    selected, missing, conflicts = [], [], []
    for field in plan.fields:
        periods = [{period for code, metric, period in buckets if code == symbol and metric == field}
                   for symbol in plan.symbols]
        comparable = set.intersection(*periods)
        if len(plan.symbols) > 1:
            comparable.discard(None)
        if not comparable:
            missing.append(field)
            continue
        period = max(comparable, key=lambda key: key or "")
        groups = [buckets[symbol, field, period] for symbol in plan.symbols]
        if any(len({value for value, _ in group}) > 1 for group in groups):
            conflicts.extend(f for group in groups for _, f in group)
            missing.append(field)
            continue
        selected.extend(max(group, key=lambda item: item[1].snapshot_time)[1] for group in groups)
    return selected, missing, conflicts


def _calls(plan: StockQuestion, missing: list[str]) -> list[DataCall]:
    calls = []
    for symbol in plan.symbols:
        if plan.year:
            method = "get_annual_financial_metrics"
            calls.append(DataCall(f"annual:{symbol}", method, (symbol, plan.year), f"{method}@{symbol}:{plan.year}"))
        else:
            if any(field in QUOTES for field in missing):
                calls.append(DataCall(f"quote:{symbol}", "get_quote", (symbol,), f"get_quote@{symbol}"))
            if any(field not in QUOTES for field in missing):
                calls.append(DataCall(f"financial:{symbol}", "get_financial_metrics", (symbol,), f"get_financial_metrics@{symbol}"))
    return calls


def _answer(intent, conclusion, *, rows=(), missing=(), conflicts=(), source=False):
    trace_id = "A-" + uuid4().hex[:12].upper()
    status = ComplianceStatus.REVIEW if missing or conflicts or not rows else ComplianceStatus.PASS
    issues = [CrossValidationIssue(code="AUTO_FACT_CONFLICT", severity="warning",
                                   message="同一标的和口径存在不同数值，相关指标已撤下。",
                                   fact_ids=[f.fact_id for f in conflicts])] if conflicts else []
    return AdvicePackage(
        trace_id=trace_id, intent=intent, research_mode="auto", conclusion=conclusion,
        response_scope="evidence_review" if rows else "clarification", confidence=min((f.quality for f in rows), default=0),
        scope_notice="以下为历史回答的原始引用。" if source else "自动问答按本轮问题核对资料。",
        evidence=[f.fact_id for f in rows], answer_fact_ids=[] if source else [f.fact_id for f in rows],
        source_reference=rows[0].fact_id if source and rows else None,
        snapshot_time=max((f.snapshot_time for f in rows), default=None),
        compliance=ComplianceResult(status=status, reason="已核对客观资料范围。"),
        task_plan=TaskPlan(trace_id=trace_id, intent=intent),
        cross_validation=CrossValidationResult(status=status, issues=issues),
    )


def source_item(question: str) -> int | None:
    match = SOURCES.search(user_question(question))
    if not match:
        return None
    return int(match[1]) if match[1].isdigit() else CHINESE_NUMBERS.index(match[1]) + 1


def _historical_source(history, number):
    payload = next((message["payload"] for message in reversed((history or {}).get("messages", []))
                    if message["role"] == "assistant" and (message.get("payload") or {}).get("answer_fact_ids")), {})
    identifiers = payload.get("answer_fact_ids") or []
    if number > len(identifiers) or (payload.get("compliance") or {}).get("status") == "BLOCK":
        return None
    fact_id = identifiers[number - 1]
    return next((FactRecord.model_validate(fact) for fact in payload.get("facts") or []
                 if fact.get("fact_id") == fact_id and fact_id in (payload.get("evidence") or [])), None)


def _reported_annual_value(fact, text):
    labels = {
        "revenue": r"营业收入(?:合计)?",
        "net_profit": r"归属于(?:上市公司股东|母公司(?:股东|所有者))的净利润|归母净利润",
    }
    label = labels.get(fact.field)
    if not label or fact.unit not in CNY_UNITS:
        return False
    page_declares_yuan = bool(re.search(r"单位\s*[：:]\s*(?:人民币\s*)?元", text))
    # 报表会将单位写在表头或指标名后。只核对指标行的首列，避免拿上年数值作证。
    values = re.findall(
        rf"(?:^|\n)[ \t]*(?:{label})[ \t]*(?:([（(](?:人民币)?元[）)]))?[ \t：:]*"
        r"([+-]?[\d,]+(?:\.\d+)?)(?=\s|$)", text,
    )
    try:
        expected = Decimal(str(fact.value).replace(",", "")) * CNY_UNITS[fact.unit]
        return any((page_declares_yuan or row_unit) and Decimal(value.replace(",", "")) == expected
                   for row_unit, value in values)
    except InvalidOperation:
        return False


async def _locate_annual_page(request, pipeline, fact):
    symbol = str(fact.entity_code or "").split(".")[0]
    match = re.match(r"^(20\d{2})", str(fact.period or ""))
    if (not request.auto_fetch or fact.field not in {"revenue", "net_profit"}
            or not STOCK_CODE.fullmatch(symbol) or not match or not _period(fact, int(match[1]))
            or not hasattr(pipeline.provider, "get_annual_report")):
        return [], DataAcquisitionResult()
    year = int(match[1])
    key = f"get_annual_report@{symbol}:{year}"
    name = next((label for label, code in STOCK_NAMES.items() if code == symbol), fact.entity)
    call = DataCall("annual_report", "get_annual_report", (symbol, year, name), key)
    prepared, audit = await pipeline.prepare(request.model_copy(update={"facts": []}), Intent.SECURITY_RESEARCH,
                                             planned_calls=[call])
    title = re.compile(rf"{year}\s*年\s*(?:年度报告|年报)(?:\s*全文)?[。\s]*$")
    documents = [item for item in await _verified_facts(prepared.facts, pipeline.now())
                 if item.field == "announcement" and item.observation_date and title.search(str(item.value))
                 and item.observation_date <= pipeline.now().date()
                 and (str(item.entity_code).upper() in {symbol, f"{symbol}.{_market_suffix(symbol)}"}
                      if item.entity_code else item.entity == fact.entity)]
    if not documents:
        return [], audit
    report = max(documents, key=lambda item: item.observation_date)
    url = canonical_disclosure_url(report.source_url or "")
    if allowed_disclosure_url(url):
        report = report.model_copy(update={"source_url": url})
    else:
        try:
            report = await asyncio.wait_for(resolve_official_mirror(report, code=symbol, name=fact.entity), timeout=6)
        except (ProviderCallError, TimeoutError):
            return [], audit
        if report is None:
            return [], audit
    try:
        excerpts = await asyncio.wait_for(read_disclosure(report, code=symbol, name=report.entity,
                                                          extractor=partial(extract_pdf, focus="financial")), timeout=16)
    except (ProviderCallError, TimeoutError):
        return [report], audit
    verified = await _verified_facts([report, *excerpts], pipeline.now())
    page = next((item for item in verified if item.field == "announcement_excerpt"
                 and _reported_annual_value(fact, str(item.value))), None)
    return [report, *[item for item in verified if page and item.fact_id in {*page.derived_from, page.fact_id}
                      and item.fact_id != report.fact_id]], audit


def _keep_references(request, advice):
    keep = set(advice.evidence) | {fid for issue in advice.cross_validation.issues for fid in issue.fact_ids}
    by_id = {f.fact_id: f for f in request.facts}
    pending = list(keep)
    while pending:
        for parent in by_id[pending.pop()].derived_from:
            if parent in by_id and parent not in keep:
                keep.add(parent)
                pending.append(parent)
    return request.model_copy(update={"facts": [f for f in request.facts if f.fact_id in keep]})


async def _screen(request, pipeline, threshold):
    intent = Intent.SECURITY_RESEARCH
    conditions = f"净资产收益率大于 {threshold:g}%，非 ST"
    key = f"stock_screen@roe:{threshold:g}:non-st"
    call = DataCall("stock_screen", "screen_stocks",
                    (f"{conditions}，返回股票简称、股票代码、净资产收益率及报告期、是否ST",), key)
    # Re-evaluate the vendor's conditions from its evidence, not from a match count.
    request = request.model_copy(update={"facts": []})
    prepared, audit = await pipeline.prepare(request, intent, planned_calls=[call])
    now = pipeline.now()
    groups = {}
    for fact in await _verified_facts(prepared.facts, now):
        code = str(fact.entity_code or "").upper()
        match = STOCK_CODE.fullmatch(code)
        if (match and (not match[2] or match[2] == _market_suffix(match[1]))
                and fact.field in {"roe", "is_st"}):
            groups.setdefault(match[1], []).append(fact)
    rows = []
    for facts in groups.values():
        risk = [f for f in facts if f.field == "is_st" and now - f.snapshot_time <= timedelta(days=1)]
        roe = [f for f in facts if f.field == "roe" and _period(f, None) and observation_value(f) is not None]
        if (not risk or not roe or any(f.entity.upper().startswith(("ST", "*ST")) for f in facts)
                or any(str(f.value).strip().casefold() not in {"false", "0", "否", "非st"} for f in risk)):
            continue
        newest = max(_period(f, None) for f in roe)
        current = [f for f in roe if _period(f, None) == newest]
        if len({observation_value(f) for f in current}) == 1 and observation_value(current[0]) > threshold:
            rows.extend((current[0], risk[0]))
        if len(rows) >= 20:
            break
    count = len(rows) // 2
    conclusion = (f"筛选条件为{conditions}。本次有 {count} 只候选的两个条件均有资料支持。"
                  if rows else f"筛选条件为{conditions}。本次没有同时取得两项有效证据的候选。")
    advice = _answer(intent, conclusion, rows=rows)
    return _keep_references(prepared, advice), audit, advice


async def _announcement(request, pipeline, symbol):
    intent = Intent.SECURITY_RESEARCH
    key = f"get_announcements@{symbol}"
    names = [name for name, code in STOCK_NAMES.items() if code == symbol]
    target = f"{names[0]} {symbol}" if names else symbol
    call = DataCall("announcement", "get_announcements", (target,), key)
    # Only allow a server-fetched disclosure to initiate a document download.
    prepared, audit = await pipeline.prepare(request.model_copy(update={"facts": []}), intent, planned_calls=[call])
    now = pipeline.now()
    documents = [f for f in await _verified_facts(prepared.facts, now)
                 if f.field in {"announcement", "announcement_summary"} and f.observation_date and f.source_url
                 and (str(f.entity_code).upper() in {symbol, f"{symbol}.{_market_suffix(symbol)}"}
                      if f.entity_code else f.entity in names)]
    if not documents:
        advice = _answer(intent, f"暂未取得 {symbol} 带日期和来源的公告。")
        return _keep_references(prepared, advice), audit, advice
    document = max(documents, key=lambda f: (f.observation_date, f.field == "announcement"))
    rows = [document]
    conclusion = f"已取得 {symbol} 的公告，披露日期为 {document.observation_date}。"
    question = user_question(request.query)
    missing = []
    if re.search(r"原文|哪一页|风险|影响", question):
        try:
            excerpts = await asyncio.wait_for(read_disclosure(document, code=symbol, name=document.entity), timeout=12)
        except (ProviderCallError, TimeoutError):
            excerpts = []
        if excerpts:
            prepared = prepared.model_copy(update={"facts": [*prepared.facts, *excerpts]})
        candidates = [f for f in await _verified_facts(prepared.facts, now) if f.field == "announcement_excerpt"]
        if re.search(r"风险|影响", question):
            candidates = [f for f in candidates if re.search(r"风险|影响", str(f.value))]
        if candidates:
            rows.extend(candidates[:2])
            conclusion += "下方列出了可定位的原文片段。"
        else:
            missing.append("原文片段")
            conclusion += " 暂未定位到相关原文段落。"
    advice = _answer(intent, conclusion, rows=rows, missing=missing)
    return _keep_references(prepared, advice), audit, advice


async def run_automatic(request, pipeline, instant, understanding, *, history=None, progress_sink=None):
    if (understanding.risk_rules or understanding.reason == "请求不在只读研究范围内"
            or understanding.intent is Intent.EDUCATION or not assessment_is_current(request.profile)):
        prepared, audit, advice = await instant.run(request, understanding, progress_sink=progress_sink)
        return prepared, audit, advice, understanding

    if request.comparison_targets:
        prepared, audit, advice = await instant.run(request, understanding, progress_sink=progress_sink)
        return prepared, audit, advice, understanding

    number = source_item(request.query)
    if number:
        fact = _historical_source(history, number)
        rows = [fact] if fact else []
        audit = DataAcquisitionResult()
        if fact:
            page = re.search(r"第\s*(\d+)\s*页", fact.source_field or "")
            location = f"原文第 {page[1]} 页。" if page else "这条资料未提供原文页码。"
            if not page:
                if progress_sink:
                    progress_sink("核对历史引用与年报")
                located, audit = await _locate_annual_page(request, pipeline, fact)
                rows.extend(located)
                if located:
                    page = (re.search(r"第\s*(\d+)\s*页", located[-1].source_field or "")
                            if located[-1].field == "announcement_excerpt" else None)
                    location = (f"同一数值已在官方年报 PDF 第 {page[1]} 页核对。" if page else
                                "已找到官方年报，暂未定位到能核对这个数值的页码。")
            conclusion = f"最近一份数据表第 {number} 项是{fact.entity}的{FIELD_LABELS.get(fact.field, fact.field)}。{location}"
        else:
            conclusion = "这段对话没有可核对的对应编号。请先打开一条带编号的数据回答。"
        advice = _answer(Intent.SECURITY_RESEARCH, conclusion, rows=rows, source=True)
        return request.model_copy(update={"facts": rows}), audit, advice, understanding

    screen = SCREEN.fullmatch(user_question(request.query))
    if screen:
        threshold = float(screen[1])
        intent = Intent.SECURITY_RESEARCH
        understanding = RequestUnderstanding(intent=intent, confidence=1, risk_rules=[], reason="核对客观选股条件。")
        if progress_sink:
            progress_sink("筛选并核对股票条件")
        prepared, audit, advice = await _screen(request, pipeline, threshold)
        return prepared, audit, advice, understanding

    now = pipeline.now()
    plan = stock_question(request, now)
    if not plan:
        direction = next((label for pattern, label in (
            (r"可转债|转债|转股溢价", "可转债分析"), (r"基金|ETF", "基金筛选"),
            (r"行业|板块", "行业分析"), (r"指数|大盘|宏观", "市场解读"),
        ) if re.search(pattern, user_question(request.query), re.I)), request.research_direction)
        scoped = request.model_copy(update={"research_direction": direction,
                                            "research_target": request.research_target if direction == request.research_direction else None})
        understanding = select_scope(scoped)
        prepared, audit, advice = await instant.run(scoped, understanding, progress_sink=progress_sink)
        return prepared, audit, advice, understanding

    intent = Intent.SECURITY_RESEARCH
    understanding = RequestUnderstanding(intent=intent, confidence=1, risk_rules=[], reason="按明确证券及财务口径核对资料。",
                                        target=plan.symbols[0] if len(plan.symbols) == 1 else None)
    if plan.clarification:
        advice = _answer(intent, plan.clarification)
        return request.model_copy(update={"facts": []}), DataAcquisitionResult(), advice, understanding

    if plan.fields == ("announcement",):
        if progress_sink:
            progress_sink("查找带日期的公告")
        prepared, audit, advice = await _announcement(request, pipeline, plan.symbols[0])
        return prepared, audit, advice, understanding

    if progress_sink:
        progress_sink("核对已有指标与报告期")
    rows, missing, conflicts = await _select_facts(request.facts, plan, now)
    prepared, audit = request, DataAcquisitionResult(mode="provided", supplied_fact_count=len(request.facts))
    if missing and request.auto_fetch:
        if progress_sink:
            progress_sink("补取本题缺少的资料")
        prepared, audit = await pipeline.prepare(request, intent, planned_calls=_calls(plan, missing))
        rows, missing, conflicts = await _select_facts(prepared.facts, plan, pipeline.now())

    scope = "、".join(next((name for name, code in STOCK_NAMES.items() if code == symbol), symbol)
                    + f"（{symbol}）" for symbol in plan.symbols)
    period = f"{plan.year} 年度" if plan.year else "已有有效时点"
    conclusion = f"暂未取得 {scope} 可核对的{period}资料。"
    if rows:
        if len(plan.symbols) == 1:
            fact = rows[0]
            label = FIELD_LABELS.get(fact.field, fact.field)
            unit = {"CNY": "元", "percent": "%"}.get(fact.unit, fact.unit or "")
            value = fact.value if fact.field in MONEY else observation_value(fact)
            if fact.normalized_value is not None:
                unit = "%"
            if fact.field in {"pe_ttm", "pb"} and not unit:
                unit = "倍"
            conclusion = f"{scope}的{label}为 {value}{unit}（第1项）。"
        else:
            first = rows[:len(plan.symbols)]
            label = FIELD_LABELS.get(first[0].field, first[0].field)
            highest = max(first, key=_value)
            ordered = sorted((_value(fact) for fact in first), reverse=True)
            finding = f"{highest.entity}的{label}最高" if ordered[0] > ordered[1] else f"{label}最高值并列"
            conclusion = f"{scope}的{period}资料已按同一口径对齐，{finding}（第1至{len(first)}项）。"
    if missing:
        labels = "、".join(FIELD_LABELS.get(field, field) for field in missing)
        conclusion += f" 仍缺少可按同一口径核对的{labels}。"
    advice = _answer(intent, conclusion, rows=rows, missing=missing, conflicts=conflicts)
    audit = audit.model_copy(update={"missing_fields_by_agent": {"security": missing} if missing else {}})
    return _keep_references(prepared, advice), audit, advice, understanding

