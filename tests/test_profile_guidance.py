"""财务画像、受约束配置和旧问卷升级的业务边界。"""
from itertools import product

import pytest
from streamlit.testing.v1 import AppTest

from backend.app.agents.coordinator import CoordinatorAgent
from backend.app.models import AgentResult, OrchestrationRequest, TaskStatus, UserProfile
from backend.app.risk_questionnaire import (
    BASE_QUESTIONS, LEGACY_QUESTIONNAIRE_VERSION, QUESTIONNAIRE_VERSION,
    QUESTIONS, assessment_is_current, evaluate_answers,
)
from backend.app.services.profile import confirm_profile


def answers(**patch):
    selected = dict(zip((q.id for q in QUESTIONS), "ACABDCCACCBBCCBCCDBCADAECDC", strict=True))
    return dict(selected, **patch)


def test_confirm_rebuilds_supplementary_fields_and_preserves_explicit_limits():
    expected = evaluate_answers(answers(q21="D", q26="B"))
    profile = UserProfile(**expected, single_security_limit=.1, industry_limit=.25, constraints=["不使用杠杆"])
    tampered = profile.model_copy(update={"max_drawdown": .9, "liquidity_need": "低",
        "financial_plan": {"cash_floor": 0}, "allocation_guidance": [{"asset_class": "权益类", "max_weight": 1}],
        "assessment_reasons": [], "scoring_breakdown": [], "financial_warnings": [], "stress_scenarios": []})
    confirmed = confirm_profile(tampered)
    assert confirmed.max_drawdown == .05 and confirmed.liquidity_need == "高"
    assert confirmed.financial_plan == expected["financial_plan"]
    assert confirmed.allocation_guidance == expected["allocation_guidance"]
    assert confirmed.assessment_reasons == expected["assessment_reasons"]
    assert confirmed.scoring_breakdown == expected["scoring_breakdown"]
    assert confirmed.financial_warnings == expected["financial_warnings"]
    assert confirmed.stress_scenarios == expected["stress_scenarios"]
    assert confirmed.single_security_limit == .1 and confirmed.industry_limit == .25
    assert confirmed.constraints == ["不使用杠杆"]


def test_same_risk_grade_has_different_allocation_for_earlier_goal():
    long = evaluate_answers(answers(q25="C"))
    short = evaluate_answers(answers(q25="A"))
    assert long["investor_category"] == short["investor_category"] == "C4"
    assert short["effective_horizon_max_months"] == 12
    assert short["allocation_guidance"][0]["max_weight"] == 0
    assert long["allocation_guidance"][0]["max_weight"] > 0
    assert short["liquidity_need"] == "高"


def test_intervals_are_preserved_without_invented_amounts_or_return_targets():
    result = evaluate_answers(answers(q20="E", q25="E", q26="E", q27="E"))
    profile = confirm_profile(UserProfile(**result))
    assert profile.financial_plan["investment_capital_min"] == 1_000_000
    assert profile.financial_plan["investment_capital_max"] is None
    assert profile.financial_plan["goal_horizon_max_months"] is None
    assert profile.max_drawdown is None
    assert profile.expected_annual_return is None and profile.horizon_months is None
    assert profile.financial_plan["expected_return_max"] is None
    assert len(profile.financial_plan["followups"]) == 2
    assert sum(row["points"] for row in profile.scoring_breakdown) == profile.risk_score
    assert sum(row["maximum"] for row in profile.scoring_breakdown) == 100


def test_debt_contradiction_blocks_confirmation_and_zero_drawdown_is_conservative():
    with pytest.raises(ValueError, match="第23题"):
        evaluate_answers(answers(q03="A", q23="B"))
    result = evaluate_answers(answers(q26="A"))
    assert result["risk_score"] == 59
    assert result["investor_category"] == "C1"
    assert result["allocation_guidance"][0]["max_weight"] == 0
    assert result["allocation_guidance"][2]["reference_weight"] == 1
    assert any("不能保证" in warning for warning in result["financial_warnings"])


