"""Request-local model accounting; never stores prompts or credentials."""
from contextvars import ContextVar
from typing import Any

analysis_telemetry: ContextVar[dict[str, Any] | None] = ContextVar("analysis_telemetry", default=None)


def summarize_costs(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """An unknown billable call keeps the total unknown, never silently zero."""
    known = [call["estimated_cost_usd"] for call in calls if call.get("estimated_cost_usd") is not None]
    return {"currency": "USD", "status": "complete" if len(known) == len(calls) else "incomplete",
            "estimated_total": round(sum(known), 8) if len(known) == len(calls) else None,
            "known_subtotal": round(sum(known), 8), "unknown_calls": len(calls) - len(known),
            "cache_hits": sum(bool(call.get("cache_hit")) for call in calls)}
