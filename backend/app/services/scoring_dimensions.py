"""Versioned evidence-backed proxies, with explicit inputs and no document counts."""
from backend.app.fact_taxonomy import NEWS_FIELDS, fact_is_current

QUALITATIVE_RUBRICS = {
    "policy": {"supportive": 75, "neutral": 50, "restrictive": 25},
    "event": {"favorable": 75, "neutral": 50, "adverse": 25},
    "audit_opinion": {"unqualified": 100, "qualified": 50, "adverse": 0, "disclaimer": 0},
    "regulatory_status": {"explicitly_clear": 100, "penalty": 0},
    "disclosure_status": {"timely": 100, "delayed": 0},
}


def additional_dimensions(by_field, *, now, numeric, emit):
    """All callbacks operate on one entity; output retains every input reference."""
    clamp = lambda value: max(0, min(100, value))
    for raw, output, formula, rule in (
        ("m2_growth", "liquidity_score", lambda x: clamp(50 + (x - 8) * 5), "M2_YOY_PROXY_V1: clip(50+5*(m2_growth_pct-8))"),
        ("market_advancing_ratio", "risk_appetite_score", lambda x: x, "MARKET_BREADTH_V1: advancing_pct"),
        ("industry_revenue_growth", "prosperity_score", lambda x: clamp(50 + x * 2), "INDUSTRY_REVENUE_V1: clip(50+2*yoy_pct)"),
        ("industry_turnover_percentile", "crowding_score", lambda x: 100 - x, "CROWDING_V1: 100-turnover_percentile_pct; higher=less_crowded"),
    ):
        source = numeric(by_field.get(raw, []), now)
        if source and (raw not in {"market_advancing_ratio", "industry_turnover_percentile"} or 0 <= source[1] <= 100):
            emit(output, formula(source[1]), [source[0]], rule)

    flow = numeric(by_field.get("capital_flow", []), now)
    turnover = numeric(by_field.get("turnover_value", []), now)
    if (flow and turnover and turnover[1] > 0 and abs(flow[1]) <= turnover[1]
            and flow[0].unit in {"CNY", "万元", "亿元"} and flow[0].unit == turnover[0].unit
            and flow[0].period and flow[0].period == turnover[0].period):
        emit("capital_flow_score", clamp(50 + 50 * flow[1] / turnover[1]), [flow[0], turnover[0]],
             "CAPITAL_FLOW_V1: clip(50+50*net_flow/turnover); same_currency_period")

    documents = {fact.fact_id: fact for records in by_field.values() for fact in records
                 if fact.field in {*NEWS_FIELDS, "event"}
                 and fact.source_url and fact.period and fact_is_current(fact, now)}
    for dimension, criteria in (("policy", ("policy",)), ("event", ("event",)),
                                ("governance", ("audit_opinion", "regulatory_status", "disclosure_status"))):
        assessments = sorted(by_field.get(dimension + "_assessment", []), key=lambda f: f.snapshot_time, reverse=True)
        for assessment in assessments:
            raw = assessment.value
            if (not fact_is_current(assessment, now) or not isinstance(raw, dict)
                    or raw.get("rubric") != dimension.upper() + "_V1"
                    or raw.get("complete") is not True):
                continue
            values, parents = [], [assessment]
            for criterion in criteria:
                item = raw.get(criterion)
                if not isinstance(item, dict):
                    break
                label, evidence_id = item.get("label"), item.get("evidence_id")
                if not isinstance(label, str) or not isinstance(evidence_id, str):
                    break
                score = QUALITATIVE_RUBRICS[criterion].get(label)
                doc = documents.get(evidence_id)
                if score is None or doc is None or doc.entity != assessment.entity:
                    break
                values.append(score)
                parents.append(doc)
            if len(values) == len(criteria):
                emit(dimension + "_score", sum(values) / len(values), parents,
                     dimension.upper() + "_V1: explicit_complete_assessment_with_linked_documents")
                break
