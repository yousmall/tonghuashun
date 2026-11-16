from datetime import date,timedelta
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest
from backend.app.models import UserProfile
from backend.app.services.investor_workbench import (
    Trade,ReviewRequest,WorkspaceSave,PaperOrder,review_trades,fit_candidate,cost_scenario,paper_order,history_suggestions)
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

def test_paper_cash_sell_restrictions_and_replay_are_server_calculated():
    def order(**patch): return PaperOrder(expected_version=1,**{**trade("buy",10),**patch})
    first=paper_order({},order())
    assert first["cash"]=="98995.00"
    with pytest.raises(ValueError): paper_order(first,order(side="sell"))
    with pytest.raises(ValueError): paper_order(first,order(quantity=1))
    sold=paper_order(first,order(side="sell",price=11,date="2026-11-02"))
    assert sold["cash"]=="100090.00" and sold["review"]["matched_net"]==90
    assert first["cash"]=="98995.00" and len(first["trades"])==1
    with pytest.raises(ValueError): paper_order(sold,order(side="sell",date="2026-11-03"))

def test_history_suggestions_are_not_automatically_confirmed():
    a={"confirmed":True,"reported_median_holding_days":16,"report_sections":[{"title":"题材偏好","rows":[{"label":"半导体"},{"label":"其他"}]}]}
    s=history_suggestions(a)
    assert s["industries"]==["半导体"] and s["style"]=="短期观察"
    assert history_suggestions({**a,"confirmed":False})["industries"]==[]

def test_real_api_account_isolation_version_conflicts_and_risk_confirmation_protection(monkeypatch,request):
    from backend.app import main
    from backend.app.database import Database
    from backend.app.session_pool import SessionThreadPool
    from backend.app.risk_questionnaire import QUESTIONS,evaluate_answers
    db=Database("sqlite+pysqlite:///:memory:","test-workbench-secret-longer-than-thirty-two-characters"); db.initialize()
    pool=SessionThreadPool(); request.addfinalizer(pool.shutdown)
    monkeypatch.setattr(main,"database",db); monkeypatch.setattr(main,"session_thread_pool",pool)
    with TestClient(main.app) as c:
        assert c.get("/api/v1/investor/workspace").status_code==401
        def account(name):
            r=c.post("/api/v1/auth/register",json={"username":name,"password":"Test-workbench-123"}).json()
            return {"Authorization":"Bearer "+r["access_token"]}
        a,b=account("workbench-a"),account("workbench-b")
        payload={"expected_version":1,"preferences":{"industries":["半导体"]},"cash":50000,"holdings":[],"holdings_complete":True,"holdings_as_of":local_today().isoformat()}
        saved=c.post("/api/v1/investor/workspace",headers=a,json=payload)
        assert saved.status_code==200 and saved.json()["version"]==2
        assert c.post("/api/v1/investor/workspace",headers=a,json=payload).status_code==409
        assert c.get("/api/v1/investor/workspace",headers=b).json()["workspace"]=={}
        answers=dict(zip((q.id for q in QUESTIONS),"ACABDCCACCBBCCBCCDBCADAECDC",strict=True))
        values={**evaluate_answers(answers),"version":2,"investment_workspace":{"paper":{"cash":9999999}}}
        confirmed=c.post("/api/v1/profile/confirm",headers=a,json={"profile":UserProfile(**values).model_dump(mode="json")})
        assert confirmed.status_code==200 and "paper" not in confirmed.json()["investment_workspace"]
        assert confirmed.json()["investment_workspace"]["preferences"]["industries"]==["半导体"]
        result=c.post("/api/v1/investor/review",headers=a,json={"trades":[trade("buy",10),trade("sell",11)]})
        assert result.status_code==200 and result.json()["matched_net"]==90

def test_new_workbench_ui_and_tab_navigation():
    app=AppTest.from_string('''
import streamlit as st
from frontend.investor_workbench import render_workbench
def api(*args):
    return {'profile':{'version':1},'workspace':{},'history_suggestions':{'industries':[],'style':'未设定','notice':'待确认'}}
render_workbench('http://localhost',api_request=api,go_to=lambda page:None)
''').run(timeout=15)
    assert not app.exception and len(app.radio[0].options)==5
    assert app.title[0].value=="投资工作台"
    for label in app.radio[0].options[1:]:
        app.radio[0].set_value(label).run(timeout=15)
        assert not app.exception
