"""自动规划的数值依据、区间、确认边界、账号隔离及界面交互。"""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from backend.app import main as main_module
from backend.app.database import Database
from backend.app.models import UserProfile
from backend.app.risk_questionnaire import QUESTIONNAIRE_VERSION, QUESTIONS, evaluate_answers
from backend.app.services.automatic_planning import build_automatic_planning, extract_planning_inputs
from backend.app.services.profile import confirm_profile
from backend.app.session_pool import SessionThreadPool


ANSWERS = dict(zip((q.id for q in QUESTIONS), "ACABDCCACCBBCCBCCDBCADAECDC", strict=True))
DESCRIPTION = "我偏好稳健，可投资资金20万元，计划投资3年，2年后买房，最多可接受回撤8%，期望年化收益8%。"


def test_literal_goals_earliest_deadline_and_reproducible_compounding():
    result = build_automatic_planning(narrative=DESCRIPTION, today=date(2026, 1, 31))
    assert result["risk_preference"]["label"] == "稳健倾向"
    assert result["risk_preference"]["drawdown_limit"] == .08
    assert result["risk_preference"]["capacity_level"] is None
    assert result["horizon_plan"]["exact_months"] == 24
    assert result["horizon_plan"]["milestones"][-1]["date"] == "2028-01-31"
    assert result["return_expectation"]["scenario"]["ending_amount"] == 233280
    assert result["return_expectation"]["scenario"]["profit"] == 33280
    assert result["status"] == "needs_information"  # 文字不能代答正式测评。
    assert any("更早" in w for w in result["warnings"])
    assert all(item["text"] in DESCRIPTION for item in result["evidence"])


@pytest.mark.parametrize("text,minimum,maximum", [
    ("计划投资3至5年",36,60), ("计划持有3年到5年",36,60),
    ("计划投资3个月到1年",3,12), ("至少投资五年",60,None),
    ("未来3年内不用钱",36,None), ("一年以内需要买房",0,12),
])
def test_ranges_and_lower_bounds_never_become_an_exact_date(text, minimum, maximum):
    result = build_automatic_planning(narrative=text)
    horizon = result["horizon_plan"]
    assert horizon["inputs"][0]["min"] == minimum
    assert horizon["inputs"][0]["max"] == maximum
    assert horizon["exact_months"] is None
    assert all(item["date"] is None for item in horizon["milestones"][1:])
    assert result["return_expectation"]["scenario"] is None


def test_chinese_half_year_and_month_end_are_calendar_safe():
    half = build_automatic_planning(narrative="计划投资三年半")
    assert half["horizon_plan"]["exact_months"] == 42
    short = build_automatic_planning(narrative="计划持有1个月", today=date(2028, 1, 31))
    assert short["horizon_plan"]["milestones"][-1]["date"] == "2028-02-29"
    assert short["horizon_plan"]["milestones"][2]["when"] == "现在起安排用款"


def test_historical_and_negated_statements_are_not_new_targets():
    inputs, _, _ = extract_planning_inputs("我有3年投资经验，过去最大回撤20%，不期望年化收益20%，不是激进型，不打算投资5年。")
    assert not inputs
    inputs, _, _ = extract_planning_inputs("不是不能接受亏损，过去能承受回撤20%，我有20万元债务。")
    assert not inputs


@pytest.mark.parametrize("text,low,high", [
    ("期望年化收益-5%",-.05,-.05), ("期望年化收益至少8%",.08,None),
    ("期望年化收益8%以上",.08,None), ("期望年化收益不超过5%",None,.05),
])
def test_return_signs_and_one_sided_goals_remain_literal(text, low, high):
    result = build_automatic_planning(narrative=text)
    assert result["return_expectation"]["min_annual"] == low
    assert result["return_expectation"]["max_annual"] == high


def test_no_fixed_return_does_not_invent_capital_guarantee_and_amount_conflicts_do_not_project():
    result = build_automatic_planning(narrative="稳健，无固定收益目标，计划投资3年，能承受回撤8%。")
    assert result["return_expectation"]["label"] == "无固定收益目标"
    assert not result["return_expectation"]["safety_first"]
    assert not any("期望年化收益" in item for item in result["missing_information"])
    result = build_automatic_planning(narrative="本金为10万元，投资本金20万元，计划投资3年，期望年化收益8%。")
    assert "capital" not in result["inputs"]
    assert result["return_expectation"]["scenario"] is None
    assert any("不同投资本金" in item for item in result["warnings"])


