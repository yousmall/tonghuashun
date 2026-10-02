"""Profile-directed, read-only stock discovery behind the existing research entry."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from time import perf_counter
from uuid import uuid4

from backend.app.agents.coordinator import RISK_NOTICE, CoordinatorAgent, cross_validate_results
from backend.app.fact_taxonomy import fact_is_current
from backend.app.models import (
    AgentResult, ComplianceStatus, DataAcquisitionResult, FactRecord,
    Intent, OrchestrationRequest, StockRecommendationCandidate, StockRecommendationResult,
    TaskStatus,
)
from backend.app.research_routing import DIRECTION_FOR_INTENT
from backend.app.risk_questionnaire import assessment_is_current
from backend.app.semantic import RequestUnderstanding
from backend.app.services.evidence_coverage import missing_fields_by_agent
from backend.app.services.provider_errors import failure_summary
from backend.app.services.research import AutomatedResearchPipeline, DataCall
from backend.app.services.scoring_dimensions import QUALITATIVE_RUBRICS

DOCUMENT_FIELDS = {"news", "announcement", "event", "research_report"}
CRITERIA = {"policy": ("policy",), "event": ("event",),
            "governance": ("audit_opinion", "regulatory_status", "disclosure_status")}
LIQUIDITY_FLOORS = {"高": 50_000_000, "中": 20_000_000, "低": 10_000_000}


def symbol_code(value) -> str | None:
    """Only a provider-returned mainland symbol can seed instrument research."""
    match = re.fullmatch(r"(?:(?:SH|SZ|BJ)[.:]?)?([034689]\d{5})(?:[.](?:SH|SZ|BJ))?",
                         str(value or "").strip().upper())
    return match[1] if match else None


def _merge(*groups):
    return list({f.fact_id: f for group in groups for f in group}.values())


def _text(fact):
    return fact.value if isinstance(fact.value, str) else json.dumps(fact.value, ensure_ascii=False, sort_keys=True)


class StockRecommendationService:
    def __init__(self, pipeline: AutomatedResearchPipeline, coordinator: CoordinatorAgent, *,
                 max_candidates: int = 10, concurrency: int = 2, timeout_seconds: float = 120):
        self.pipeline, self.coordinator = pipeline, coordinator
        self.max_candidates = max(1, min(20, max_candidates))
        self.concurrency = max(1, min(4, concurrency))
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def should_run(request, understanding):
        return (understanding.action == "recommend_stocks"
                and understanding.intent is Intent.SECURITY_RESEARCH
                and understanding.confidence >= .65 and not understanding.risk_rules
                and not understanding.unsupported_part and assessment_is_current(request.profile)
                and (not request.research_direction or request.research_direction ==
                     DIRECTION_FOR_INTENT[Intent.SECURITY_RESEARCH]))

    def _call(self, label, method, target):
        return DataCall(label, method, (target,), f"{method}@{target}")

    async def _prepare(self, req, calls, *, refresh=False):
        return await self.pipeline.prepare(req, Intent.SECURITY_RESEARCH,
                                           planned_calls=calls, force_refresh=refresh)

    def _empty(self, req, report, reason):
        plan = self.coordinator.plan(req, f"T-{uuid4().hex[:12].upper()}", Intent.SECURITY_RESEARCH)
        plan.nodes = []
        output = self.coordinator._review_package(plan, reason)
        return output.model_copy(update={"stock_recommendation": report,
                                         "next_steps": [reason], "user_fit": "画像匹配尚待核实。"})

    def _conditions(self, request, understanding):
        profile = request.profile
        floor = LIQUIDITY_FLOORS.get(profile.liquidity_need, 10_000_000)
        # These are versioned discovery proxies, not product risk ratings.
        # Exact drawdown/liquidity limits are enforced on each shortlisted stock.
        pe_limit, roe_floor = {"R1": (25, 10), "R2": (25, 10), "R3": (40, 8),
                               "R4": (60, 5), "R5": (60, 5)}.get(profile.risk_level, (40, 8))
        conditions = {
            "risk_level": profile.risk_level,
            "horizon_months": profile.horizon_months,
            "investment_horizon_label": profile.investment_horizon_label,
            "max_drawdown": profile.max_drawdown,
            "loss_tolerance_label": profile.loss_tolerance_label,
            "liquidity_need": profile.liquidity_need,
            "min_avg_turnover_20d_cny": floor,
            "preferred_product_types": profile.preferred_product_types,
            "suitable_product_levels": profile.suitable_product_levels,
            "constraints": list(profile.constraints),
            "preferences": understanding.stock_preferences,
            "investment_target": profile.target,
            "screen_pe_upper_bound": pe_limit,
            "screen_roe_lower_bound_percent": roe_floor,
        }
        # No account identifier, questionnaire answers or investment history goes
        # to the public provider/cache. Profile limits are server-owned criteria.
        query = f"非ST，非停牌，市盈率大于0且小于{pe_limit}，净资产收益率大于{roe_floor}%"
        if profile.horizon_months is not None and profile.horizon_months >= 12:
            query += "，最近三年归母净利润均为正"
        for condition in [*profile.constraints, *understanding.stock_preferences]:
            query += "，" + condition[:200]
        query += "，返回股票代码、股票简称、所属行业、市盈率、市净率、ROE、营业收入同比增长率"
        return conditions, query

    async def run(self, request: OrchestrationRequest, understanding: RequestUnderstanding, *,
                  progress_sink=None, metrics_sink=None):
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await self._run(request, understanding, progress_sink=progress_sink, metrics_sink=metrics_sink)
        except TimeoutError:
            conditions, query = self._conditions(request, understanding)
            report = StockRecommendationResult(requested_count=understanding.recommendation_count,
                                               screening_conditions=conditions, screening_query=query)
            reason = "本次股票推荐超过研究时间预算，完整核验尚未完成，暂不生成推荐。"
            audit = DataAcquisitionResult(mode="unavailable", failed_capabilities=["stock_recommendation"],
                capability_errors={"stock_recommendation": {"code": "RESEARCH_TIMEOUT", "retryable": True}})
            return request, audit, self._empty(request, report, reason)

    async def _run(self, request: OrchestrationRequest, understanding: RequestUnderstanding, *,
                   progress_sink=None, metrics_sink=None):
        started = perf_counter()
        conditions, query = self._conditions(request, understanding)
        report = StockRecommendationResult(requested_count=understanding.recommendation_count,
                                           screening_conditions=conditions, screening_query=query)
        audits = []
        if not self.should_run(request, understanding):
            advice = await self.coordinator.run(request, understanding=understanding)
            return request, DataAcquisitionResult(), advice
        profile = request.profile
        missing_profile = []
        if profile.risk_level not in {"R1", "R2", "R3", "R4", "R5"}:
            missing_profile.append("风险等级")
        if profile.horizon_months is None and not profile.investment_horizon_label:
            missing_profile.append("投资期限")
        if profile.max_drawdown is None and not profile.loss_tolerance_label:
            missing_profile.append("损失承受能力")
        if not profile.liquidity_need and not profile.questionnaire_version:
            missing_profile.append("流动性需求")
        if missing_profile:
            reason = "请先确认" + "、".join(missing_profile) + "，再按画像筛选股票。"
            return request, DataAcquisitionResult(mode="not_required"), self._empty(request, report, reason)
        if profile.preferred_product_types and not any("权益" in p or "股票" in p for p in profile.preferred_product_types):
            reason = "已确认的投资品种不包含股票，请先重新确认投资偏好。"
            return request, DataAcquisitionResult(mode="not_required"), self._empty(request, report, reason)
        if not request.auto_fetch or self.pipeline.provider is None:
            reason = "股票推荐需要同花顺候选数据；自动取数未启用或同花顺数据服务未配置。"
            return request, DataAcquisitionResult(mode="provided" if not request.auto_fetch else "unavailable"), self._empty(request, report, reason)
        if progress_sink:
            progress_sink("按投资偏好筛选股票")
        screen = self._call("stock_screen", "screen_stocks", query)
        # Previous conversation facts are not a new profile-matched candidate pool.
        seed = request.model_copy(update={"facts": []})
        screened, audit = await self._prepare(seed, [screen])
        audits.append(audit)
        groups = {}
        for fact in screened.facts:
            code = symbol_code(fact.entity_code)
            if code and fact_is_current(fact, self.pipeline.now()):
                groups.setdefault(code, []).append(fact)
        limit = min(self.max_candidates, max(understanding.recommendation_count * 2, 4))
        symbols = list(groups)[:limit]
        if not symbols:
            reason = ("同花顺暂未返回可核验的候选股票，未生成推荐。"
                      if audit.failed_capabilities else "同花顺未返回符合本次画像条件的有效股票代码，未生成推荐。")
            return screened, self._audit(audits, screened.facts), self._empty(screened, report, reason)
        shared_calls = [self._call("macro", "get_macro_data", "中国最新宏观经济"),
                        self._call("macro_policy", "get_news", "中国宏观经济")]
        shared, shared_audit = await self._prepare(seed, shared_calls)
        audits.append(shared_audit)
        shared = await self._assess(shared, [{"entity": "中国宏观经济", "dimension": "policy"}], [])
        # A bounded refresh is attempted before any candidate analysis.
        if "market" in missing_fields_by_agent(shared.facts, Intent.SECURITY_RESEARCH, now=self.pipeline.now()):
            retry = [call for call in shared_calls if shared_audit.capability_errors.get(call.label, {}).get("retryable", True)]
            if retry:
                shared, repair = await self._prepare(shared, retry, refresh=True)
                audits.append(self._repair_audit(repair))
                shared = await self._assess(shared, [{"entity": "中国宏观经济", "dimension": "policy"}], [])
        semaphore = asyncio.Semaphore(self.concurrency)
        outputs = {}

        async def research(code):
            async with semaphore:
                name = groups[code][0].entity
                try:
                    outputs[code] = await self._candidate(request, understanding, code, name, groups[code], shared.facts)
                except Exception as exc:
                    entry = StockRecommendationCandidate(symbol=code, name=name, reasons=["候选研究暂不可用，未进入推荐。"])
                    failure = DataAcquisitionResult(failed_capabilities=[f"research:{code}"],
                        capability_errors={f"research:{code}": failure_summary(exc)})
                    outputs[code] = (entry, None, groups[code], [failure])

        if progress_sink:
            progress_sink("核验候选股票")
        tasks = [asyncio.create_task(research(code)) for code in symbols]
        try:
            # Reserve time for final verification instead of returning unreviewed
            # partial output when the candidate deadline has been reached.
            reserve = min(self.timeout_seconds / 4, self.coordinator._llm_node_timeout + 5)
            _, pending = await asyncio.wait(tasks, timeout=max(.001, self.timeout_seconds - reserve - (perf_counter() - started)))
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        all_facts = _merge(screened.facts, shared.facts)
        eligible = []
        for code in symbols:
            if code not in outputs:
                report.candidates.append(StockRecommendationCandidate(symbol=code, name=groups[code][0].entity,
                    reasons=["候选研究超过本次时间预算，未进入推荐。"], evidence=[groups[code][0].fact_id]))
                audits.append(DataAcquisitionResult(failed_capabilities=[f"research:{code}"],
                    capability_errors={f"research:{code}": {"code": "RESEARCH_TIMEOUT", "retryable": True}}))
                continue
            entry, advice, facts, local_audits = outputs[code]
            report.candidates.append(entry)
            all_facts = _merge(all_facts, facts)
            audits.extend(local_audits)
            if entry.status == "recommended":
                eligible.append((entry, advice))
        prepared = request.model_copy(update={"facts": all_facts})
        acquisition = self._audit(audits, all_facts)
        acquisition.missing_fields_by_agent = {f"{entry.symbol}:{agent}": fields
            for entry in report.candidates for agent, fields in entry.missing_fields.items()}
        if metrics_sink is not None:
            metrics_sink.update({"available": len(all_facts)})
        eligible.sort(key=lambda pair: (-(pair[0].score or 0), -pair[0].confidence, pair[0].symbol))
        chosen = eligible[:understanding.recommendation_count]
        chosen_codes = {entry.symbol for entry, _ in chosen}
        for entry, _ in eligible:
            if entry.symbol not in chosen_codes:
                entry.status = "excluded"
                entry.reasons.append("已完成研究，本次排序未进入推荐名额。")
        if not chosen:
            reason = "已按投资画像查询同花顺并核验候选，暂无通过画像匹配与完整核验的股票。"
            return prepared, acquisition, self._empty(prepared, report, reason)
        # Final output uses the existing verifier and semantic/compliance gates,
        # including all selected stocks rather than borrowing one stock's PASS.
        entries = [entry for entry, _ in chosen]
        opinion = "按已确认投资偏好筛选，建议关注以下股票：\n\n" + "\n\n".join(
            f"{i}. {entry.name}（{entry.symbol}）：" + "；".join(entry.reasons)
            + ("。主要风险：" + "；".join(entry.risks) if entry.risks else "")
            for i, entry in enumerate(entries, 1))
        security = AgentResult(agent_id="security", status=TaskStatus.COMPLETED, opinion=opinion,
            score=round(sum(entry.score for entry in entries) / len(entries), 2),
            confidence=min(entry.confidence for entry in entries),
            facts_used=sorted({fact_id for entry in entries for fact_id in entry.evidence}),
            risk_flags=list(dict.fromkeys(risk for entry in entries for risk in entry.risks)),
            details={"recommendation_symbols": [entry.symbol for entry in entries]})
        results = [r for r in chosen[0][1].agent_results if r.agent_id != "security"] + [security]
        try:
            async with asyncio.timeout(self.coordinator._llm_node_timeout + 5):
                verified = await self.coordinator.verifier(results, all_facts)
                cross, compliance = await self.coordinator._review_results(prepared, verified)
                advice = self.coordinator._aggregate(prepared, chosen[0][1].task_plan, verified, cross, compliance)
        except Exception:
            advice = self._empty(prepared, report, "推荐结果的最终核验暂未完成，需要复核。")
        if (advice.compliance.status is not ComplianceStatus.PASS or
                advice.cross_validation.status is not ComplianceStatus.PASS or
                any(r.status is not TaskStatus.COMPLETED for r in advice.agent_results)):
            for entry in entries:
                entry.status = "review"
                entry.reasons.append("最终核验未通过，暂不推荐。")
            advice.conclusion = "候选股票的最终核验尚未通过，暂不输出推荐名单。"
        else:
            report.recommendations = entries
            advice.conclusion = opinion
            advice.next_steps = ["结合每只股票的风险及资料日期复核，关注后续财报和公告。"]
        advice.stock_recommendation = report
        return prepared, acquisition, advice

    async def _candidate(self, request, understanding, code, name, screen_facts, shared_facts):
        entry = StockRecommendationCandidate(symbol=code, name=name, evidence=[screen_facts[0].fact_id],
                                             snapshot_time=screen_facts[0].snapshot_time)
        audits = []
        own = [f.model_copy(update={"entity": name}) for f in screen_facts]
        req = request.model_copy(update={"query": f"研究候选股票{code}是否符合已确认投资画像。",
                                         "context_messages": [], "facts": own})
        basic_call = self._call(f"basic_info:{code}", "get_basic_info", code)
        req, audit = await self._prepare(req, [basic_call])
        audits.append(audit)
        own = self._own_facts(req.facts, code, name)
        industry_fact = next((f for f in own if f.field == "industry" and isinstance(f.value, str)
                              and fact_is_current(f, self.pipeline.now())), None)
        if industry_fact is None:
            entry.missing_fields = {"security": ["industry"]}
            code_status = audit.capability_errors.get(basic_call.label, {}).get("code")
            entry.reasons = ["同花顺基本资料接口未获授权，所属行业尚未核实。" if code_status in
                             {"AUTHENTICATION_REJECTED", "CAPABILITY_FORBIDDEN"} else
                             "所属行业资料未核实，无法完成行业与个股联合研究。"]
            return entry, None, own, audits
        industry = industry_fact.value.strip()
        calls = [self._call(f"{label}:{code}", method, code) for label, method in (
            ("quote", "get_quote"), ("financial", "get_financial_metrics"),
            ("event", "get_event_data"), ("governance", "get_announcements"),
            ("risk_metrics", "get_stock_risk_metrics"))]
        calls += [self._call(f"industry:{industry}", "get_industry_rank", industry),
                  self._call(f"industry_policy:{industry}", "get_news", industry)]
        req = req.model_copy(update={"facts": _merge(own, shared_facts)})
        req, audit = await self._prepare(req, calls)
        audits.append(audit)
        req = self._scope_candidate(req, code, name, industry, shared_facts)
        targets = [{"entity": industry, "dimension": "policy"},
                   {"entity": name, "dimension": "event"}, {"entity": name, "dimension": "governance"}]
        conditions = list(dict.fromkeys([*request.profile.constraints, *understanding.stock_preferences]))
        conditions.append("历史风险特征与已确认风险等级" + request.profile.risk_level + "匹配")
        if request.profile.max_drawdown is None:
            conditions.append("历史风险特征适合已确认的损失承受能力：" + request.profile.loss_tolerance_label)
        if request.profile.investment_horizon_label:
            conditions.append("适合已确认投资期限：" + request.profile.investment_horizon_label)
        elif request.profile.horizon_months is not None:
            conditions.append(f"适合已确认投资期限：{request.profile.horizon_months}个月")
        if request.profile.target:
            conditions.append("适合已确认投资目标：" + request.profile.target)
        req = await self._assess(req, targets, conditions)
        gaps = missing_fields_by_agent(req.facts, Intent.SECURITY_RESEARCH, now=self.pipeline.now())
        metrics_missing = any(self._latest(req.facts, name, f) is None for f in
                              ("max_drawdown_1y", "avg_turnover_20d", "is_st", "trading_status"))
        if gaps.get("security") or gaps.get("industry") or metrics_missing:
            retry = [call for call in calls if audit.capability_errors.get(call.label, {}).get("retryable", True)]
            if retry:
                req, repaired = await self._prepare(req, retry, refresh=True)
                audits.append(self._repair_audit(repaired))
                req = self._scope_candidate(req, code, name, industry, shared_facts)
                req = await self._assess(req, targets, conditions)
        entry.missing_fields = missing_fields_by_agent(req.facts, Intent.SECURITY_RESEARCH, now=self.pipeline.now())
        fit, fit_reason, fit_ids = self._fit(req, name, conditions)
        entry.evidence = sorted(set(entry.evidence + fit_ids))
        if fit != "yes":
            entry.status = "excluded" if fit == "no" else "review"
            entry.reasons = [fit_reason]
            return entry, None, req.facts, audits
        candidate_understanding = understanding.model_copy(update={"action": "analyze", "target": code,
                                                                  "data_requirements": []})
        advice = await self.coordinator.run(req, understanding=candidate_understanding)
        security = next((r for r in advice.agent_results if r.agent_id == "security"), None)
        if security is not None:
            security.facts_used = sorted(set(security.facts_used + fit_ids + entry.evidence))
            advice.agent_results = await self.coordinator.verifier(advice.agent_results, req.facts)
            advice.cross_validation = cross_validate_results(advice.agent_results, req.facts)
            security = next((r for r in advice.agent_results if r.agent_id == "security"), None)
        if (advice.compliance.status is not ComplianceStatus.PASS or advice.cross_validation.status is not ComplianceStatus.PASS
                or len(advice.agent_results) < 3 or any(r.status is not TaskStatus.COMPLETED for r in advice.agent_results)
                or entry.missing_fields or not security or security.score is None
                or security.details.get("security") != name
                or not any(r.agent_id == "industry" and r.details.get("industry") == industry
                           for r in advice.agent_results)):
            entry.reasons = ["关键研究资料、专业判断或交叉核验尚未完整通过。"]
            entry.risks = advice.risks
            return entry, advice, req.facts, audits
        if any((self._latest(req.facts, name, f).value < threshold) for f, threshold in
               (("event_score", 50), ("governance_score", 75))):
            entry.status = "excluded"
            entry.reasons = ["事件或治理证据提示不利情况，未进入推荐。"]
            return entry, advice, req.facts, audits
        entry.status, entry.score, entry.confidence = "recommended", security.score, advice.confidence
        entry.reasons = [fit_reason, security.opinion]
        entry.risks = list(dict.fromkeys([*advice.risks, "历史回撤不代表未来最大亏损，短期价格仍可能波动。", RISK_NOTICE]))
        entry.evidence = sorted(set(entry.evidence + advice.evidence + fit_ids))
        entry.snapshot_time = min(f.snapshot_time for f in req.facts if f.fact_id in entry.evidence)
        return entry, advice, req.facts, audits

    def _own_facts(self, facts, code, name):
        # Recompute normalized units from raw values even if an adapter returns
        # a copied model whose cached normalized_value no longer matches value.
        return [FactRecord.model_validate({**f.model_dump(), "entity": name}) for f in facts
                if (symbol_code(f.entity_code) == code or (not f.entity_code and f.entity == code))]

    def _scope_candidate(self, req, code, name, industry, shared):
        own = self._own_facts(req.facts, code, name)
        sector = [f for f in req.facts if f.entity == industry and not f.entity_code]
        roots = [f for f in _merge(own, sector, shared) if not f.source_id.startswith("DERIVED_RULE_")]
        return req.model_copy(update={"facts": _merge(roots, self.pipeline._derive(roots))})

    def _latest(self, facts, entity, field):
        return max((f for f in facts if f.entity == entity and f.field == field
                    and fact_is_current(f, self.pipeline.now())), key=lambda f: f.snapshot_time, default=None)

    async def _assess(self, req, targets, conditions):
        roots = [f for f in req.facts if not f.source_id.startswith("DERIVED_RULE_")]
        current = [f for f in roots if fact_is_current(f, self.pipeline.now())]
        docs = [f for f in current if f.field in DOCUMENT_FIELDS and f.source_url and f.period]
        needed = [t for t in targets if not self._latest(req.facts, t["entity"], t["dimension"] + "_score")]
        if not needed and not conditions:
            return req
        # Keep document coverage for every requested entity within a fixed budget.
        selected = []
        for entity in dict.fromkeys(t["entity"] for t in targets):
            selected.extend(sorted((f for f in docs if f.entity == entity), key=lambda f: f.snapshot_time, reverse=True)[:8])
        selected = _merge(selected, [f for f in current if f.field not in DOCUMENT_FIELDS][:60])
        candidate = next((t["entity"] for t in targets if t["dimension"] == "governance"), None)
        review = await self.coordinator.semantic.assess_stock_evidence(
            req, selected, needed or targets, conditions, candidate_entity=candidate)
        if review is None:
            return req.model_copy(update={"facts": _merge(roots, self.pipeline._derive(roots))})
        by_id = {f.fact_id: f for f in selected}
        allowed = {(t["entity"], t["dimension"]) for t in targets}
        seen = set()
        for assessment in review.assessments:
            key = (assessment.entity, assessment.dimension)
            if key not in allowed or key in seen or not assessment.complete:
                continue
            seen.add(key)
            criteria = CRITERIA[assessment.dimension]
            items = {item.criterion: item for item in assessment.items}
            if len(items) != len(assessment.items) or set(items) != set(criteria):
                continue
            value, parents = {"rubric": assessment.dimension.upper() + "_V1", "complete": True}, []
            for criterion in criteria:
                item = items[criterion]
                doc = by_id.get(item.evidence_id)
                if (item.label not in QUALITATIVE_RUBRICS[criterion] or doc not in docs
                        or doc.entity != assessment.entity or item.quote not in _text(doc)):
                    break
                value[criterion] = {"label": item.label, "evidence_id": item.evidence_id, "quote": item.quote}
                parents.append(doc)
            else:
                roots.append(self._derived(assessment.entity, assessment.dimension + "_assessment", value, parents))
        # Match evidence must belong to this candidate, never a different stock.
        matches = {m.condition_index: m for m in review.matches}
        if conditions and len(matches) == len(review.matches) and set(matches) == set(range(len(conditions))):
            parents, outcomes = [], []
            for index in range(len(conditions)):
                match = matches[index]
                source = by_id.get(match.evidence_id)
                if (match.result == "unknown" or source is None or source.entity != candidate
                        or not match.quote or match.quote not in _text(source)):
                    break
                parents.append(source)
                outcomes.append(match.result)
            else:
                roots.append(self._derived(candidate, "stock_constraint_match",
                    {"conditions": conditions, "result": "no" if "no" in outcomes else "yes"}, parents))
        return req.model_copy(update={"facts": _merge(roots, self.pipeline._derive(roots))})

    def _derived(self, entity, field, value, parents):
        digest = hashlib.sha256(json.dumps([entity, field, value, sorted(f.fact_id for f in parents)],
                                          ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]
        codes = {f.entity_code for f in parents if f.entity_code}
        return FactRecord(fact_id="STOCK-ASSESS-" + digest, entity=entity, field=field, value=value,
                          snapshot_time=min(f.snapshot_time for f in parents), source_id="DERIVED_STOCK_ASSESSMENT_V1",
                          quality=min(f.quality for f in parents) * .9,
                          entity_code=next(iter(codes)) if len(codes) == 1 else None,
                          derived_from=sorted({f.fact_id for f in parents}), derivation_rule="PROFILE_STOCK_V1: validated evidence quotes")

    def _fit(self, req, name, conditions):
        evidence = []
        metrics = {field: self._latest(req.facts, name, field) for field in
                   ("max_drawdown_1y", "avg_turnover_20d", "is_st", "trading_status")}
        if any(f is None for f in metrics.values()):
            return "unknown", "历史回撤、流动性或交易状态资料不完整，画像匹配尚未核实。", []
        for fact in metrics.values():
            evidence.append(fact.fact_id)
        st = metrics["is_st"].value
        if str(st).strip().casefold() in {"true", "1", "是", "st", "*st"} or "ST" in name.upper():
            return "no", "候选存在风险警示，不符合本次筛选条件。", evidence
        if str(st).strip().casefold() not in {"false", "0", "否", "非st"}:
            return "unknown", "候选风险警示状态尚未核实。", evidence
        status = str(metrics["trading_status"].value).strip()
        if status in {"停牌", "退市", "终止上市", "暂停上市"}:
            return "no", "候选未处于正常交易状态，不符合本次筛选条件。", evidence
        if status not in {"正常", "正常交易", "交易", "交易中", "交易状态正常"}:
            return "unknown", "候选的正常交易状态尚未核实。", evidence
        drawdown = metrics["max_drawdown_1y"].normalized_value
        if drawdown is None or not math.isfinite(drawdown) or not -100 <= drawdown <= 100:
            return "unknown", "历史回撤的数值或百分比单位尚未核实。", evidence
        drawdown = abs(drawdown)
        if req.profile.max_drawdown is not None and drawdown > req.profile.max_drawdown * 100:
            return "no", "候选历史回撤超过已确认的承受范围。", evidence
        amount = metrics["avg_turnover_20d"]
        multiplier = {"CNY": 1, "元": 1, "万元": 10_000, "亿元": 100_000_000}.get(amount.unit)
        try:
            turnover = float(amount.value) * multiplier if multiplier else None
        except (ValueError, TypeError):
            turnover = None
        if isinstance(amount.value, bool) or turnover is None or not math.isfinite(turnover):
            return "unknown", "成交额的数值或币种单位尚未核实。", evidence
        if turnover < LIQUIDITY_FLOORS.get(req.profile.liquidity_need, 10_000_000):
            return "no", "候选成交活跃度未达到本次流动性筛选条件。", evidence
        if conditions:
            matched = self._latest(req.facts, name, "stock_constraint_match")
            if not matched or matched.value.get("conditions") != conditions:
                return "unknown", "投资禁忌、期限或本轮偏好尚缺少可核验的匹配依据。", evidence
            evidence.append(matched.fact_id)
            if matched.value["result"] != "yes":
                return "no", "候选不符合已确认的投资条件或本轮筛选偏好。", evidence
        return "yes", f"历史回撤{drawdown:g}%，成交活跃度符合本次画像筛选条件", evidence

    @staticmethod
    def _repair_audit(audit):
        return audit.model_copy(update={"recovery_rounds": 1,
            "recovery_capabilities": audit.requested_capabilities,
            "recovery_successful_capabilities": audit.successful_capabilities,
            "recovery_failed_capabilities": audit.failed_capabilities,
            "recovery_empty_capabilities": audit.empty_capabilities,
            "recovery_errors": audit.capability_errors, "recovery_phase": "before_analysis"})

    @staticmethod
    def _audit(audits, facts):
        values = {"mode": "live" if any(a.fetched_fact_count for a in audits) else "unavailable",
                  "provider": next((a.provider for a in audits if a.provider), None),
                  "fetched_fact_count": len({f.fact_id for f in facts if f.produced_by}),
                  "derived_fact_count": sum(f.source_id.startswith("DERIVED_") for f in facts),
                  "recovery_rounds": int(any(a.recovery_rounds for a in audits)),
                  "message": "已按确认画像查询同花顺候选并逐只研究，核验结果见股票推荐明细。"}
        for field in ("requested_capabilities", "successful_capabilities", "reused_capabilities", "empty_capabilities",
                      "failed_capabilities", "cached_capabilities", "recovery_capabilities", "recovery_successful_capabilities",
                      "recovery_empty_capabilities", "recovery_failed_capabilities"):
            values[field] = list(dict.fromkeys(item for a in audits for item in getattr(a, field)))
        for field in ("capability_errors", "capability_timings_ms", "recovery_errors"):
            values[field] = {key: value for a in audits for key, value in getattr(a, field).items()}
        if values["recovery_rounds"]:
            values["recovery_phase"] = "before_analysis"
        return DataAcquisitionResult(**values)
