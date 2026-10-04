"""服务端按证据缺口选择问财只读能力，并核验模型的定性评估引用。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import defaultdict

from backend.app.fact_taxonomy import NEWS_FIELDS, fact_is_current
from backend.app.models import FactRecord, Intent
from backend.app.services.evidence_coverage import AGENT_FIELDS, missing_fields_by_agent
from backend.app.services.research import DataCall, _merge_by_fact_id
from backend.app.services.recommendation_recovery import derive_explicit_state, derive_history_metrics, history_window
from backend.app.services.return_expectation import build_return_expectation
from backend.app.services.scoring_dimensions import QUALITATIVE_RUBRICS
from backend.app.services.disclosure_reader import allowed_disclosure_url, read_disclosure
from backend.app.services.provider_errors import failure_summary
from backend.app.services.research_requirements import evidence_status
from backend.app.services.return_expectation import _date

DOCUMENT_FIELDS = {*NEWS_FIELDS, "event"}
CRITERIA = {"policy": ("policy",), "event": ("event",),
            "governance": ("audit_opinion", "regulatory_status", "disclosure_status")}
MAX_GAP_CALLS = 12
DIMENSION_TERMS = {
    "policy": ("政策", "财政", "货币", "税", "支持", "补贴", "监管"),
    "event": ("业绩", "增持", "回购", "分红", "解禁", "重组", "停牌", "增长"),
    "governance": ("审计", "监管", "披露", "年度报告", "年报", "处罚", "会计", "问询"),
}
CALL_FAMILIES = {
    "get_macro_policy": ("news",), "get_market_breadth": ("quote",),
    "get_industry_fundamentals": ("industry",), "get_industry_turnover_history": ("industry",),
    "get_industry_policy": ("news",), "get_governance_disclosures": ("event", "announcement"),
    "get_stock_disclosure_details": ("event", "announcement"),
    "get_institutional_research": ("institutional_research",), "get_research_reports": ("research_report",),
    "get_quote": ("quote",), "get_basic_info": ("basic_info",),
    "get_target_prices": ("institutional_research",), "get_industry_flow": ("industry",),
    "get_macro_data": ("macro",), "get_market_calendar": ("quote",),
    "get_financial_metrics": ("financial",), "get_event_data": ("event", "announcement"),
    "get_company_operations": ("company_operations",), "get_shareholder_equity": ("shareholder_equity",),
    "get_news": ("news",), "get_announcements": ("announcement", "event"),
    "get_industry_rank": ("industry",), "get_fund_candidates": ("fund",),
    "get_convertible_bond": ("convertible",),
}


def current_facts(pipeline, request):
    return [fact for fact in request.facts if fact_is_current(fact, pipeline.now()) and fact.quality >= .4]


def scopes(pipeline, request, intent, target):
    """从当前问题及返回的实体字段选取范围，不能把全部行业和股票拼成一个实体。"""
    current = current_facts(pipeline, request)
    by_entity = defaultdict(list)
    for fact in current:
        by_entity[fact.entity].append(fact)
    market_candidates = {fact.entity for fact in current if fact.field in {
        "pmi", "cpi", "ppi", "m2_growth", "growth_score", "inflation_score", "risk_appetite_score"}}
    market = max(sorted(market_candidates), key=lambda entity: len(
        {f.field for f in by_entity[entity]} & set(AGENT_FIELDS["market"])), default="中国宏观经济")
    verified_target = pipeline._validated_target(request, target)
    if not verified_target and intent in {Intent.SECURITY_RESEARCH, Intent.CONVERTIBLE_BOND_ANALYSIS}:
        verified_target = next((f.entity for f in current if f.field in {
            "close_price", "roe", "fundamental_score", "event_score", "governance_score"}
            and f.entity and f.entity in request.query), None)
    codes = re.findall(r"(?<!\d)\d{6}(?!\d)", verified_target or "")
    securities = []
    if intent in {Intent.SECURITY_RESEARCH, Intent.CONVERTIBLE_BOND_ANALYSIS} and verified_target:
        candidates = [fact for fact in current if (fact.entity == verified_target or
                      codes and str(fact.entity_code or "").split(".")[0] == codes[0])]
        named = [fact for fact in candidates if codes
                 and str(fact.entity_code or "").split(".")[0] == codes[0]
                 and not re.fullmatch(r"\d{6}(?:\.(?:SH|SZ|BJ))?", fact.entity.upper())]
        name = (named or candidates)[0].entity if candidates else verified_target
        securities = [(name, verified_target)]
    elif intent == Intent.PORTFOLIO_REVIEW:
        for holding in request.portfolio[:pipeline.max_portfolio_entities]:
            if isinstance(holding, dict):
                name = next((str(holding[k]).strip() for k in ("symbol", "code", "name", "entity") if holding.get(k)), "")
                if name:
                    matched = next((f.entity for f in current if f.entity == name or
                                    str(f.entity_code or "").split(".")[0] == name), name)
                    securities.append((matched, name))
        securities = securities[:2]
    relevant_names = {name for name, _ in securities}
    industries = {str(fact.value).strip() for fact in current if fact.field == "industry"
                  and fact.entity in relevant_names and isinstance(fact.value, str) and fact.value.strip()}
    if not industries and not securities:
        industries = {fact.entity for fact in current if (str(fact.entity_code or "").upper().endswith(".TI") or
                      fact.field in {"industry_revenue_growth", "industry_turnover_percentile", "prosperity_score", "crowding_score", "capital_flow_score"})}
    if intent == Intent.INDUSTRY_ANALYSIS and verified_target:
        industries = {verified_target}
    # 一次补取只深入一个与当前问题相关的行业，避免对排名表里每个行业抓取全文。
    industry = max(sorted(industries), key=lambda entity: len(
        {f.field for f in by_entity[entity]} & set(AGENT_FIELDS["industry"])), default=None)
    return market, industry, securities


def assessment_targets(pipeline, request, intent, target):
    market, industry, securities = scopes(pipeline, request, intent, target)
    gaps = missing_fields_by_agent(request.facts, intent, now=pipeline.now())
    targets = []
    if "policy_score" in gaps.get("market", []):
        targets.append({"entity": market, "dimension": "policy"})
    if industry and "policy_score" in gaps.get("industry", []):
        targets.append({"entity": industry, "dimension": "policy"})
    for name, _ in securities:
        fields = {fact.field for fact in current_facts(pipeline, request) if fact.entity == name}
        for dimension in ("event", "governance"):
            if dimension + "_score" not in fields:
                targets.append({"entity": name, "dimension": dimension})
    return targets


def gap_calls(pipeline, request, intent, target, audit):
    market, industry, securities = scopes(pipeline, request, intent, target)
    gaps = missing_fields_by_agent(request.facts, intent, now=pipeline.now())
    current = current_facts(pipeline, request)
    calls = []
    def add(label, method, *args):
        errors = audit.capability_errors
        families = CALL_FAMILIES.get(method, ())
        if (callable(getattr(pipeline.provider, method, None)) and
                not any(errors.get(family, {}).get("retryable") is False for family in families)):
            prefix = label.split(":")[0]
            priorities = {"security_scope": 100, "macro_liquidity": 90, "market_breadth": 90,
                "disclosure_details": 85, "return_targets": 85, "return_quote": 85,
                "industry_fundamentals": 80, "industry_flow": 80, "macro_policy": 75,
                "industry_policy": 75, "industry_history": 70, "trading_calendar": 70,
                "governance_documents": 60}
            required = {"security_scope": ("industry",), "macro_liquidity": ("m2_growth",),
                "market_breadth": ("advancing_count", "market_total_count"),
                "industry_fundamentals": ("industry_revenue_growth",),
                "industry_flow": ("capital_flow", "turnover_value"),
                "industry_history": ("industry_turnover_history",),
                "trading_calendar": ("market_session", "market_session_count"),
                "return_quote": ("close_price",), "return_targets": ("target_price",),
                "macro_policy": ("document",), "industry_policy": ("document",),
                "governance_documents": ("document",), "disclosure_details": ("document",)}
            entity = (market if prefix in {"macro_policy", "macro_liquidity", "market_breadth"} else
                      "中国A股交易日历" if prefix == "trading_calendar" else args[0])
            bundle = "industry_history@" + str(industry) if prefix in {"industry_history", "trading_calendar"} else None
            calls.append(DataCall(label, method, args, method + "@" + "|".join(args),
                priority=priorities.get(prefix, 50), bundle=bundle,
                required_fields=required.get(prefix, ()), expected_entity=entity))
    def has_docs(entity, dimension):
        return any(f.entity == entity and f.field in DOCUMENT_FIELDS and f.source_url and _date(f)
                   and isinstance(f.value, str) and any(term in f.value for term in DIMENSION_TERMS[dimension])
                   for f in current)
    if "policy_score" in gaps.get("market", []) and not has_docs(market, "policy"):
        add("macro_policy", "get_macro_policy", market)
    if "risk_appetite_score" in gaps.get("market", []):
        add("market_breadth", "get_market_breadth", market)
    if ("liquidity_score" in gaps.get("market", []) and market == "中国宏观经济"
            and not {"growth_score", "inflation_score"} & set(gaps.get("market", []))):
        add("macro_liquidity", "get_macro_data", "中国最新M2同比增长率")
    if industry:
        industry_gaps = gaps.get("industry", [])
        if "prosperity_score" in industry_gaps:
            add("industry_fundamentals", "get_industry_fundamentals", industry)
        if "policy_score" in industry_gaps and not has_docs(industry, "policy"):
            add("industry_policy", "get_industry_policy", industry)
        if "capital_flow_score" in industry_gaps:
            add("industry_flow", "get_industry_flow" if callable(getattr(pipeline.provider, "get_industry_flow", None))
                else "get_industry_rank", industry)
        if "crowding_score" in industry_gaps:
            start, end = history_window(pipeline.now())
            if callable(getattr(pipeline.provider, "get_market_calendar", None)):
                add("industry_history", "get_industry_turnover_history", industry, start, end)
                if any(call.method == "get_industry_turnover_history" for call in calls):
                    add("trading_calendar", "get_market_calendar", start, end)
    for index, (name, query) in enumerate(securities):
        fields = {f.field for f in current if f.entity == name}
        if not industry:
            add(f"security_scope:{index}", "get_basic_info", query)
        if "governance_score" not in fields and not has_docs(name, "governance") and not callable(
                getattr(pipeline.provider, "get_stock_disclosure_details", None)):
            add(f"governance_documents:{index}", "get_governance_disclosures", query)
        # 已有公告也可能只有摘要；查询覆盖审计、监管、披露及事件的原始记录。
        if not {"event_score", "governance_score"} <= fields:
            add(f"disclosure_details:{index}", "get_stock_disclosure_details", query)
        if intent == Intent.SECURITY_RESEARCH:
            owned = [f for f in current if f.entity == name]
            forecast = build_return_expectation(owned, [f.fact_id for f in owned], request.profile, "PASS")
            if not forecast.scenarios:
                quote_call = DataCall("quote", "get_quote", (query,), "quote", expected_entity=name)
                if evidence_status(quote_call, owned, pipeline.now())["status"] != "complete":
                    add(f"return_quote:{index}", "get_quote", query)
                target_call = DataCall("targets", "get_institutional_research", (query,), "targets", expected_entity=name)
                if evidence_status(target_call, owned, pipeline.now())["status"] != "complete":
                    add(f"return_targets:{index}", "get_target_prices" if callable(
                        getattr(pipeline.provider, "get_target_prices", None)) else "get_institutional_research", query)
    return calls


def prune_coarse_calls(calls, targeted, pipeline, request, intent, target):
    """专用能力已能覆盖缺项时，省去重复的泛化查询，给历史数据和收益依据留出预算。"""
    gaps = missing_fields_by_agent(request.facts, intent, now=pipeline.now())
    methods = {call.method for call in targeted}
    omitted = set()
    market = set(gaps.get("market", []))
    if market and market <= {"policy_score", "risk_appetite_score", "liquidity_score"} and all(
            method in methods for field, method in (("policy_score", "get_macro_policy"),
                ("risk_appetite_score", "get_market_breadth"), ("liquidity_score", "get_macro_data")) if field in market):
        omitted.add("get_macro_data")
    industry = set(gaps.get("industry", []))
    if (industry and industry <= {"prosperity_score", "policy_score", "crowding_score", "capital_flow_score"} and all(
            method in methods for field, method in (("prosperity_score", "get_industry_fundamentals"),
                ("policy_score", "get_industry_policy"), ("crowding_score", "get_industry_turnover_history")) if field in industry)
            and ("capital_flow_score" not in industry or "get_industry_flow" in methods)):
        omitted.add("get_industry_rank")
    if "get_stock_disclosure_details" in methods:
        omitted.update({"get_event_data", "get_shareholder_equity"})
        if scopes(pipeline, request, intent, target)[1]:
            omitted.add("get_basic_info")
    if any(call.label.startswith("security_scope:") for call in targeted):
        omitted.add("get_industry_rank")  # resolve the actual company industry before index queries
    return [call for call in calls if call.method not in omitted]


def derive_gap_metrics(pipeline, request, intent, target):
    request = align_security_documents(pipeline, request, intent, target)
    roots = [f for f in request.facts if not f.source_id.startswith("DERIVED_RULE_")]
    roots = _merge_by_fact_id([*roots, *derive_explicit_state(roots, now=pipeline.now())])
    _, industry, _ = scopes(pipeline, request, intent, target)
    if industry:
        start, end = history_window(pipeline.now())
        roots = _merge_by_fact_id([*roots, *derive_history_metrics(
            roots, code="", name="", industry=industry, now=pipeline.now(), start=start, end=end)])
    return request.model_copy(update={"facts": _merge_by_fact_id([*roots, *pipeline._derive(roots)])})


def align_security_documents(pipeline, request, intent, target):
    """精确证券代码与已返回简称属于同一实体；不能将其他证券的资料挪用。"""
    _, _, securities = scopes(pipeline, request, intent, target)
    aliases = {}
    for name, query in securities:
        code = re.fullmatch(r"(\d{6})(?:\.(?:SH|SZ|BJ))?", query.upper())
        if code and name != query:
            aliases[code[1]] = name
    if not aliases:
        return request
    facts = []
    for fact in request.facts:
        code = re.fullmatch(r"(\d{6})(?:\.(?:SH|SZ|BJ))?", fact.entity.upper())
        returned_code = str(fact.entity_code or "").split(".")[0]
        if (code and code[1] in aliases and (not returned_code or returned_code == code[1])
                and fact.field in {*DOCUMENT_FIELDS, "source_original", "publish_time", "publish_date"}):
            fact = fact.model_copy(update={"entity": aliases[code[1]]})
        facts.append(fact)
    return request.model_copy(update={"facts": facts})


async def assess_gap_evidence(pipeline, request, intent, target, semantic, *, timeout_seconds=None):
    targets = assessment_targets(pipeline, request, intent, target)
    if not targets:
        return request, "not_required", []
    selected = select_assessment_documents(current_facts(pipeline, request), targets)
    if not selected or not callable(getattr(semantic, "assess_stock_evidence", None)):
        return request, "unavailable", targets
    if timeout_seconds is not None and timeout_seconds <= 0:
        return request, "unavailable", targets
    try:
        review = await asyncio.wait_for(semantic.assess_stock_evidence(request, selected, targets, []),
                                        timeout=min(pipeline.call_timeout_seconds, timeout_seconds)
                                        if timeout_seconds is not None else pipeline.call_timeout_seconds)
    except Exception:
        return request, "unavailable", targets
    if review is None:
        return request, "unavailable", targets
    by_id = {f.fact_id: f for f in selected}
    allowed = {(t["entity"], t["dimension"]) for t in targets}
    added, seen = [], set()
    for assessment in review.assessments:
        key = (assessment.entity, assessment.dimension)
        if key not in allowed or key in seen or not assessment.complete:
            continue
        seen.add(key)
        criteria = CRITERIA[assessment.dimension]
        items = {item.criterion: item for item in assessment.items}
        if len(items) != len(assessment.items) or set(items) != set(criteria):
            continue
        values, parents = {"rubric": assessment.dimension.upper() + "_V1", "complete": True}, []
        for criterion in criteria:
            item = items[criterion]
            source = by_id.get(item.evidence_id)
            text = source.value if source and isinstance(source.value, str) else ""
            if (source is None or source.entity != assessment.entity or not text or item.quote not in text
                    or item.label not in QUALITATIVE_RUBRICS[criterion]):
                break
            values[criterion] = {"label": item.label, "evidence_id": item.evidence_id, "quote": item.quote}
            parents.append(source)
        else:
            identity = json.dumps([key, values, sorted(f.fact_id for f in parents)], ensure_ascii=False, sort_keys=True)
            added.append(FactRecord(fact_id="GAP-ASSESS-" + hashlib.sha256(identity.encode()).hexdigest()[:20],
                entity=assessment.entity, field=assessment.dimension + "_assessment", value=values,
                snapshot_time=min(f.snapshot_time for f in parents), quality=min(f.quality for f in parents) * .9,
                source_id="DERIVED_GAP_ASSESSMENT_V1", derived_from=sorted({f.fact_id for f in parents}),
                derivation_rule="GAP_ASSESSMENT_V1: exact source quotes and complete rubric"))
    if not added:
        return request, "partial", targets
    enriched = request.model_copy(update={"facts": _merge_by_fact_id([*request.facts, *added])})
    enriched = derive_gap_metrics(pipeline, enriched, intent, target)
    return enriched, "completed" if len(added) == len(targets) else "partial", targets


def select_assessment_documents(facts, targets):
    """Deduplicate title/summary pairs and reserve relevant material for each rubric."""
    grouped = {}
    for fact in facts:
        if (fact.field not in DOCUMENT_FIELDS or not fact.source_url or not _date(fact)
                or not isinstance(fact.value, str) or not fact.value.strip()):
            continue
        key = (fact.entity, fact.source_url, fact.period,
               fact.source_field or fact.fact_id if fact.field.endswith("excerpt") else "")
        current = grouped.get(key)
        rank = lambda f: (f.field.endswith("excerpt"), f.field.endswith("summary"), len(f.value))
        if current is None or rank(fact) > rank(current):
            grouped[key] = fact
    output = []
    for entity in dict.fromkeys(t["entity"] for t in targets):
        docs = [f for f in grouped.values() if f.entity == entity]
        dimensions = list(dict.fromkeys(t["dimension"] for t in targets if t["entity"] == entity))
        selected = {}
        for dimension in dimensions:
            relevant = sorted(docs, key=lambda f: (
                sum(term in f.value for term in DIMENSION_TERMS[dimension]), _date(f), f.fact_id), reverse=True)
            for fact in relevant[:max(1, 12 // len(dimensions))]:
                selected[fact.fact_id] = fact
        for fact in sorted(docs, key=lambda f: (_date(f), f.fact_id), reverse=True):
            if len(selected) >= 12:
                break
            selected[fact.fact_id] = fact
        output.extend(selected.values())
    return output


async def read_gap_documents(pipeline, request, intent, target, fetched, *, timeout_seconds=None):
    """最多读取两份本轮供应商返回的官方 PDF，标题不能替代原文核验。"""
    _, _, securities = scopes(pipeline, request, intent, target)
    if timeout_seconds is not None and timeout_seconds <= 0:
        return [], [], [], [], {}
    jobs, seen = [], set()
    # Prefer relevant original reports by actual publication date, not retrieval time.
    titles = {f.source_url: str(f.value) for f in fetched if f.field == 'announcement'}
    def document_rank(f):
        title = titles.get(f.source_url, '')
        full_report = (8 if '审计报告' in title else 6 if '年度报告' in title else 0)
        full_report -= 8 if any(word in title for word in ('摘要', '英文')) else 0
        return (full_report, _date(f) or '', sum(term in str(f.value) for term in DIMENSION_TERMS['governance']))
    fetched = sorted(fetched, key=document_rank, reverse=True)
    for name, query in securities:
        code = re.search(r"(?<!\d)\d{6}(?!\d)", query)
        code = code.group() if code else next((str(f.entity_code).split('.')[0] for f in request.facts
                                             if f.entity == name and f.entity_code), "")
        if not re.fullmatch(r"\d{6}", code):
            continue
        for source in fetched:
            if (source.entity == name and source.field in DOCUMENT_FIELDS and source.source_url
                    and source.source_url not in seen and allowed_disclosure_url(source.source_url)
                    and fact_is_current(source, pipeline.now())):
                seen.add(source.source_url)
                source = next((f for f in fetched if f.source_url == source.source_url
                               and f.entity == name and f.field == 'announcement'), source)
                jobs.append((f"disclosure_pdf:{len(jobs)}", source, code, name))
    jobs = jobs[:2]
    async def read(source, code, name):
        return await asyncio.wait_for(read_disclosure(source, code=code, name=name),
            timeout=min(30, timeout_seconds) if timeout_seconds is not None else 30)
    results = await asyncio.gather(*(read(source, code, name) for _, source, code, name in jobs), return_exceptions=True)
    facts, success, empty, errors = [], [], [], {}
    for (label, _, _, _), result in zip(jobs, results, strict=True):
        if isinstance(result, BaseException):
            errors[label] = failure_summary(result)
        elif result:
            success.append(label)
            facts.extend(result)
        else:
            empty.append(label)
    return facts, [label for label, *_ in jobs], success, empty, errors