def test_derived_display_planning_is_not_duplicated_into_specialist_model_inputs():
    from backend.app.model_payload import profile_for_model
    profile = confirm_profile(UserProfile(**evaluate_answers(ANSWERS)))
    payload = profile_for_model(profile)
    assert "automatic_planning" not in payload
    assert payload["financial_plan"] == profile.financial_plan
    assert payload["risk_level"] == profile.risk_level


def test_conflicting_targets_are_exposed_instead_of_falling_back_to_old_answers():
    saved = confirm_profile(UserProfile(**evaluate_answers(ANSWERS))).model_dump(mode="json")
    result = build_automatic_planning(saved, narrative="期望年化收益8%，目标年化收益20%，不能接受亏损，也可接受回撤10%。")
    assert result["return_expectation"]["min_annual"] is None
    assert result["return_expectation"]["max_annual"] is None
    assert result["risk_preference"]["drawdown_limit"] is None
    assert len(result["missing_information"]) >= 2
    assert any("不同收益目标" in w for w in result["warnings"])
    assert any("回撤边界存在矛盾" in w for w in result["warnings"])


def test_target_amount_analysis_and_mismatch_are_math_not_forecasts():
    result = build_automatic_planning(narrative="可投资资金10万元，目标金额20万元，计划投资1年，期望年化收益8%，可接受回撤5%。")
    returns = result["return_expectation"]
    assert returns["required_annual_for_goal"] == pytest.approx(1)
    assert returns["scenario"]["ending_amount"] == 108000
    assert "不是收益预测" in returns["scenario"]["basis"]
    assert any("所需的年化收益高于" in w for w in result["warnings"])
    high = build_automatic_planning(narrative="保守，最多可接受回撤5%，期望年化收益20%，计划投资6个月")
    assert any("存在张力" in w for w in high["warnings"])


def test_questionnaire_automatically_plans_and_confirmation_recomputes_tampering():
    values = evaluate_answers(dict(ANSWERS, q25="A", q27="C"))
    plan = values["automatic_planning"]
    assert plan["return_expectation"]["min_annual"] == .05
    assert plan["return_expectation"]["max_annual"] == .10
    assert plan["horizon_plan"]["upper_bound_months"] == 12
    assert plan["horizon_plan"]["exact_months"] is None
    assert not plan["risk_preference"]["confirmed_assessment"]
    profile = UserProfile(**values).model_copy(update={"automatic_planning":{"risk_preference":{"capacity_level":"R5"}}})
    confirmed = confirm_profile(profile)
    assert confirmed.automatic_planning["risk_preference"]["capacity_level"] == confirmed.risk_level
    assert confirmed.automatic_planning["risk_preference"]["confirmed_assessment"]
    assert confirmed.horizon_months is None and confirmed.expected_annual_return is None


def test_expired_assessment_is_not_renewed_by_preview_and_zero_loss_is_preserved():
    profile = confirm_profile(UserProfile(**evaluate_answers(dict(ANSWERS, q26="A", q27="A")))).model_dump(mode="json")
    profile["valid_until"] = (date.today()-timedelta(days=1)).isoformat()
    result = build_automatic_planning(profile)
    assert result["risk_preference"]["drawdown_limit"] == 0
    assert result["risk_preference"]["label"] == "保守倾向"
    assert not result["risk_preference"]["confirmed_assessment"]
    assert result["return_expectation"]["safety_first"]
    assert result["return_expectation"]["scenario"] is None
    assert any("过期" in item for item in result["missing_information"])


def test_text_cannot_extend_earlier_questionnaire_funding_deadline():
    values = evaluate_answers(dict(ANSWERS, q25="A"))
    result = build_automatic_planning(values, narrative="计划投资10年，期望年化收益8%")
    assert result["horizon_plan"]["upper_bound_months"] == 12
    assert result["horizon_plan"]["exact_months"] is None


def test_declared_preference_and_questionnaire_capacity_are_distinct():
    values = evaluate_answers(dict(ANSWERS, q14="A"))
    risk = values["automatic_planning"]["risk_preference"]
    assert risk["label"] == "保守倾向"
    assert risk["capacity_level"] == values["risk_level"] == "R3"


