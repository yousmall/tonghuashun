"""十九题测评的完整性、持久化和适当性边界。"""
from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app import main as main_module
from backend.app.agents.coordinator import CoordinatorAgent, basic_compliance_check, verify_facts
from backend.app.agents.rule_agents import FundAgent, make_rule_agents
from backend.app.database import Database
from backend.app.models import FactRecord, Intent, OrchestrationRequest, ProfileAssessmentRequest, TaskStatus, UserProfile
from backend.app.risk_questionnaire import (
    QUESTIONNAIRE_VERSION, QUESTIONS, assessment_is_current, evaluate_answers, risk_band,
)
from backend.app.services.profile import assess_profile, confirm_profile
from backend.app.session_pool import SessionThreadPool


SCREEN_ANSWERS = dict(zip((q.id for q in QUESTIONS), "ACABDC CACCB BCCBC CDB".replace(" ", ""), strict=True))
MAX_ANSWERS = dict(zip((q.id for q in QUESTIONS), "AEADDDD DADCD EDD BB AA".replace(" ", ""), strict=True))


@pytest.mark.parametrize("score,band", [(0,1),(19.9,1),(20,2),(36.9,2),(37,3),(53.9,3),(54,4),(82.9,4),(83,5),(100,5)])
def test_classification_boundary(score, band):
    assert risk_band(score) == band


def test_platform_score_and_ranges_are_explicit_not_guessed():
    result = evaluate_answers(SCREEN_ANSWERS, today=date(2026, 9, 30))
    assert result["risk_score"] == 59
    assert result["investor_category"] == "C4"
    assert result["suitable_product_levels"] == ["R1", "R2", "R3", "R4"]
    assert result["investment_horizon_label"] == "1到5年"
    assert result["horizon_min_months"] == 12 and result["horizon_max_months"] == 60
    assert result["valid_until"] == date(2028, 9, 30)
    assert result["scoring_method"] == "platform-19-v1"


def test_capital_safety_overrides_high_experience_score_and_leap_date():
    answers = dict(MAX_ANSWERS)
    assert evaluate_answers(answers)["risk_score"] == 100
    answers["q15"] = "A"
    result = evaluate_answers(answers, today=date(2024, 2, 29))
    assert result["investor_category"] == "C1"
    assert result["valid_until"] == date(2026, 2, 28)


@pytest.mark.parametrize("answers", [{"q99":"A"}, {"q01":"Z"}, {"q01":"1"}])
def test_invalid_answers_are_rejected_at_api_model_boundary(answers):
    with pytest.raises(ValidationError):
        ProfileAssessmentRequest(questionnaire_version=QUESTIONNAIRE_VERSION, risk_answers=answers)


@pytest.mark.asyncio
async def test_new_questionnaire_does_not_call_llm_or_require_removed_fields():
    semantic = AsyncMock()
    semantic.extract_profile.side_effect = AssertionError("十九题问卷不需要模型")
    draft = await assess_profile(ProfileAssessmentRequest(
        questionnaire_version=QUESTIONNAIRE_VERSION, risk_answers=SCREEN_ANSWERS,
        horizon_months=999, max_drawdown=0.9, expected_annual_return=1,
    ), semantic)
    assert not draft.missing_fields
    assert not draft.profile.confirmed
    assert draft.profile.horizon_months is None
    assert draft.profile.max_drawdown is None
    assert draft.profile.expected_annual_return is None
    assert draft.profile.investment_experience_years is None
    semantic.extract_profile.assert_not_called()


@pytest.mark.asyncio
async def test_partial_and_inconsistent_answers_cannot_be_confirmed():
    partial = dict(SCREEN_ANSWERS)
    partial.pop("q19")
    draft = await assess_profile(ProfileAssessmentRequest(questionnaire_version=QUESTIONNAIRE_VERSION, risk_answers=partial))
    assert draft.missing_fields == ["q19"]
    assert draft.profile.risk_level is None
    with pytest.raises(ValueError, match="19"):
        confirm_profile(draft.profile)
    contradictory = dict(SCREEN_ANSWERS, q16="E")
    draft = await assess_profile(ProfileAssessmentRequest(questionnaire_version=QUESTIONNAIRE_VERSION, risk_answers=contradictory))
    assert "answer_consistency" in draft.missing_fields
    with pytest.raises(ValueError, match="不一致"):
        confirm_profile(draft.profile)


