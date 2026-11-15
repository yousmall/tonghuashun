"""Compact model-only payloads; audit and rule inputs remain complete."""
from __future__ import annotations

from typing import Any
from copy import deepcopy
from backend.app.models import AgentResult, FactRecord, UserProfile


_REFERENCE_FIELDS = frozenset({"fact_id", "facts_used", "derived_from", "evidence_id"})
_LITERAL_FIELDS = frozenset({"value", "normalized_value", "quote"})


def rewrite_fact_references(value: Any, mapping: dict[str, str], *, reference: bool = False) -> Any:
    """Rewrite reference fields only; source values and quoted evidence stay literal."""
    if isinstance(value, str):
        return mapping.get(value, value) if reference else value
    if isinstance(value, list):
        return [rewrite_fact_references(item, mapping, reference=reference) for item in value]
    if isinstance(value, dict):
        return {key: deepcopy(item) if key in _LITERAL_FIELDS else
                rewrite_fact_references(item, mapping, reference=key in _REFERENCE_FIELDS)
                for key, item in value.items()}
    return value


def compact_fact_references(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Use short model-local IDs, returning the inverse map for normal validation.

    Cache identity must still use the original payload. Never shorten references
    to absent facts or change evidence text, numbers, dates, units or source URLs.
    """
    facts = payload.get("authorized_facts") or []
    reserved: set[str] = set()

    def collect(value: Any, reference: bool = False) -> None:
        if isinstance(value, str) and reference:
            reserved.add(value)
        elif isinstance(value, list):
            for item in value:
                collect(item, reference)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key not in _LITERAL_FIELDS:
                    collect(item, key in _REFERENCE_FIELDS)

    collect(payload)
    aliases: dict[str, str] = {}
    index = 1
    for fact in facts:
        fact_id = fact.get("fact_id")
        if not isinstance(fact_id, str) or len(fact_id) <= 12 or fact_id in aliases:
            continue
        while f"f{index}" in reserved:
            index += 1
        alias = f"f{index}"
        aliases[fact_id] = alias
        reserved.add(alias)
        index += 1
    return rewrite_fact_references(payload, aliases), {alias: original for original, alias in aliases.items()}


def _without_empty_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Only remove top-level absent values; retain false, zero and nested source text."""
    return {key: value for key, value in data.items()
            if value is not None and value != [] and value != {}}


def profile_for_model(profile: UserProfile, *, exclude: set[str] | None = None) -> dict[str, Any]:
    """Send suitability context once, without the duplicated questionnaire audit trail.

    Full question/answer text is retained: debt type and behavioral answers are not
    always represented by the risk score. Holdings, trading analysis, financial
    policies, warnings and scenario assumptions also remain unchanged.
    """
    # 自动规划是展示用的重复派生结果；原始问卷、目标区间和财务约束已在下方保留。
    data = profile.model_dump(mode="json", exclude={"user_id", "risk_answers", "scoring_breakdown", "automatic_planning"} | (exclude or set()))
    # These answers already appear in questionnaire_details; keep the reasoning
    # summary and question IDs so the model can still distinguish its basis.
    if data.get("questionnaire_details"):
        data["assessment_reasons"] = [
            {key: value for key, value in reason.items() if key != "answers"}
            for reason in data.get("assessment_reasons", [])
        ]
    return _without_empty_fields(data)


def profile_for_routing(profile: UserProfile) -> dict[str, Any]:
    """Intent/risk routing needs suitability signals, not full financial or trade history.

    This view is used only before research. Specialists, scoring and final review
    continue to receive the full profile and questionnaire evidence.
    """
    fields = {"confirmed", "risk_level", "constraints", "target", "preferred_product_types",
              "suitable_product_levels", "horizon_months", "investment_horizon_label",
              "liquidity_need", "loss_tolerance_label", "max_drawdown", "financial_warnings"}
    return _without_empty_fields(profile.model_dump(mode="json", include=fields))


def fact_for_model(fact: FactRecord) -> dict[str, Any]:
    """Compact a fact without changing its value, units, dates or evidence lineage."""
    raw = fact.model_dump(mode="json", exclude={"produced_by"})
    data = _without_empty_fields(raw)
    # A source value of null is itself evidence of a missing value, not an
    # optional display field. Do not filter inside arbitrary source objects.
    data["value"] = raw["value"]
    return data


def baseline_for_model(baseline: AgentResult) -> dict[str, Any]:
    """Keep rule constraints and details; references live in authorized_facts."""
    data = _without_empty_fields(baseline.model_dump(mode="json", exclude={
        "citations", "rule_score", "model_score", "score_policy", "score_difference_reason",
    }))
    # Explicitly preserve an unknown rule score; the model must not fill it in.
    data["score"] = baseline.score
    return data


def compact_model_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Remove generated display titles, preserving all JSON Schema validation.

    Keep the root title for telemetry/cache dispatch and descriptions, defaults,
    enums, bounds, required keys, $defs/$refs and extra-field restrictions. The
    property named 'title' is a field, not a removable annotation.
    """
    def visit(value: Any, *, root: bool = False) -> Any:
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if key == "title" and not root:
                continue
            if key in {"properties", "$defs", "definitions", "patternProperties", "dependentSchemas"}:
                result[key] = {name: visit(child) for name, child in item.items()}
            elif key in {"default", "const", "enum", "examples"}:
                result[key] = item
            else:
                result[key] = visit(item)
        return result
    return visit(schema, root=True)