def test_authenticated_api_isolated_preview_validation_and_persistence(monkeypatch, request):
    database = Database("sqlite+pysqlite:///:memory:", "test-secret-that-is-longer-than-thirty-two-characters")
    database.initialize()
    monkeypatch.setattr(main_module, "database", database)
    pool = SessionThreadPool()
    request.addfinalizer(pool.shutdown)
    monkeypatch.setattr(main_module, "session_thread_pool", pool)
    with TestClient(main_module.app) as client:
        path = "/api/v1/profile/auto-plan"
        assert client.post(path, json={"narrative":DESCRIPTION}).status_code == 401
        def headers(name):
            response = client.post("/api/v1/auth/register", json={"username":name,"password":"Strong-plan-123"})
            assert response.status_code == 200
            return {"Authorization":f"Bearer {response.json()['access_token']}"}
        first, second = headers("planning-a"), headers("planning-b")
        profile = UserProfile(**evaluate_answers(ANSWERS)).model_dump(mode="json")
        saved = client.post("/api/v1/profile/confirm", headers=first, json={"profile":profile}).json()
        plan = client.post(path, headers=first, json={}).json()
        assert plan["risk_preference"]["confirmed_assessment"]
        other = client.post(path, headers=second, json={}).json()
        assert other["risk_preference"]["capacity_level"] is None
        assert client.post(path, headers=first, json={"user_id":"other", "risk_level":"R5"}).status_code == 422
        assert client.post(path, headers=first, json={"risk_answers":{"q99":"A"}}).status_code == 422
        assert client.post(path, headers=first, json={"narrative":"x"*2001}).status_code == 422
        assert client.post(path, headers=first, json={"risk_answers":{},"questionnaire_version":"old"}).status_code == 422
        draft = client.post(path, headers=first, json={"narrative":DESCRIPTION,"risk_answers":{"q26":"B"},"questionnaire_version":QUESTIONNAIRE_VERSION})
        assert draft.status_code == 200
        assert draft.json()["risk_preference"]["capacity_level"] is None
        assert not draft.json()["risk_preference"]["confirmed_assessment"]
        assert client.get("/api/v1/profile", headers=first).json()["profile"] == saved


def test_ui_generates_three_results_and_invalidates_preview_on_answer_edit():
    script = '''
import streamlit as st
from frontend.automatic_planning import render_automatic_planning
from backend.app.services.automatic_planning import build_automatic_planning
if 'profile' not in st.session_state:
    st.session_state.profile = {}
    st.session_state.risk_assessment_editing = True
    st.session_state.risk_assessment_answers = {}
def api(base, method, path, payload):
    st.session_state.sent = payload
    return build_automatic_planning(narrative=payload['narrative'], risk_answers=payload.get('risk_answers'))
render_automatic_planning('http://localhost', api_request=api)
'''
    app = AppTest.from_string(script).run(timeout=15)
    app.text_area[0].set_value(DESCRIPTION)
    next(button for button in app.button if button.label == "自动识别并规划").click().run(timeout=15)
    assert not app.exception
    assert {"风险偏好识别","收益预期分析","投资期限规划"} <= {item.value for item in app.subheader}
    assert len(app.table) == 2  # 时间线及原文依据均实际渲染。
    assert app.session_state["sent"]["risk_answers"] == {}
    assert app.session_state["automatic_plan_preview"]["horizon_plan"]["exact_months"] == 24
    app.session_state["risk_assessment_answers"] = {"q26":"A"}
    app.run(timeout=15)
    assert not app.exception and len(app.table) == 0
    assert "automatic_plan_preview" not in app.session_state


def test_ui_displays_questionnaire_planning_without_an_extra_click():
    profile = confirm_profile(UserProfile(**evaluate_answers(ANSWERS))).model_dump(mode="json")
    script = f'''
import streamlit as st
from frontend.automatic_planning import render_automatic_planning
st.session_state.profile = {profile!r}
render_automatic_planning('http://localhost', api_request=lambda *args: None)
'''
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    assert {"风险偏好识别","收益预期分析","投资期限规划"} <= {item.value for item in app.subheader}
    assert any("已确认且有效" in item.value for item in app.caption)
