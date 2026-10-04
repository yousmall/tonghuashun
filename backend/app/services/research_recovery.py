"""Bounded pre-analysis and agent-directed read-only evidence recovery."""
from __future__ import annotations

import asyncio
from typing import Callable
from dataclasses import replace
from time import perf_counter

from backend.app.fact_taxonomy import FAST_MARKET_FIELDS, SLOW_FINANCIAL_FIELDS, fact_is_current
from backend.app.models import (
    AdvicePackage, ComplianceStatus, DataAcquisitionResult, Intent,
    OrchestrationRequest, ResearchCapability, TaskStatus,
)
from backend.app.risk_questionnaire import assessment_is_current
from backend.app.services.evidence_coverage import AGENT_FIELDS, missing_fields_by_agent
from backend.app.services.research import AutomatedResearchPipeline, CAPABILITY_METHODS, _merge_by_fact_id
from backend.app.services.agent_data_requirements import agent_requirements
from backend.app.services.provider_errors import failure_summary
from backend.app.services.research_requirements import evidence_status, select_calls
from backend.app.services.on_demand_research import (
    MAX_GAP_CALLS, CALL_FAMILIES, gap_calls, prune_coarse_calls, derive_gap_metrics, assess_gap_evidence, read_gap_documents,
    align_security_documents,
)


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
    semantic=None,
) -> tuple[OrchestrationRequest, DataAcquisitionResult]:
    """Each phase may repair once; agent requests cannot create an unbounded loop."""
    phase = "before_analysis" if advice is None else "after_analysis"
    phases = list(audit.recovery_phases)
    if not phases and audit.recovery_rounds:
        if audit.recovery_phase is None:
            return request, audit  # legacy audit without phase: conservatively stop
        phases = [audit.recovery_phase]
    if (phase in phases or audit.recovery_rounds >= 2 or not request.auto_fetch or pipeline.provider is None
            or not assessment_is_current(request.profile)
            or (advice is not None and (not advice.agent_results
                or advice.compliance.status is ComplianceStatus.BLOCK))
            or intent in {Intent.UNKNOWN, Intent.EDUCATION}):
        return request, audit

    gaps = missing_fields_by_agent(request.facts, intent, now=pipeline.now())
    requested_by_agents = agent_requirements(advice.agent_results) if advice else {}
    requested_capabilities = list(dict.fromkeys(cap for caps in requested_by_agents.values() for cap in caps))
    # A permission/quota failure remains binding under another call label.
    prior_errors = {**audit.capability_errors, **audit.recovery_errors}
    for attempt in audit.recovery_attempts:
        prior_errors.update(attempt.get("blocked_families", {}))
    for label, error in list(prior_errors.items()):
        if error.get("retryable") is False and ":" in label:
            prior_errors.setdefault(label.split(":", 1)[0], error)
    # Do not repeat the same unresolved preflight gaps without a new signal.
    if phase == "after_analysis" and "before_analysis" in phases and not requested_capabilities:
        special_issue = any(issue.code in CONFLICT_CODES | {"EVIDENCE_REFERENCE_REJECTED"}
                            for issue in advice.cross_validation.issues)
        new_fields = any(set(fields) - set(audit.recovery_missing_fields_before.get(agent, ()))
                         for agent, fields in gaps.items())
        declared_new = any({field for field in (result.details or {}).get("missing_fields", ())
                            if isinstance(field, str)}
                           - set(audit.recovery_missing_fields_before.get(result.agent_id, ()))
                           for result in advice.agent_results
                           if isinstance((result.details or {}).get("missing_fields", ()), list))
        rejected = any((r.details or {}).get("rejected_reference_count") for r in advice.agent_results)
        if not (special_issue or new_fields or declared_new or rejected):
            return request, audit
    if (prior_errors and not audit.successful_capabilities and not audit.reused_capabilities
            and not audit.recovery_successful_capabilities
            and all(error.get("code") == "AUTHENTICATION_REJECTED" for error in prior_errors.values())):
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

    extra = list(dict.fromkeys([*(data_requirements or []), *requested_capabilities]))
    requested_methods = {CAPABILITY_METHODS[cap] for cap in requested_capabilities}
    methods.update(requested_methods)
    if "governance_score" in security_gaps:
        extra.extend([ResearchCapability.BASIC_INFO, ResearchCapability.SHAREHOLDER_EQUITY])
        methods.update({"get_basic_info", "get_shareholder_equity"})
    target = pipeline._validated_target(request, target)
    deadline = perf_counter() + pipeline.recovery_timeout_seconds
    remaining = lambda: max(0, deadline - perf_counter())
    calls = [call for call in pipeline._calls_for(request, intent, target, extra)
             if (call.method in methods or call.key in conflicted_keys)
             and prior_errors.get(call.label, {}).get("retryable", True)
             and not any(prior_errors.get(family, {}).get("retryable") is False
                         for family in CALL_FAMILIES.get(call.method, ()))
             and callable(getattr(pipeline.provider, call.method, None))]
    guarded_audit = audit.model_copy(update={"capability_errors": prior_errors})
    targeted = gap_calls(pipeline, request, intent, target, guarded_audit)
    # Keep explicit agent requests even when a broad default call is pruned.
    agent_calls = [replace(call, priority=95) for call in calls if call.method in requested_methods]
    calls = [*prune_coarse_calls(calls, targeted, pipeline, request, intent, target), *targeted, *agent_calls]
    # Reserve the five dependent industry calls until company basic info resolves
    # its industry. Rank calls by evidence need, preserving history/calendar pairs.
    budget = MAX_GAP_CALLS - 5 if any(c.label.startswith("security_scope:") for c in calls) else MAX_GAP_CALLS
    calls, deferred = select_calls(pipeline, calls, budget)
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

    fetched, successful, empty, failed = [], [], [], []
    errors = {}
    fulfillment = {}
    attempted = []
    async def execute(batch):
        timeout = min(pipeline.call_timeout_seconds, remaining())
        results = await asyncio.gather(*(asyncio.wait_for(fetch(call), timeout=timeout) for call in batch),
                                       return_exceptions=True)
        for call, result in zip(batch, results, strict=True):
            attempted.append(call)
            if isinstance(result, BaseException):
                failed.append(call.label)
                errors[call.label] = failure_summary(result)
                if isinstance(result, TimeoutError):
                    errors[call.label]["code"] = "RECOVERY_TIMEOUT"
                fulfillment[call.label] = {"status": "failed", "reason_codes": [errors[call.label]["code"]]}
            else:
                current = [fact for fact in result if fact_is_current(fact, pipeline.now())]
                fulfillment[call.label] = evidence_status(call, current, pipeline.now())
                if current:
                    fetched.extend(current)
                    successful.append(call.label)
                else:
                    empty.append(call.label)
    await execute(calls)
    # 首批基本信息可能才返回所属行业；只为新解析出的范围追加未调用能力，共享本轮预算。
    interim = request.model_copy(update={"facts": _merge_by_fact_id([*request.facts, *fetched])})
    interim = derive_gap_metrics(pipeline, interim, intent, target)
    known_keys = {pipeline._cache_key(call) for call in attempted}
    guarded_errors = {**prior_errors, **errors}
    for call in attempted:
        if errors.get(call.label, {}).get("retryable") is False:
            for family in CALL_FAMILIES.get(call.method, ()):
                guarded_errors[family] = errors[call.label]
    interim_audit = audit.model_copy(update={"capability_errors": guarded_errors})
    followups = [call for call in [*gap_calls(pipeline, interim, intent, target, interim_audit), *deferred]
                 if pipeline._cache_key(call) not in known_keys]
    # Deferred calls also respect permission failures learned in the first batch.
    followups = [c for c in followups if not any(guarded_errors.get(family, {}).get("retryable") is False
                  for family in CALL_FAMILIES.get(c.method, ())) and
                 guarded_errors.get(c.label, {}).get("retryable") is not False]
    followups, deferred = select_calls(pipeline, followups, MAX_GAP_CALLS - len(attempted))
    if remaining() > 0:
        await execute(followups)
    else:
        deferred = [*followups, *deferred]
    enriched = request.model_copy(update={"facts": _merge_by_fact_id([*request.facts, *fetched])})
    enriched = align_security_documents(pipeline, enriched, intent, target)
    aligned_ids = {f.fact_id: f for f in enriched.facts}
    fetched = [aligned_ids[f.fact_id] for f in fetched]
    documents, document_calls, document_success, document_empty, document_errors = await read_gap_documents(
        pipeline, enriched, intent, target, fetched, timeout_seconds=remaining())
    enriched = enriched.model_copy(update={"facts": _merge_by_fact_id([*enriched.facts, *documents])})
    successful.extend(document_success)
    empty.extend(document_empty)
    errors.update(document_errors)
    failed.extend(document_errors)
    enriched = derive_gap_metrics(pipeline, enriched, intent, target)
    assessed, assessment_status, targets = await assess_gap_evidence(pipeline, enriched, intent, target, semantic,
                                                                   timeout_seconds=remaining())
    added_ids = {f.fact_id for f in assessed.facts} - {f.fact_id for f in request.facts}
    if not attempted and not added_ids and assessment_status == "not_required":
        return request, audit
    for call in deferred:
        fulfillment[call.label] = {"status": "deferred", "required_fields": list(call.required_fields),
                                  "reason_codes": ["RECOVERY_TIME_BUDGET" if remaining() <= 0 else "CALL_BUDGET"]}
    updates = dict(
        recovery_rounds=audit.recovery_rounds + 1, recovery_phases=[*phases, phase],
        recovery_agent_requirements={**audit.recovery_agent_requirements, **requested_by_agents},
        recovery_capabilities=[call.label for call in attempted] + document_calls,
        recovery_successful_capabilities=successful, recovery_empty_capabilities=empty,
        recovery_failed_capabilities=failed,
        recovery_errors=errors,
        recovery_evidence=fulfillment,
        recovery_deferred_capabilities=[c.label for c in deferred],
        recovery_gap_metrics={"missing_before": sum(map(len, gaps.values())),
            "missing_after": sum(map(len, missing_fields_by_agent(assessed.facts, intent, now=pipeline.now()).values())),
            "capability_calls": len(attempted), "deferred_calls": len(deferred),
            "complete_calls": sum(v["status"] == "complete" for v in fulfillment.values()),
            "partial_calls": sum(v["status"] == "partial" for v in fulfillment.values())},
        recovery_phase=phase,
        recovery_missing_fields_before=gaps,
        recovery_timings_ms=timings,
        recovery_cached_capabilities=cached_calls,
        assessment_status=assessment_status, assessment_targets=targets,
        missing_fields_by_agent=missing_fields_by_agent(assessed.facts, intent, now=pipeline.now()),
    )
    updates["recovery_attempts"] = [*audit.recovery_attempts, {
        "phase": phase, "agent_requirements": requested_by_agents,
        "capabilities": updates["recovery_capabilities"],
        "successful_capabilities": successful, "empty_capabilities": empty,
        "failed_capabilities": failed, "errors": errors, "evidence": fulfillment,
        "blocked_families": {family: errors[call.label] for call in attempted
            if errors.get(call.label, {}).get("retryable") is False
            for family in CALL_FAMILIES.get(call.method, ())},
        "missing_fields_before": gaps, "missing_fields_after": updates["missing_fields_by_agent"],
    }]
    if not fetched and not added_ids:
        return request, audit.model_copy(update=updates)
    # Recompute our rule-derived scores from all retained source evidence. Old
    # conflicting source records remain available to the verifier and audit.
    facts = assessed.facts
    derived = [f for f in facts if f.source_id.startswith("DERIVED_")]
    updates.update(
        mode=("mixed" if audit.supplied_fact_count else "live") if audit.mode == "unavailable"
             else "mixed" if audit.mode == "reused" else audit.mode,
        fetched_fact_count=audit.fetched_fact_count + len(_merge_by_fact_id([*fetched, *documents])),
        derived_fact_count=max(0, audit.derived_fact_count
                            - sum(fact.source_id.startswith("DERIVED_RULE_") for fact in request.facts)
                            + len(derived)),
        missing_fields_by_agent=missing_fields_by_agent(facts, intent, now=pipeline.now()),
        message="系统已自动补取相关资料，新增资料将重新进入分析、事实核验与风险检查。",
    )
    return request.model_copy(update={"facts": facts}), audit.model_copy(update=updates)
