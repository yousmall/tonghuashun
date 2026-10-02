"""One bounded, read-only recovery round before returning the research report."""
from __future__ import annotations

import asyncio
from typing import Callable
from time import perf_counter

from backend.app.fact_taxonomy import FAST_MARKET_FIELDS, SLOW_FINANCIAL_FIELDS, fact_is_current
from backend.app.models import (
    AdvicePackage, ComplianceStatus, DataAcquisitionResult, Intent,
    OrchestrationRequest, ResearchCapability, TaskStatus,
)
from backend.app.risk_questionnaire import assessment_is_current
from backend.app.services.evidence_coverage import AGENT_FIELDS, missing_fields_by_agent
from backend.app.services.research import AutomatedResearchPipeline, _merge_by_fact_id
from backend.app.services.provider_errors import failure_summary


AGENT_METHODS = {
    "market": {"get_macro_data"},
    "industry": {"get_industry_rank"},
    "security": {"get_quote", "get_financial_metrics", "get_event_data", "get_convertible_bond"},
    "fund": {"get_fund_candidates"},
}
CONFLICT_CODES = {"INTERNAL_VALUE_CONFLICT", "SOURCE_VALUE_CONFLICT"}


async def recover_research(
    pipeline: AutomatedResearchPipeline,
    request: OrchestrationRequest,
    intent: Intent,
    advice: AdvicePackage | None,
    audit: DataAcquisitionResult,
    *,
    target: str | None = None,
    data_requirements: list[ResearchCapability] | None = None,
    progress_sink: Callable[[str], None] | None = None,
) -> tuple[OrchestrationRequest, DataAcquisitionResult]:
    """Refresh relevant capabilities once; never erase contradictory source facts.

    With no advice, repair observable gaps before any specialist/model review.
    After analysis, retain recovery for rejected references and source conflicts.
    Both paths share the same one-round budget and profile/intent gates.
    """
    if (audit.recovery_rounds or not request.auto_fetch or pipeline.provider is None
            or not assessment_is_current(request.profile)
            or (advice is not None and (not advice.agent_results
                or advice.compliance.status is ComplianceStatus.BLOCK))
            or intent in {Intent.UNKNOWN, Intent.EDUCATION}):
        return request, audit

    gaps = missing_fields_by_agent(request.facts, intent, now=pipeline.now())
    if (audit.capability_errors and not audit.successful_capabilities and not audit.reused_capabilities
            and all(error.get("code") == "AUTHENTICATION_REJECTED" for error in audit.capability_errors.values())):
        return request, audit  # no authorized capability succeeded; extra calls cannot repair credentials
    agents = set(gaps)
    for result in advice.agent_results if advice is not None else ():
        if result.status in {TaskStatus.DEGRADED, TaskStatus.UNKNOWN} and (
                (result.details or {}).get("missing_fields")
                or (result.details or {}).get("rejected_reference_count")):
            agents.add(result.agent_id)
    methods = set().union(*(AGENT_METHODS.get(agent, set()) for agent in agents if agent != "security"))
    security_gaps = set(gaps.get("security", ()))
    for fields, method in (
        ({"fundamental_score", "valuation_score"}, "get_financial_metrics"),
        ({"technical_score"}, "get_quote"), ({"event_score"}, "get_event_data"),
    ):
        if security_gaps & fields:
            methods.add(method)
    if "security" in agents and not security_gaps:
        methods.update(AGENT_METHODS["security"])
    conflicted_ids = {fact_id for issue in (advice.cross_validation.issues if advice else ())
                      if issue.code in CONFLICT_CODES for fact_id in issue.fact_ids}
    conflicted_keys = {fact.produced_by for fact in request.facts
                       if fact.fact_id in conflicted_ids and fact.produced_by}
    # Manually supplied conflicts have no producing call. Use only known field
    # families; never turn a source ID into an arbitrary method or URL.
    for fact in request.facts:
        if fact.fact_id not in conflicted_ids or fact.produced_by:
            continue
        if fact.field in FAST_MARKET_FIELDS or fact.field == "technical_score":
            methods.add("get_quote")
        elif fact.field in SLOW_FINANCIAL_FIELDS or fact.field == "fundamental_score":
            methods.add("get_financial_metrics")
        else:
            for agent, fields in AGENT_FIELDS.items():
                if fact.field in fields:
                    methods.update(AGENT_METHODS.get(agent, set()))

    extra = list(data_requirements or [])
    if "governance_score" in security_gaps:
        extra.extend([ResearchCapability.BASIC_INFO, ResearchCapability.SHAREHOLDER_EQUITY])
        methods.update({"get_basic_info", "get_shareholder_equity"})
    target = pipeline._validated_target(request, target)
    calls = [call for call in pipeline._calls_for(request, intent, target, extra)
             if (call.method in methods or call.key in conflicted_keys)
             and audit.capability_errors.get(call.label, {}).get("retryable", True)
             and callable(getattr(pipeline.provider, call.method, None))]
    if not calls:
        return request, audit
    if progress_sink:
        progress_sink("补充资料")

    # Force an actual fresh read on the first repair; share successful public
    # repair facts for 30 seconds for pre-analysis gaps. Conflicts/rejected
    # references always bypass that cooldown. Empty/failure results never cache.
    timings: dict[str, float] = {}
    cached_calls: list[str] = []
    async def fetch(call):
        started = perf_counter()
        try:
            facts, cached = await pipeline.cache.get(
                pipeline._cache_key(call), lambda: pipeline._execute(call), pipeline.now,
                force_refresh=True, refresh_cooldown_seconds=30 if advice is None else 0,
            )
            if cached:
                cached_calls.append(call.label)
            return [fact.model_copy(update={"produced_by": call.key}) for fact in facts]
        finally:
            timings[call.label] = round((perf_counter() - started) * 1000, 2)

    results = await asyncio.gather(*(fetch(call) for call in calls), return_exceptions=True)
    fetched, successful, empty, failed = [], [], [], []
    errors = {}
    for call, result in zip(calls, results, strict=True):
        if isinstance(result, BaseException):
            failed.append(call.label)
            errors[call.label] = failure_summary(result)
        else:
            current = [fact for fact in result if fact_is_current(fact, pipeline.now())]
            if current:
                fetched.extend(current)
                successful.append(call.label)
            else:
                empty.append(call.label)
    updates = dict(
        recovery_rounds=1, recovery_capabilities=[call.label for call in calls],
        recovery_successful_capabilities=successful, recovery_empty_capabilities=empty,
        recovery_failed_capabilities=failed,
        recovery_errors=errors,
        recovery_phase="before_analysis" if advice is None else "after_analysis",
        recovery_missing_fields_before=gaps,
        recovery_timings_ms=timings,
        recovery_cached_capabilities=cached_calls,
    )
    if not fetched:
        return request, audit.model_copy(update=updates)
    # Recompute our rule-derived scores from all retained source evidence. Old
    # conflicting source records remain available to the verifier and audit.
    roots = [fact for fact in request.facts if not fact.source_id.startswith("DERIVED_RULE_")]
    roots = _merge_by_fact_id([*roots, *fetched])
    derived = pipeline._derive(roots)
    facts = _merge_by_fact_id([*roots, *derived])
    updates.update(
        mode=("mixed" if audit.supplied_fact_count else "live") if audit.mode == "unavailable"
             else "mixed" if audit.mode == "reused" else audit.mode,
        fetched_fact_count=audit.fetched_fact_count + len(_merge_by_fact_id(fetched)),
        derived_fact_count=max(0, audit.derived_fact_count
                            - sum(fact.source_id.startswith("DERIVED_RULE_") for fact in request.facts)
                            + len(derived)),
        missing_fields_by_agent=missing_fields_by_agent(facts, intent, now=pipeline.now()),
        message="系统已自动补取相关资料，新增资料将重新进入分析、事实核验与风险检查。",
    )
    return request.model_copy(update={"facts": facts}), audit.model_copy(update=updates)
