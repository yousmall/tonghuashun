from datetime import date,timedelta
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest
from backend.app.models import UserProfile
from backend.app.services.investment_support import (
    Trade,ReviewRequest,WorkspaceSave,review_trades,fit_candidate,cost_scenario,history_suggestions)
from backend.app.risk_questionnaire import local_today

def trade(side,price,quantity=100,fee=5,day="2026-11-01",code="600000"):
    return {"side":side,"price":price,"quantity":quantity,"fee":fee,"date":day,"code":code}

def test_fifo_partial_lots_fees_and_wins_are_net_not_gross():
    r=review_trades(ReviewRequest(trades=[trade("buy",10,200,10),trade("sell",10.05,100,5),trade("sell",11,100,5)]))
    assert r["closed"][0]["net"] == -5
    assert r["closed"][1]["net"] == 90
    assert r["matched_net"] == 85 and r["by_security"][0]["win_rate"]==.5
    assert r["known_fees"]==20

@pytest.mark.parametrize("rows",[
    [trade("buy",10,fee=None),trade("sell",11)],
    [trade("sell",11)],
    [trade("buy",10,100),trade("sell",11,200)]])
def test_missing_cost_or_opening_positions_never_claim_complete_profit(rows):
    r=review_trades(ReviewRequest(trades=rows)); assert r["matched_net"] is None and r["status"]=="partial"

def test_flow_adjusted_performance_is_distinct_from_trading_pnl():
    r=review_trades(ReviewRequest(nav=[{"date":"2026-01-01","value":1000},
        {"date":"2026-02-01","value":2100,"flow":1000},{"date":"2026-03-01","value":1890}]))
    assert r["performance"]["period_return"]==pytest.approx(-.01)
    assert r["performance"]["max_drawdown"]==pytest.approx(.1)
    assert r["matched_net"] is None
    with pytest.raises(ValueError): review_trades(ReviewRequest(nav=[{"date":"2026-01-01","value":1000,"flow":1}]))


def test_explicit_opening_positions_supply_real_cost_without_synthetic_trade():
    r=review_trades(ReviewRequest(trades=[trade("sell",11)],opening_positions=[{
        "code":"600000","quantity":100,"unit_cost":10,"acquired_on":"2026-10-01"}]))
    assert r["matched_net"]==95 and r["known_fees"]==5 and r["unmatched_sells"]==0

def workspace(**patch):
    return {"confirmed":True,"preferences":{"industries":["半导体"]},"cash":"40000","holdings_complete":True,
        "holdings_as_of":local_today().isoformat(),"holdings":[{"code":"600000","industry":"半导体","value":"10000"}],**patch}

def test_holdings_and_preference_change_actual_fit_without_changing_risk():
    p=UserProfile(risk_level="R3",investment_workspace=workspace())
    fit=fit_candidate(p,code="600001",industry="半导体")
    assert fit["bonus"]==5 and fit["max_additional_amount"]==5000
    assert p.risk_level=="R3"
    blocked=fit_candidate(p.model_copy(update={"investment_workspace":workspace(preferences={"avoid_industries":["半导体"]})}),code="600001",industry="半导体")
    assert not blocked["allowed"]
    same=fit_candidate(p,code="600000",industry="半导体")
    assert not same["allowed"] and same["max_additional_amount"]==0

def test_incomplete_or_stale_holdings_never_invent_buy_budget():
    p=UserProfile(investment_workspace=workspace(holdings_complete=False))
    assert fit_candidate(p,code="600001",industry="半导体")["max_additional_amount"] is None
    p.investment_workspace=workspace(holdings_as_of=(local_today()-timedelta(days=8)).isoformat())
    assert not fit_candidate(p,code="600001",industry="半导体")["allowed"]

def test_cost_budget_can_exclude_small_high_cost_trades():
    p=UserProfile(investment_workspace=workspace(preferences={"planned_trade_amount":"100","max_round_trip_cost_ratio":.02}))
    assert not fit_candidate(p,code="600001",industry="制造业")["allowed"]
    r=cost_scenario(100,{"minimum_commission":5,"commission_rate":0,"sell_tax_rate":0,"slippage_rate":0})
    assert r["round_trip_cost"]==10 and r["break_even_return"]==.1

def test_workspace_rejects_duplicates_nonfinite_and_conflicting_preferences():
    payload={"expected_version":1,"preferences":{},"holdings":[],"cash":0,"holdings_as_of":local_today()}
    with pytest.raises(ValueError): WorkspaceSave(**{**payload,"cash":"NaN"})
    with pytest.raises(ValueError): WorkspaceSave(**{**payload,"preferences":{"industries":["银行"],"avoid_industries":["银行"]}})
    h={"code":"600000","industry":"银行","value":1000}
    with pytest.raises(ValueError): WorkspaceSave(**{**payload,"holdings":[h,h]})

def test_history_suggestions_are_not_automatically_confirmed():
    a={"confirmed":True,"reported_median_holding_days":16,"report_sections":[{"title":"题材偏好","rows":[{"label":"半导体"},{"label":"其他"}]}]}
    s=history_suggestions(a)
    assert s["industries"]==["半导体"] and s["style"]=="短期观察"
    assert history_suggestions({**a,"confirmed":False})["industries"]==[]

def test_removed_workbench_endpoints_are_not_registered():
    from backend.app.main import app
    client = TestClient(app)
    assert client.get("/api/v1/investor/workspace").status_code == 404
    for path in ("workspace", "paper-order", "review", "journal"):
        assert client.post(f"/api/v1/investor/{path}", json={}).status_code == 404


def test_removed_workbench_has_no_navigation_entry_and_migrates_old_sessions():
    script = """
import streamlit as st
from frontend import streamlit_app as ui
ui.init_session()
st.session_state.navigation = '投资工作台'
ui.page_navigation()
"""
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    assert app.session_state['navigation'] == '主页'
    assert all('工作台' not in button.label for button in app.button)
