"""Compact model-only payloads; audit and rule inputs remain complete."""
from __future__ import annotations

from typing import Any
from backend.app.models import AgentResult, FactRecord, UserProfile


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
    data = profile.model_dump(mode="json", exclude={"user_id", "risk_answers", "scoring_breakdown"} | (exclude or set()))
    # These answers already appear in questionnaire_details; keep the reasoning
    # summary and question IDs so the model can still distinguish its basis.
    if data.get("questionnaire_details"):
        data["assessment_reasons"] = [
            {key: value for key, value in reason.items() if key != "answers"}
            for reason in data.get("assessment_reasons", [])
        ]
    return _without_empty_fields(data)


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
