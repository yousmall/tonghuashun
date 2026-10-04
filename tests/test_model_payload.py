"""Model compression must preserve suitability, original evidence and validation."""
import json
from copy import deepcopy
from datetime import datetime, timezone

import httpx
import pytest

from backend.app.agents.llm_agents import HybridInvestmentAgent, LLMConfig, OpenAICompatibleLLM
from backend.app.model_payload import baseline_for_model, compact_model_schema, fact_for_model, profile_for_model
from backend.app.models import AgentResult, FactRecord, OrchestrationRequest, TaskStatus, UserProfile
from backend.app.risk_questionnaire import QUESTIONS, evaluate_answers
from backend.app.semantic import (
    OutputReview, ProfileExtraction, RequestUnderstanding, SemanticService, StockEvidenceReview,
)
from backend.app.services.model_telemetry import analysis_telemetry


def profile():
    answers = dict(zip((q.id for q in QUESTIONS), "ACABDC CACCB BCCBC CDB CADAECDC".replace(" ", ""), strict=True))
    return UserProfile(**evaluate_answers(answers), user_id="private-account", confirmed=False,
                       constraints=["不得使用杠杆"], single_security_limit=.1,
                       holding_history=[{"entity": "示例", "weight": 0}],
                       trading_analysis={"warnings": ["持仓集中"], "trade_count": 0})


def fact(**updates):
    return FactRecord(**dict(dict(fact_id="F1", entity="示例", field="change", value=0,
                                 unit="%", snapshot_time=datetime.now(timezone.utc), quality=.8,
                                 source_id="DEMO_SNAPSHOT", source_url="https://example.com/evidence",
                                 source_field="涨跌幅", observation_date="2026-10-04",
                                 period="2026Q3", entity_code="600000", produced_by="quote@示例"), **updates))


def test_profile_compression_retains_questions_and_all_business_constraints():
    original = profile()
    before = original.model_dump(mode="json")
    payload = profile_for_model(original)
    assert original.model_dump(mode="json") == before
    assert not {"user_id", "risk_answers", "scoring_breakdown"} & payload.keys()
    for key in ("questionnaire_details", "risk_level", "risk_score", "financial_plan", "financial_warnings",
                "allocation_guidance", "stress_scenarios", "confirmed", "single_security_limit",
                "industry_limit", "constraints", "holding_history", "trading_analysis"):
        assert payload[key] == before[key]
    assert payload["confirmed"] is False
    assert all("answers" not in item for item in payload["assessment_reasons"])
    assert len(json.dumps(payload, ensure_ascii=False)) < len(json.dumps(before, ensure_ascii=False))


def test_reason_evidence_is_retained_when_no_duplicate_questionnaire_text_exists():
    original = UserProfile(assessment_reasons=[{"summary": "有短期债务", "answers": ["信用卡欠款"]}])
    assert profile_for_model(original)["assessment_reasons"] == original.assessment_reasons


@pytest.mark.parametrize("value", [None, 0, False, {}, {"title": "原文", "null": None, "empty": [], "zero": 0}])
def test_facts_preserve_literal_values_units_dates_and_full_lineage(value):
    original = fact(value=value, derived_from=["parent"], derivation_rule="公开规则v1")
    before = original.model_dump(mode="json")
    payload = fact_for_model(original)
    assert original.model_dump(mode="json") == before
    assert payload["value"] == before["value"]
    assert "produced_by" not in payload
    for key in ("fact_id", "entity", "field", "unit", "snapshot_time", "source_id", "source_url",
                "source_field", "quality", "period", "observation_date", "entity_code", "derived_from", "derivation_rule"):
        assert payload[key] == before[key]
    if before["normalized_value"] is not None:
        assert payload["normalized_value"] == before["normalized_value"]


def test_unknown_rule_score_and_risks_are_not_removed_or_replaced():
    original = AgentResult(agent_id="security", status=TaskStatus.DEGRADED, opinion="资料不足", confidence=0,
                           score=None, facts_used=["F1"], citations=["DEMO_SNAPSHOT"],
                           risk_flags=["尚未核实"], invalidation_conditions=["公告更正"],
                           details={"equity_cap": 0, "missing": ["governance"], "unknown": None})
    before = original.model_dump(mode="json")
    payload = baseline_for_model(original)
    for key in ("status", "opinion", "score", "confidence", "facts_used", "risk_flags", "invalidation_conditions", "details"):
        assert payload[key] == before[key]
    assert "score" in payload and payload["score"] is None
    assert "citations" not in payload
    assert original.model_dump(mode="json") == before


