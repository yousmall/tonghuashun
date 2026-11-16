"""个性化方案的资金约束、确认边界、账户隔离及真实组件交互。"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from backend.app import main as main_module
from backend.app.database import Database
from backend.app.models import UserProfile
from backend.app.risk_questionnaire import QUESTIONS, evaluate_answers
from backend.app.services.personalized_investment import build_personalized_investment
from backend.app.session_pool import SessionThreadPool

DAY = date(2026, 11, 16)
ANSWERS = dict(zip((q.id for q in QUESTIONS), "ACABDCCACCBBCCBCCDBCADAECDC", strict=True))


def profile(*, today=DAY, **answers):
    values = evaluate_answers({**ANSWERS, "q03": "B", "q20": "B", "q21": "A", "q22": "D", "q23": "A", "q25": "C", **answers}, today=today)
    return UserProfile(**values, confirmed=True, version=3).model_dump(mode="json")


def test_rebuilds_from_answers_and_does_not_mutate_or_extend_saved_assessment():
    original = profile()
    tampered = dict(original, risk_level="R5", financial_plan={"equity_cap": 1, "cash_floor": 0},
        max_drawdown=1, allocation_guidance=[{"reference_weight": 1}], stress_scenarios=[])
    result = build_personalized_investment(tampered, today=DAY + timedelta(days=1))
    expected = build_personalized_investment(original, today=DAY)
    assert result["status"] == "ready"
    assert result["profile_summary"]["risk_level"] == expected["profile_summary"]["risk_level"]
    assert result["allocation"] == expected["allocation"]
    assert result["profile_summary"]["valid_until"] == original["valid_until"]
    assert tampered["risk_level"] == "R5" and tampered["financial_plan"]["equity_cap"] == 1
    assert result["profile_version"] == 3


@pytest.mark.parametrize("patch", [
    {"confirmed": False}, {"questionnaire_version": "old"}, {"questionnaire_version": None},
    {"risk_answers": {"q26": "A"}}, {"risk_answers": dict(ANSWERS, q99="A")},
    {"valid_until": "2026-11-15"}, {"assessed_on": "2026-11-17"},
])
def test_invalid_assessment_never_produces_allocations_or_amounts(patch):
    result = build_personalized_investment(dict(profile(), **patch), investment_amount=100000, today=DAY)
    assert result["status"] == "needs_assessment"
    assert result["allocation"] == result["recommendations"] == result["stress_scenarios"] == []
    assert result["missing_information"]


@pytest.mark.parametrize("overrides", [{"q25": "A"}, {"q23": "E"}, {"q26": "A"}, {"q21": "E"}])
def test_short_horizon_heavy_debt_and_zero_loss_never_allocate_equity(overrides):
    result = build_personalized_investment(profile(**overrides), investment_amount=100000, today=DAY)
    assert result["status"] == "ready"
    assert result["allocation"][0]["max_weight"] == 0
    assert result["allocation"][0]["reference_amount"] == 0
    if overrides.get("q26") == "A" or overrides.get("q21") == "E":
        assert result["allocation"][2]["reference_amount"] == 100000


def test_same_risk_grade_changes_allocation_for_liquidity_and_never_for_return_goal():
    long = build_personalized_investment(profile(), today=DAY)
    short = build_personalized_investment(profile(q25="A"), today=DAY)
    aggressive_goal = build_personalized_investment(profile(q27="E"), today=DAY)
    assert long["profile_summary"]["risk_level"] == short["profile_summary"]["risk_level"]
    assert short["allocation"][0]["reference_weight"] < long["allocation"][0]["reference_weight"]
    assert aggressive_goal["allocation"] == long["allocation"]
    assert any("收益目标" in message for message in aggressive_goal["warnings"])


@pytest.mark.parametrize("amount,capital", [("0.01", "A"), ("100001.01", "B"), ("999999.99", "D"), ("1000000000000", "E")])
def test_reference_amounts_sum_exactly_to_capital_and_loss_is_a_labelled_scenario(amount, capital):
    result = build_personalized_investment(profile(q20=capital), investment_amount=amount, today=DAY)
    assert result["status"] == "ready"
    assert sum(Decimal(str(row["reference_amount"])) for row in result["allocation"]) == Decimal(amount)
    assert sum(row["reference_weight"] for row in result["allocation"]) == pytest.approx(1, abs=2e-6)
    for row in result["allocation"]:
        assert row["min_weight"] <= row["reference_weight"] <= row["max_weight"]
        assert row["reference_amount"] >= 0
    for scenario in result["stress_scenarios"]:
        expected = -sum(row["reference_weight"] * scenario["assumptions"][row["asset_class"]] for row in result["allocation"])
        assert scenario["estimated_loss"] == pytest.approx(expected, abs=1e-6)
    assert "不是预测" in result["notice"] and "不是最大回撤保证" in result["notice"]


@pytest.mark.parametrize("amount,expected", [("49999.99", "needs_information"), ("50000", "ready"),
    ("199999.99", "ready"), ("200000", "needs_information")])
def test_exact_amount_respects_questionnaire_bucket_boundaries(amount, expected):
    result = build_personalized_investment(profile(q20="B"), investment_amount=amount, today=DAY)
    assert result["status"] == expected
    if expected != "ready":
        assert not result["allocation"]
        assert any("资金区间" in item for item in result["missing_information"])


def test_no_midpoint_capital_guesses_and_unknown_funding_date_requires_followup():
    result = build_personalized_investment(profile(), today=DAY)
    assert result["investment_amount"] is None
    assert all("reference_amount" not in row for row in result["allocation"])
    assert all(row["loss_amount"] is None for row in result["stress_scenarios"])
    unknown = build_personalized_investment(profile(q25="E"), today=DAY)
    assert unknown["status"] == "needs_information" and not unknown["allocation"]
    restricted = build_personalized_investment(dict(profile(), constraints=["不买任何基金"]), today=DAY)
    assert restricted["status"] == "needs_information" and not restricted["allocation"]


def test_uses_confirmed_trade_notes_without_treating_turnover_as_holdings():
    saved = dict(profile(), trading_analysis={"confirmed": True, "behavioral_notes": ["年度交易额最高股票占比70%。"]})
    result = build_personalized_investment(saved, today=DAY)
    trade = next(item for item in result["recommendations"] if "历史交易" in item["title"])
    assert "70%" in trade["basis"] and "不能替代当前持仓权重" in trade["action"]
    saved["trading_analysis"]["confirmed"] = False
    other = build_personalized_investment(saved, today=DAY)
    assert all("历史交易" not in item["title"] for item in other["recommendations"])
    assert result["allocation"] == other["allocation"]


def test_authenticated_api_validates_inputs_isolates_accounts_and_never_saves_preview(monkeypatch, request):
    from backend.app.risk_questionnaire import local_today
    database = Database("sqlite+pysqlite:///:memory:", "test-secret-that-is-longer-than-thirty-two-characters")
    database.initialize()
    monkeypatch.setattr(main_module, "database", database)
    pool = SessionThreadPool()
    request.addfinalizer(pool.shutdown)
    monkeypatch.setattr(main_module, "session_thread_pool", pool)
    with TestClient(main_module.app) as client:
        path = "/api/v1/profile/investment-plan"
        assert client.post(path, json={}).status_code == 401
        def headers(name):
            response = client.post("/api/v1/auth/register", json={"username": name, "password": "Strong-plan-123"})
            assert response.status_code == 200
            return {"Authorization": f"Bearer {response.json()['access_token']}"}
        first, second = headers("invest-plan-a"), headers("invest-plan-b")
        draft = dict(profile(today=local_today()), confirmed=False, version=1)
        response = client.post("/api/v1/profile/confirm", headers=first, json={"profile": draft})
        assert response.status_code == 200
        before = client.get("/api/v1/profile", headers=first).json()
        response = client.post(path, headers=first, json={"investment_amount": "100000.01"})
        assert response.status_code == 200 and response.json()["status"] == "ready"
        assert response.json()["profile_version"] == before["profile"]["version"]
        assert client.post(path, headers=second, json={}).json()["status"] == "needs_assessment"
        for extra in ({"user_id": "other"}, {"risk_level": "R5"}, {"profile": before["profile"]}, {"risk_answers": ANSWERS}):
            assert client.post(path, headers=first, json=extra).status_code == 422
        for amount in (0, -1, "NaN", "Infinity", "1000.001", "1000000000001"):
            assert client.post(path, headers=first, json={"investment_amount": amount}).status_code == 422
        assert client.get("/api/v1/profile", headers=first).json() == before


def ui_script(saved):
    return f'''
import streamlit as st
from backend.app.services.personalized_investment import build_personalized_investment
from frontend.personalized_investment import render_personalized_investment
if 'profile' not in st.session_state:
    st.session_state.profile = {saved!r}
def api(base, method, path, payload):
    st.session_state.sent = (method, path, payload)
    if st.session_state.get('fail'):
        return None
    result = build_personalized_investment(st.session_state.profile, investment_amount=payload.get('investment_amount'))
    if st.session_state.get('stale'):
        result['profile_version'] += 1
    return result
render_personalized_investment('http://localhost', api_request=api)
'''


def test_ui_renders_proportions_amounts_and_invalidates_preview_on_edit_or_profile_change():
    from backend.app.risk_questionnaire import local_today
    saved = profile(today=local_today())
    app = AppTest.from_string(ui_script(saved)).run(timeout=15)
    assert not app.exception and app.table
    assert "参考金额（元）" not in app.table[0].value.columns
    app.number_input[0].set_value(100000.01)
    next(b for b in app.button if b.label == "生成个性化方案").click().run(timeout=15)
    assert not app.exception
    assert app.session_state["sent"] == ("POST", "/profile/investment-plan", {"investment_amount": 100000.01})
    assert "参考金额（元）" in app.table[0].value.columns
    app.session_state["risk_assessment_editing"] = True
    app.run(timeout=15)
    assert not app.exception and not app.table and not app.number_input
    assert "automatic_plan_investment_result" not in app.session_state
    app.session_state["risk_assessment_editing"] = False
    app.session_state["profile"] = profile(today=local_today(), q25="A")
    app.run(timeout=15)
    assert not app.exception and app.number_input[0].value == 0
    assert "参考金额（元）" not in app.table[0].value.columns


def test_ui_does_not_show_old_amounts_after_api_failure_and_hides_unconfirmed_results():
    from backend.app.risk_questionnaire import local_today
    app = AppTest.from_string(ui_script(profile(today=local_today()))).run(timeout=15)
    app.number_input[0].set_value(100000)
    next(b for b in app.button if b.label == "生成个性化方案").click().run(timeout=15)
    assert not app.exception and app.table
    app.session_state["fail"] = True
    next(b for b in app.button if b.label == "生成个性化方案").click().run(timeout=15)
    assert not app.exception and not app.table and app.error
    app.run(timeout=15)
    assert not app.exception and not app.table
    app.session_state["profile"] = dict(app.session_state["profile"], confirmed=False)
    app.run(timeout=15)
    assert not app.exception and not app.table and not app.number_input


def test_ui_rejects_newer_server_profile_and_keeps_stale_allocations_hidden():
    from backend.app.risk_questionnaire import local_today
    app = AppTest.from_string(ui_script(profile(today=local_today()))).run(timeout=15)
    app.session_state["stale"] = True
    next(b for b in app.button if b.label == "生成个性化方案").click().run(timeout=15)
    assert not app.exception and not app.table and app.warning
    app.run(timeout=15)
    assert not app.exception and not app.table


def test_allocation_chart_contains_initial_finite_data_without_delayed_arrow():
    import json
    from backend.app.risk_questionnaire import local_today
    for overrides in ({}, {"q26": "A"}):
        app = AppTest.from_string(ui_script(profile(today=local_today(), **overrides))).run(timeout=15)
        assert not app.exception
        chart = app.get("vega_lite_chart")[0].proto
        assert not chart.datasets and not chart.data.data
        spec = json.loads(chart.spec)
        assert spec["height"] == 180
        rows = spec["layer"][0]["data"]["values"]
        assert len(rows) == 3
        assert all(0 <= row["示例占比（%）"] <= 100 for row in rows)
        assert sum(row["示例占比（%）"] for row in rows) == pytest.approx(100, abs=.0002)