def test_complete_financial_scenarios_have_feasible_ranges_and_normalized_examples():
    # 不同支出、储备、负债、期限和回撤边界的2500个组合。
    for spending, emergency, debt, horizon, tolerance in product("ABCDE", "ABCD", "ABCDE", "ABCDE", "ABCDE"):
        result = evaluate_answers(answers(q03="B", q21=spending, q22=emergency,
                                         q23=debt, q25=horizon, q26=tolerance))
        rows = result["allocation_guidance"]
        assert sum(row["reference_weight"] for row in rows) == pytest.approx(1, abs=2e-6)
        assert sum(row["min_weight"] for row in rows) <= 1 + 2e-6
        assert sum(row["max_weight"] for row in rows) >= 1 - 2e-6
        for row in rows:
            assert 0 <= row["min_weight"] <= row["reference_weight"] <= row["max_weight"] <= 1
        assert rows[2]["min_weight"] >= result["financial_plan"]["cash_floor"] - 1e-6
        if result["max_drawdown"] is not None:
            assert result["stress_scenarios"][0]["estimated_loss"] <= result["max_drawdown"] + 2e-6
        if spending == "E":
            assert rows[2]["reference_weight"] == 1


def test_allocation_retains_confirmed_profile_and_portfolio_evidence_gates():
    profile = confirm_profile(UserProfile(**evaluate_answers(answers(q25="A"))))
    request = OrchestrationRequest(query="组合诊断", profile=profile)
    result = AgentResult(agent_id="portfolio", status=TaskStatus.COMPLETED, opinion="集中度诊断", confidence=.9,
                         details={"largest_weight": .4}, facts_used=["F1"])
    allocation = CoordinatorAgent._allocation_summary([result], request)
    assert allocation[0]["max_weight"] == 0
    assert all(row["evidence"] == ["F1"] for row in allocation)
    assert not CoordinatorAgent._allocation_summary([result.model_copy(update={"facts_used": []})], request)
    assert not CoordinatorAgent._allocation_summary([result], request.model_copy(update={"profile": profile.model_copy(update={"confirmed": False})}))


def test_legacy_profile_prompts_only_missing_supplementary_questions_in_same_wizard():
    old_answers = {q.id: answers()[q.id] for q in BASE_QUESTIONS}
    legacy = {"questionnaire_version": LEGACY_QUESTIONNAIRE_VERSION, "risk_answers": old_answers, "confirmed": True}
    assert not assessment_is_current(legacy)
    script = f"""
import streamlit as st
from frontend.risk_assessment import render_risk_assessment
if 'profile' not in st.session_state:
    st.session_state.profile = {legacy!r}
render_risk_assessment('http://localhost', api_request=lambda *args, **kwargs: None)
"""
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    assert app.session_state["risk_assessment_index"] == 19
    assert app.session_state["risk_assessment_answers"] == old_answers
    assert len(app.radio) == 1 and app.radio[0].value is None
    assert not app.number_input and not app.text_input and not app.slider
    assert "20." in app.markdown[0].value
    app.radio[0].set_value("C").run(timeout=15)
    next(b for b in app.button if b.label == "下一题").click().run(timeout=15)
    assert app.radio[0].value is None
    next(b for b in app.button if b.label == "上一题").click().run(timeout=15)
    assert app.radio[0].value == "C"
    assert app.session_state["risk_assessment_answers"]["q01"] == old_answers["q01"]


@pytest.mark.asyncio
async def test_earliest_goal_horizon_rejects_locked_fund():
    from datetime import datetime, timezone
    from backend.app.agents.rule_agents import FundAgent
    from backend.app.models import FactRecord
    profile = confirm_profile(UserProfile(**evaluate_answers(answers(q25="A"))))
    facts = [FactRecord(fact_id=f"f-{i}", entity=entity, field=field, value=value,
                       snapshot_time=datetime.now(timezone.utc), source_id="TEST", quality=.9)
             for i, (entity, field, value) in enumerate([
                 ("锁定债基", "fund_risk_level", 2), ("锁定债基", "fund_type", "债券型"),
                 ("锁定债基", "minimum_holding_months", 24), ("锁定债基", "fund_score", 99),
                 ("短期债基", "fund_risk_level", 2), ("短期债基", "fund_type", "债券型"),
                 ("短期债基", "minimum_holding_months", 3), ("短期债基", "fund_score", 80),
             ])]
    result = await FundAgent().run(OrchestrationRequest(query="筛选基金", profile=profile, facts=facts))
    assert result.details["primary_candidate"] == "短期债基"
    assert result.details["rejected_candidates"] == ["锁定债基"]
