"""历史记录的轻量存储格式。

`AdvicePackage` 的完整证据包可能包含上千条事实（单条消息可达 1MB），直接写进
MySQL 会撑大行宽、拖慢查询，并在 filesort 时触发 `Out of sort memory`。历史记录
保留展示所需字段及有限预览；完整引用和派生血缘另存于 research_evidence，
读取详情时由账号与消息 ID 关联恢复。
"""

from __future__ import annotations

from typing import Any

from backend.app.models import FactRecord

# 消息行只保留预览；完整引用和派生血缘由 database 的独立证据表保存。
MAX_STORED_EVIDENCE = 60

_FACT_KEYS = ("fact_id", "entity", "entity_code", "field", "value", "unit", "normalized_value", "snapshot_time", "source_id", "source_url", "source_field", "quality", "period", "observation_date", "derived_from", "produced_by", "derivation_rule")


def summarise_advice(advice: dict[str, Any], used_fact_ids: set[str] | None = None) -> dict[str, Any]:
    """把完整建议包压缩为历史记录可展示的轻量结构。"""

    kept: list[dict[str, Any]] = []
    for fact in advice.get("facts") or []:
        if not isinstance(fact, dict):
            continue
        fact_id = fact.get("fact_id")
        if used_fact_ids is not None and fact_id not in used_fact_ids:
            continue
        kept.append({key: fact[key] for key in _FACT_KEYS if key in fact})
        if len(kept) >= MAX_STORED_EVIDENCE:
            break

    available = {str(fact.get("fact_id")) for fact in advice.get("facts") or [] if isinstance(fact, dict)}
    summary: dict[str, Any] = {
        "history_version": 3,
        "intent": advice.get("intent"),
        "trace_id": advice.get("trace_id"),
        "profile_version": advice.get("profile_version"),
        "snapshot_time": advice.get("snapshot_time"),
        "conclusion": advice.get("conclusion"),
        "risk_conclusion": advice.get("risk_conclusion"),
        "return_expectation": advice.get("return_expectation"),
        "risks": list(advice.get("risks") or []),
        "next_steps": list(advice.get("next_steps") or []),
        "evidence": list(advice.get("evidence") or []),
        "compliance": advice.get("compliance"),
        "data_acquisition": _compact_acquisition(advice.get("data_acquisition")),
        "cross_validation": _compact_cross_validation(advice.get("cross_validation")),
        "agent_results": _compact_agent_results(advice.get("agent_results")),
        "facts": kept,
        "facts_truncated": len(kept) < len(advice.get("facts") or []),
        "missing_evidence_ids": sorted(used_fact_ids_of(advice) - available),
        "research_mode": advice.get("research_mode", "deep"),
        "model_calls": advice.get("model_calls") or [],
        "model_cost": advice.get("model_cost") or {},
        "timings_ms": advice.get("timings_ms") or {},
    }
    if advice.get("user_fit"):
        summary["user_fit"] = advice["user_fit"]
    if advice.get("allocation"):
        summary["allocation"] = advice["allocation"]
    if advice.get("stock_recommendation"):
        summary["stock_recommendation"] = advice["stock_recommendation"]
    if advice.get("task_plan"):
        plan = advice["task_plan"] or {}
        summary["task_plan"] = {
            "clarification_question": plan.get("clarification_question"),
            "nodes": [
                {key: node.get(key) for key in ("task_id", "agent_id", "status", "depends_on")}
                for node in (plan.get("nodes") or [])
            ],
        }
    return summary


def used_fact_ids_of(advice: dict[str, Any]) -> set[str]:
    """从建议包与各专业结果中收集被引用的 evidence id。"""

    used = {str(item) for item in (advice.get("evidence") or [])}
    for result in advice.get("agent_results") or []:
        if isinstance(result, dict):
            used.update(str(item) for item in (result.get("facts_used") or []))
    for issue in (advice.get("cross_validation") or {}).get("issues") or []:
        if isinstance(issue, dict):
            used.update(str(item) for item in issue.get("fact_ids") or [])
    recommendation = advice.get("stock_recommendation") or {}
    for candidate in [*(recommendation.get("candidates") or []), *(recommendation.get("recommendations") or [])]:
        if isinstance(candidate, dict):
            used.update(str(item) for item in candidate.get("evidence") or [])
    # 保存派生结论的原始输入，避免历史里只剩一个不可回溯的评分。
    facts = {str(fact.get("fact_id")): fact for fact in advice.get("facts") or [] if isinstance(fact, dict)}
    pending = list(used)
    while pending:
        fact = facts.get(pending.pop(), {})
        for parent in fact.get("derived_from") or []:
            parent = str(parent)
            if parent not in used:
                used.add(parent)
                pending.append(parent)
    return used


def _compact_acquisition(acquisition: Any) -> dict[str, Any] | None:
    if not isinstance(acquisition, dict):
        return None
    return {
        key: acquisition.get(key)
        for key in (
            "mode",
            "requested_capabilities",
            "successful_capabilities",
            "empty_capabilities",
            "failed_capabilities",
            "capability_errors", "recovery_errors",
            "capability_evidence", "recovery_evidence", "recovery_deferred_capabilities", "recovery_gap_metrics",
            "fetched_fact_count",
            "missing_fields_by_agent",
            "capability_timings_ms",
            "cached_capabilities",
            "recovery_rounds", "recovery_phases", "recovery_attempts", "recovery_agent_requirements",
            "recovery_capabilities", "recovery_successful_capabilities",
            "recovery_empty_capabilities", "recovery_failed_capabilities", "recovery_reanalyzed",
            "assessment_status", "assessment_targets", "recovery_phase", "recovery_capabilities",
        )
        if acquisition.get(key) is not None
    }


def _compact_cross_validation(cross_validation: Any) -> dict[str, Any] | None:
    if not isinstance(cross_validation, dict):
        return None
    return {
        "status": cross_validation.get("status"),
        "issues": [
            {key: issue.get(key) for key in ("code", "message", "fact_ids")}
            for issue in (cross_validation.get("issues") or [])
            if isinstance(issue, dict)
        ][:20],
    }


def _compact_agent_results(results: Any) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for result in results or []:
        if not isinstance(result, dict):
            continue
        compact.append(
            {
                key: result.get(key)
                for key in (
                    "agent_id",
                    "status",
                    "opinion",
                    "score",
                    "rule_score",
                    "model_score",
                    "score_policy",
                    "score_difference_reason",
                    "confidence",
                    "confidence_reasons",
                    "risk_flags",
                    "invalidation_conditions",
                    "details",
                    "facts_used",
                )
                if result.get(key) is not None
            }
        )
    return compact


def cited_facts_of(advice: dict[str, Any]) -> list[dict[str, Any]]:
    """Complete cited evidence, including transitive derived inputs, without a cap."""
    used = used_fact_ids_of(advice)
    return [{key: fact[key] for key in _FACT_KEYS if key in fact}
            for fact in advice.get("facts") or []
            if isinstance(fact, dict) and str(fact.get("fact_id")) in used]


def facts_as_dicts(facts: list[FactRecord]) -> list[dict[str, Any]]:
    """把事实记录转为可 JSON 序列化的摘要（供历史记录使用）。"""

    return [{key: value for key, value in fact.model_dump(mode="json").items() if key in _FACT_KEYS}
            for fact in facts]
