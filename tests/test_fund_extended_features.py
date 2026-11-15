"""重仓/行业、同类比较及统一复利风险计算的回归。"""
import json
import math
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from backend.app import main
from backend.app.data_provider.fund_details import parse_fund_home, parse_ths_portfolio
from backend.app.services.fund_analytics import parse_daily_returns, calculate_comparison
from backend.app.services.overview_cache import OverviewCache
from tests.test_fund_details_and_board_watchlist import board_script

NOW = datetime(2026, 11, 15, tzinfo=timezone.utc)


def home(code="510300"):
    positions = ''.join(f'<tr><td><a href="//quote.eastmoney.com/unify/r/0.{i:06d}">股票{i}</a></td><td>1.20%</td></tr>' for i in range(1,11))
    return f'''<title>测试基金({code})基金净值</title><table><tr><th>股票名称</th><th>持仓占比</th></tr>{positions}</table>
    <p>持仓截止日期: 2026-09-30</p><div id="IncreaseAmount"><h3 data-date="2026-11-13">收益</h3></div>
    <table><tr><th></th><th>近1月</th><th>近3年</th></tr>
    <tr><td>阶段涨幅</td><td>-5.89%</td><td>20.13%</td></tr>
    <tr><td>同类平均<span>划分说明</span></td><td>-6.67%</td><td>23.20%</td></tr>
    <tr><td>沪深300</td><td>-6.07%</td><td>12.21%</td></tr>
    <tr><td>同类排名<span>排名说明</span></td><td>2412 | 5613</td><td>1189 | 2334</td></tr></table>'''


THS = '''<title>测试基金</title><script>var mobileurl="https://fund.10jqka.com.cn/public/ifundout/dist/detail.html#/510300";</script>
<p>行业配置</p><p>数据更新至 2026-09-30</p><div id="industryList"><div class="box">
<p class="r-title">金融业</p><p><span class="percent">20.57%</span></p>
<p class="r-title">制造业</p><p><span class="percent">0.00%</span></p></div></div>
<p>重仓债</p><p>数据更新至 2025-03-31</p><ul><li>债券代码</li></ul>
<ul class="data"><li>1</li><li>012345</li><li>测试债券</li><li>100</li><li>200</li><li>0.01%</li></ul>'''


def test_holdings_keep_security_codes_period_and_peer_columns_do_not_shift():
    fields = {fact.field: fact for fact in parse_fund_home(home(), "510300", now=NOW)}
    assert len(fields["top_holdings"].value) == 10
    assert fields["top_holdings"].value[0]["证券代码"] == "000001"
    assert fields["top_holdings"].period == "2026-09-30"
    assert math.isclose(fields["top10_weight"].value, 12)
    assert fields["top10_weight"].derived_from == [fields["top_holdings"].fact_id]
    assert fields["reference_return_1m"].value == -5.89
    assert fields["reference_return_3y"].value == 20.13
    peer = fields["fund_peer_comparison"].value[0]
    assert peer["同类平均(%)"] == -6.67 and peer["同类名次"] == 2412 and peer["同类总数"] == 5613
    assert parse_fund_home(home("510500"), "510300", now=NOW) == []


def test_industry_and_bonds_keep_separate_periods_zero_weights_and_identity():
    fields = {fact.field: fact for fact in parse_ths_portfolio(THS, "510300", now=NOW)}
    assert fields["industry_allocation"].period == "2026-09-30"
    assert fields["industry_allocation"].value[1]["占净值比例(%)"] == 0
    assert fields["bond_holdings"].period == "2025-03-31"
    assert fields["bond_holdings"].value[0]["证券代码"] == "012345"
    assert fields["industry_allocation"].source_id == "PUBLIC_FUND_THS_V1"
    assert parse_ths_portfolio(THS, "510500", now=NOW) == []


def source(code="510300", *, dividend=False, varied=False):
    points, day, nav, index = [], date(2025,11,13), 1.0, 0
    while day <= date(2026,11,13):
        if day.weekday() < 5:
            growth = .2 if varied and index%2 == 0 else -.1 if varied else .1
            nav *= 1+growth/100
            event = "分红" if dividend and index == 100 else ""
            if event:
                nav *= .9
            points.append({"date": day.isoformat(), "nav": nav, "growth_pct": growth, "event": event})
            index += 1
        day += timedelta(days=1)
    return {"target": code, "code": code, "name": "测试"+code, "points": points,
            "source_url": "https://fund.eastmoney.com/"+code+".html"}


