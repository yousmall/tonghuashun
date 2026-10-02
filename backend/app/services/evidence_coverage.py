"""Required dimensions and evidence coverage, independent of model assertions."""
from __future__ import annotations

from datetime import datetime, timezone
from collections import defaultdict
import math

from backend.app.fact_taxonomy import fact_is_current
from backend.app.models import FactRecord, Intent

AGENT_FIELDS = {
    "market": ("growth_score", "inflation_score", "liquidity_score", "policy_score", "risk_appetite_score"),
    "industry": ("prosperity_score", "valuation_score", "capital_flow_score", "policy_score", "crowding_score"),
    "security": ("fundamental_score", "valuation_score", "technical_score", "event_score", "governance_score"),
    "fund": ("fund_score", "fund_risk_level", "liquidity_score"),
    "portfolio": ("weight",),
}
INTENT_AGENTS = {
    Intent.MARKET_ANALYSIS: ("market", "industry"),
    Intent.INDUSTRY_ANALYSIS: ("market", "industry"),
    Intent.SECURITY_RESEARCH: ("market", "industry", "security"),
    Intent.CONVERTIBLE_BOND_ANALYSIS: ("market", "industry", "security"),
    Intent.FUND_SCREENING: ("market", "fund"),
    Intent.PORTFOLIO_REVIEW: ("market", "industry", "security", "fund", "portfolio"),
}


def missing_fields_by_agent(facts: list[FactRecord], intent: Intent, *, now: datetime | None = None) -> dict[str, list[str]]:
    by_entity: dict[str, set[str]] = defaultdict(set)
    current = now or datetime.now(timezone.utc)
    for fact in facts:
        if (fact_is_current(fact, current) and isinstance(fact.value, (int, float))
                and not isinstance(fact.value, bool) and math.isfinite(fact.value)
                and 0 <= fact.value <= 100):
            by_entity[fact.entity].add(fact.field.casefold())
    gaps = {}
    for agent in INTENT_AGENTS.get(intent, ()):
        required = AGENT_FIELDS[agent]
        # Report one coherent entity's coverage, never assemble a complete
        # dimension set out of unrelated securities or industries.
        best = max(by_entity.values(), key=lambda fields: len(fields.intersection(required)), default=set())
        missing = [field for field in required if field not in best]
        if missing:
            gaps[agent] = missing
    return gaps