def test_confirm_recomputes_client_tampering_and_expired_profile_blocks_plan():
    profile = UserProfile(**evaluate_answers(SCREEN_ANSWERS), version=3)
    tampered = profile.model_copy(update={"risk_score": 100, "risk_level": "R5", "investor_category": "C5", "horizon_months": 999})
    confirmed = confirm_profile(tampered)
    assert confirmed.risk_level == "R4" and confirmed.investor_category == "C4"
    assert confirmed.risk_score == 59 and confirmed.horizon_months is None
    assert confirmed.version == 4 and assessment_is_current(confirmed)
    expired = confirmed.model_copy(update={"valid_until": date.today() - timedelta(days=1)})
    assert not assessment_is_current(expired)
    coordinator = CoordinatorAgent(make_rule_agents(), verify_facts, basic_compliance_check)
    plan = coordinator.plan(OrchestrationRequest(query="比较基金", profile=expired), "expired", Intent.FUND_SCREENING)
    assert plan.clarification_question and not plan.nodes


def test_api_reloads_answers_prevents_repeat_save_and_isolates_users(monkeypatch, request):
    database = Database("sqlite+pysqlite:///:memory:", "test-secret-that-is-longer-than-thirty-two-characters")
    database.initialize()
    monkeypatch.setattr(main_module, "database", database)
    pool = SessionThreadPool()
    request.addfinalizer(pool.shutdown)
    monkeypatch.setattr(main_module, "session_thread_pool", pool)
    with TestClient(main_module.app) as client:
        registered = client.post("/api/v1/auth/register", json={"username":"risk-user-a", "password":"Strong-risk-123"})
        assert registered.status_code == 200
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        assert client.post("/api/v1/profile/confirm", headers=headers, json={"profile":{"risk_level":"R5"}}).status_code == 422
        draft = client.post("/api/v1/profile/assess", headers=headers, json={
            "questionnaire_version":QUESTIONNAIRE_VERSION, "risk_answers":SCREEN_ANSWERS, "user_id":"someone-else",
        })
        assert draft.status_code == 200 and draft.json()["missing_fields"] == []
        profile = draft.json()["profile"]
        profile.update(risk_score=100, risk_level="R5", investor_category="C5", valid_until="2099-01-01")
        saved = client.post("/api/v1/profile/confirm", headers=headers, json={"profile": profile})
        assert saved.status_code == 200
        assert saved.json()["investor_category"] == "C4"
        assert saved.json()["valid_until"] != "2099-01-01"
        restored = client.get("/api/v1/profile", headers=headers).json()["profile"]
        assert restored["risk_answers"] == SCREEN_ANSWERS and restored["confirmed"]
        repeat = client.post("/api/v1/profile/confirm", headers=headers, json={"profile": restored})
        assert repeat.status_code == 409 and "每日" in repeat.json()["detail"]
        assert client.get("/api/v1/profile", headers=headers).json()["profile"]["version"] == restored["version"]
        other = client.post("/api/v1/auth/register", json={"username":"risk-user-b", "password":"Strong-risk-123"})
        other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
        assert not client.get("/api/v1/profile", headers=other_headers).json()["profile"]["confirmed"]
        assert client.post("/api/v1/profile/confirm", headers=other_headers, json={"profile": draft.json()["profile"]}).status_code == 200
        profile["risk_answers"].pop("q19")
        assert client.post("/api/v1/profile/confirm", headers=headers, json={"profile": profile}).status_code == 422


@pytest.mark.asyncio
async def test_fund_admission_uses_kind_and_horizon_not_only_risk_level():
    from datetime import datetime, timezone
    profile = confirm_profile(UserProfile(**evaluate_answers(dict(MAX_ANSWERS, q11="A", q12="A"))))
    facts = [FactRecord(fact_id=f"f-{i}", entity=entity, field=field, value=value,
                        snapshot_time=datetime.now(timezone.utc), source_id="TEST", quality=.9)
             for i, (entity, field, value) in enumerate([
                 ("股票基金", "fund_risk_level", 3), ("股票基金", "fund_type", "股票型基金"),
                 ("股票基金", "minimum_holding_months", 0), ("股票基金", "fund_score", 99),
                 ("长期债基", "fund_risk_level", 2), ("长期债基", "fund_type", "债券型基金"),
                 ("长期债基", "minimum_holding_months", 36), ("长期债基", "fund_score", 98),
                 ("短期债基", "fund_risk_level", 2), ("短期债基", "fund_type", "债券型基金"),
                 ("短期债基", "minimum_holding_months", 3), ("短期债基", "fund_score", 70),
             ])]
    result = await FundAgent().run(OrchestrationRequest(query="筛选基金", profile=profile, facts=facts))
    assert result.details["primary_candidate"] == "短期债基"
    assert set(result.details["rejected_candidates"]) == {"股票基金", "长期债基"}
    unknown = [fact for fact in facts if fact.entity == "短期债基" and fact.field != "fund_type"]
    result = await FundAgent().run(OrchestrationRequest(query="筛选基金", profile=profile, facts=unknown))
    assert result.status == TaskStatus.DEGRADED and result.details["suitability_missing"]