def test_reinvested_compounding_uses_growth_not_unadjusted_nav_and_constant_returns_have_no_sharpe():
    src = source(dividend=True)
    result = calculate_comparison([src], years=1, risk_free_rate=0)
    assert result["status"] == "ok"
    item = result["items"][0]
    expected = ((1.001)**(len(src["points"])-1)-1)*100
    assert item["total_return_pct"] == pytest.approx(expected)
    assert item["max_drawdown_pct"] == 0 and item["sharpe_ratio"] is None
    assert item["curve"][0]["value"] == 100
    src["points"][100]["event"] = ""
    assert not calculate_comparison([src], years=1, risk_free_rate=0)["items"]


def test_common_dates_and_risk_free_assumption_are_shared_for_all_funds():
    first, second = source(varied=True), source("510500", varied=True)
    # 某基金没有这一天的观测，之后披露的增长率反映该基金这段间隔的完整变动。
    second["points"].pop(80)
    point, previous = second["points"][80], second["points"][79]
    point["growth_pct"] = (point["nav"]/previous["nav"]-1)*100
    low = calculate_comparison([first, second], years=1, risk_free_rate=0)
    high = calculate_comparison([first, second], years=1, risk_free_rate=3)
    assert len(low["items"]) == 2
    a, b = low["items"]
    assert a["start_date"] == b["start_date"] and a["end_date"] == b["end_date"]
    assert a["observations"] == b["observations"] == len(first["points"])-1
    assert a["total_return_pct"] == pytest.approx(b["total_return_pct"])
    assert low["items"][0]["sharpe_ratio"] > high["items"][0]["sharpe_ratio"]


def test_short_history_and_long_gaps_are_not_reported_as_full_year_risk():
    src = source()
    assert not calculate_comparison([src], years=3, risk_free_rate=0)["items"]
    src["points"] = src["points"][:100] + src["points"][120:]
    assert not calculate_comparison([src], years=1, risk_free_rate=0)["items"]


def test_daily_source_rejects_wrong_code_future_rows_and_conflicting_duplicates():
    stamp = 1794499200000
    rows = [{"x":stamp, "y":1, "equityReturn":.1}, {"x":stamp, "y":2, "equityReturn":.1}]
    script = 'var fS_code="510300";var fS_name="基金";var Data_netWorthTrend='+json.dumps(rows)+';'
    assert parse_daily_returns(script, "510500", now=NOW) == []
    assert parse_daily_returns(script, "510300", now=NOW) == []


def test_analytics_api_validates_input_and_reuses_public_series_cache(monkeypatch):
    from backend.app.models import FactRecord
    calls = []
    class Provider:
        async def get_fund_return_series(self, target):
            calls.append(target)
            src = source()
            return [FactRecord(fact_id="series", field="fund_daily_returns", entity=src["name"], entity_code=target,
                               value=src["points"], period="2025-11-13/2026-11-13", snapshot_time=NOW,
                               source_id="TEST", quality=.9, source_url=src["source_url"])]
    monkeypatch.setattr(main, "data_provider", Provider())
    monkeypatch.setattr(main, "overview_cache", OverviewCache())
    main.app.dependency_overrides[main.customer_user] = lambda: {"id": 1}
    try:
        client = TestClient(main.app)
        payload = {"targets":["510300"], "years":1, "risk_free_rate":2}
        assert client.post('/api/v1/data/fund-analytics', json=payload).json()["status"] == "ok"
        assert client.post('/api/v1/data/fund-analytics', json=payload).json()["status"] == "ok"
        assert calls == ["510300"]
        assert client.post('/api/v1/data/fund-analytics', json={**payload,"targets":["510300"]*5}).status_code == 422
        assert client.post('/api/v1/data/fund-analytics', json={**payload,"risk_free_rate":-1}).status_code == 422
    finally:
        main.app.dependency_overrides.pop(main.customer_user,None)


def test_fund_page_displays_peer_and_industry_and_long_term_controls():
    facts = parse_fund_home(home(), "510300", now=NOW) + parse_ths_portfolio(THS,"510300",now=NOW)
    section = {"key":"reference", "title":"公开档案", "status":"ok", "facts":[f.model_dump(mode="json") for f in facts]}
    script = board_script().replace('def api(base, method, path, payload=None):',
        f"data['sections'].append(json.loads({json.dumps(section,ensure_ascii=False)!r}))\ndef api(base, method, path, payload=None):")
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    assert any("同类名次" in table.value.columns for table in app.dataframe)
    assert any("行业名称" in table.value.columns for table in app.dataframe)
    assert any(button.label == "计算长周期收益与风险" for button in app.button)
    assert any(tab.label == "长周期与对比" for tab in app.tabs)
