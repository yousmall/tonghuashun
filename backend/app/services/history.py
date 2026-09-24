"""历史记录的轻量存储格式。

`AdvicePackage` 的完整证据包可能包含上千条事实（单条消息可达 1MB），直接写进
MySQL 会撑大行宽、拖慢查询，并在 filesort 时触发 `Out of sort memory`。历史记录
只需要让用户回看当时的结论、风险与依据，因此这里保留展示所需字段，并对证据
条数设上限。
"""

from __future__ import annotations

from typing import Any

from backend.app.models import FactRecord

# 保留在历史记录里的证据条数上限。界面只展示实际引用的证据，因此按引用集合
# 截断即可，不影响结论的可追溯性。
MAX_STORED_EVIDENCE = 60

_FACT_KEYS = ("fact_id", "entity", "field", "value", "snapshot_time", "source_id", "source_url", "quality", "period")


def summarise_advice(advice: dict[str, Any], used_fact_ids: set[str] | None = None) -> dict[str, Any]:
    """把完整建议包压缩为历史记录可展示的轻量结构。"""

    kept: list[dict[str, Any]] = []
    for fact in advice.get("facts") or []:
        if not isinstance(fact, dict):
            continue
        fact_id = fact.get("fact_id")
        if used_fact_ids and fact_id not in used_fact_ids:
            continue
        kept.append({key: fact[key] for key in _FACT_KEYS if key in fact})
        if len(kept) >= MAX_STORED_EVIDENCE:
            break

    summary: dict[str, Any] = {
        "trace_id": advice.get("trace_id"),
        "profile_version": advice.get("profile_version"),
        "snapshot_time": advice.get("snapshot_time"),
        "conclusion": advice.get("conclusion"),
        "risk_conclusion": advice.get("risk_conclusion"),
        "risks": list(advice.get("risks") or []),
        "next_steps": list(advice.get("next_steps") or []),
        "evidence": list(advice.get("evidence") or []),
        "compliance": advice.get("compliance"),
        "data_acquisition": _compact_acquisition(advice.get("data_acquisition")),
        "cross_validation": _compact_cross_validation(advice.get("cross_validation")),
        "agent_results": _compact_agent_results(advice.get("agent_results")),
        "facts": kept,
        "facts_truncated": len(kept) < len(advice.get("facts") or []),
    }
    if advice.get("user_fit"):
        summary["user_fit"] = advice["user_fit"]
    if advice.get("allocation"):
        summary["allocation"] = advice["allocation"]
    if advice.get("task_plan"):
        plan = advice["task_plan"] or {}
        summary["task_plan"] = {
            "clarification_question": plan.get("clarification_question"),
            "nodes": [
                {"agent_id": node.get("agent_id"), "status": node.get("status")}
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
            "fetched_fact_count",
        )
        if acquisition.get(key) is not None
    }


def _compact_cross_validation(cross_validation: Any) -> dict[str, Any] | None:
    if not isinstance(cross_validation, dict):
        return None
    return {
        "status": cross_validation.get("status"),
        "issues": [
            {"code": issue.get("code"), "message": issue.get("message")}
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
                    "confidence",
                    "confidence_reasons",
                    "risk_flags",
                    "invalidation_conditions",
                    "details",
                )
                if result.get(key) is not None
            }
        )
    return compact


def facts_as_dicts(facts: list[FactRecord]) -> list[dict[str, Any]]:
    """把事实记录转为可 JSON 序列化的摘要（供历史记录使用）。"""

    return [{key: value for key, value in fact.model_dump(mode="json").items() if key in _FACT_KEYS}
            for fact in facts]