@pytest.mark.parametrize("model", [RequestUnderstanding, OutputReview, StockEvidenceReview, ProfileExtraction])
def test_schema_compaction_changes_display_titles_only(model):
    original = model.model_json_schema()
    before = deepcopy(original)
    compact = compact_model_schema(original)
    assert compact["title"] == model.__name__
    assert compact["additionalProperties"] is False
    assert original == before
    # Restore annotations and require exact equality: all validation keywords
    # including nested required/enums/bounds/defaults/$refs must survive.
    def restore_titles(source, target):
        if isinstance(source, dict):
            if "title" in source:
                target["title"] = source["title"]
            for key, value in source.items():
                if key != "title":
                    restore_titles(value, target[key])
        elif isinstance(source, list):
            for a, b in zip(source, target, strict=True):
                restore_titles(a, b)
    restore_titles(original, compact)
    assert compact == original


def test_schema_field_named_title_and_constant_objects_are_preserved():
    schema = {"title": "Root", "properties": {"title": {"title": "Title", "type": "string"}},
              "required": ["title"], "const": {"title": "source title"}}
    compact = compact_model_schema(schema)
    assert compact["properties"]["title"] == {"type": "string"}
    assert compact["const"] == schema["const"]
    assert compact["required"] == ["title"]


@pytest.mark.asyncio
async def test_compact_payload_passes_through_http_telemetry_and_rejects_unapproved_fact():
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(json.loads(body["messages"][1]["content"]))
        content = dict(agent_id="security", status="completed", opinion="引用未授权事实", score=99,
                       confidence=.9, facts_used=["unapproved"])
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}}],
                                        "usage": {"prompt_tokens": 321, "completion_tokens": 30}})
    client = OpenAICompatibleLLM(LLMConfig("https://model.test/v1", "test-key", "test-model", max_retries=0),
                                 transport=httpx.MockTransport(handler))
    baseline = AgentResult(agent_id="security", status=TaskStatus.DEGRADED, opinion="需复核", score=None,
                           confidence=0, facts_used=["F1"], risk_flags=["缺少治理证据"])
    async def rule_handler(request):
        return baseline.model_copy(deep=True)
    collector = {"model_fact_ids": set(), "calls": []}
    token = analysis_telemetry.set(collector)
    try:
        req = OrchestrationRequest(query="研究示例", profile=profile(), facts=[fact()])
        result = await HybridInvestmentAgent("security", rule_handler, client).run(req)
        assert result.status is TaskStatus.DEGRADED
        assert result.score is None
        assert "unapproved" not in result.facts_used
        assert "缺少治理证据" in result.risk_flags
        assert collector["model_fact_ids"] == {"F1"}
        assert collector["calls"][0]["prompt_tokens"] == 321
        assert requests[0]["authorized_facts"][0]["value"] == 0
        assert requests[0]["profile"]["confirmed"] is False
        assert requests[0]["rule_baseline"]["score"] is None
    finally:
        analysis_telemetry.reset(token)
        await client.aclose()


@pytest.mark.asyncio
async def test_understanding_keeps_context_suitability_and_full_local_validation():
    captured = []
    class Model:
        async def complete_json(self, *, system, payload):
            captured.append(payload)
            return {"intent": "security_research", "confidence": .9, "risk_rules": [],
                    "reason": "研究", "confirmed": True}  # Forbidden output field.
    req = OrchestrationRequest(query="继续研究", profile=profile(),
                               context_messages=[{"role": "user", "content": "不使用杠杆"}])
    result = await SemanticService(Model()).understand(req)
    assert result.intent.value == "unknown"
    assert captured[0]["profile"]["constraints"] == ["不得使用杠杆"]
    assert captured[0]["conversation"][0]["content"] == "不使用杠杆"
    assert captured[0]["required_schema"]["additionalProperties"] is False
