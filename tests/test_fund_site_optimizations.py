"""真实网站对照发现的精度、货币基金类型与展示回归。"""
import json

import pytest
from streamlit.testing.v1 import AppTest

from backend.app.data_provider.fund_reference import parse_fund_trend
from backend.app.data_provider.iwencai import _canonical_field
from backend.app.services.fund_analytics import calculate_comparison
from frontend.research_board import format_fact
from frontend.watchlist_page import quote_rows
from tests.test_fund_extended_features import source
from tests.test_fund_reference import NOW, STAMP


def money_script(code="000198", stamp=STAMP):
    values = {"fS_code":code,"fS_name":"测试货币基金","ishb":True,
        "Data_sevenDaysYearIncome":[[stamp-86400000,.798],[stamp,.797]],
        "Data_millionCopiesIncome":[[stamp-86400000,.2125],[stamp,.2168]],
        "syl_1y":"0.07","syl_1n":"0.92"}
    return "".join(f"var {key}={json.dumps(value,ensure_ascii=False)};" for key,value in values.items())


def test_non_event_nav_precision_prevents_rounded_daily_return_accumulation():
    src = source()
    nav = 1.0
    for point in src["points"]:
        nav *= 1.000449
        point.update(nav=nav,growth_pct=.04)
    result = calculate_comparison([src],years=1,risk_free_rate=0)
    item = result["items"][0]
    expected = (src["points"][-1]["nav"]/src["points"][0]["nav"]-1)*100
    rounded = (1.0004**(len(src["points"])-1)-1)*100
    assert item["total_return_pct"] == pytest.approx(expected,abs=1e-10)
    assert abs(item["total_return_pct"]-rounded) > 1
    assert result["calculation_version"].endswith("v2")


def test_precision_improvement_still_uses_reported_growth_on_distribution_days():
    src = source(dividend=True)
    result = calculate_comparison([src],years=1,risk_free_rate=0)
    assert result["items"][0]["total_return_pct"] == pytest.approx((1.001**(len(src["points"])-1)-1)*100)
    assert result["items"][0]["total_return_pct"] > (src["points"][-1]["nav"]/src["points"][0]["nav"]-1)*100
    src["points"][100]["event"] = ""
    assert calculate_comparison([src],years=1,risk_free_rate=0)["items"] == []


def test_money_fund_preserves_units_dates_and_does_not_fabricate_nav():
    fields = {f.field:f for f in parse_fund_trend(money_script(),"000198",now=NOW)}
    assert fields["yield_7d"].unit == "percent"
    assert fields["income_per_10000"].unit == "CNY"
    assert fields["income_per_10000"].value == .2168
    assert fields["yield_7d"].period == fields["income_per_10000"].period == "2026-11-13"
    assert fields["reference_return_1y"].value == .92
    assert "reference_fund_nav" not in fields and "reference_nav_change" not in fields
    assert format_fact(fields["yield_7d"].model_dump()) == "0.7970%"
    assert format_fact(fields["income_per_10000"].model_dump()) == "0.2168 元"
    assert parse_fund_trend(money_script("000199"),"000198",now=NOW) == []
    assert parse_fund_trend(money_script(stamp=STAMP+86400000*100),"000198",now=NOW) == []


def test_money_series_conflicts_are_not_silently_accepted_and_metric_dates_remain_separate():
    text = money_script().replace(f'[{STAMP}, 0.2168]',f'[{STAMP}, 0.2168], [{STAMP}, 0.5]')
    fields = {f.field:f for f in parse_fund_trend(text,"000198",now=NOW)}
    assert fields["yield_7d"].period == "2026-11-13"
    assert fields["income_per_10000"].period == "2026-11-12"
    assert fields["income_per_10000"].value == .2125


@pytest.mark.parametrize("raw,field",[("七日年化收益率[20261113]","yield_7d"),
    ("7日年化[20261113]","yield_7d"),("每万份收益[20261113]","income_per_10000"),
    ("万份基金单位收益[20261113]","income_per_10000")])
def test_money_field_aliases_are_distinct_from_annual_and_daily_nav_returns(raw,field):
    assert _canonical_field(raw) == field


def test_money_fund_page_has_yield_income_and_chart_without_nav_risk_calculation():
    facts = [f.model_dump(mode="json") for f in parse_fund_trend(money_script(),"000198",now=NOW)]
    data = {"target":"000198","sections":[{"key":"reference","status":"ok","facts":facts}]}
    script = f'''
import streamlit as st
from frontend.fund_view import render_fund_details
st.session_state.watchlist = []
render_fund_details({data!r},'000198',api_base='test-only',api_request=lambda *args: None)
'''
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    assert {m.label:m.value for m in app.metric}["七日年化收益率"] == "0.7970%"
    assert {m.label:m.value for m in app.metric}["每万份收益"] == "0.2168 元"
    assert "单位净值" not in [m.label for m in app.metric]
    assert len(app.get("vega_lite_chart")) == 1
    assert not any(b.label == "计算长周期收益与风险" for b in app.button)


def test_money_watchlist_does_not_mislabel_yield_as_nav_or_unavailable():
    facts = [f.model_dump(mode="json") for f in parse_fund_trend(money_script(),"000198",now=NOW)
             if f.field in {"yield_7d","income_per_10000"}]
    result = quote_rows([{"id":1,"target":"000198","asset_type":"基金"}],
                       {"items":[{"watchlist_id":1,"facts":facts,"status":"ok"}]})
    assert result[0]["价格 / 净值"] == "—" and result[0]["状态"] == "可查看"
    assert result[0]["七日年化"] == "0.7970%" and result[0]["每万份收益"] == "0.2168 元"


@pytest.mark.parametrize("original_date", ["2026-11-12", "20261112", ""])
def test_money_page_keeps_newer_public_date_and_older_query_date_visible(original_date):
    facts = [f.model_dump(mode="json") for f in parse_fund_trend(money_script(),"000198",now=NOW)]
    original = {**next(f for f in facts if f['field']=='yield_7d'), 'period':original_date, 'value':.8,
                'source_id':'QUERY2DATA','fact_id':'query-yield'}
    data = {'target':'000198','sections':[{'key':'overview','status':'ok','facts':[original]},
        {'key':'reference','status':'ok','facts':facts}]}
    app = AppTest.from_string(f'''
import streamlit as st
from frontend.fund_view import render_fund_details
render_fund_details({data!r},'000198')
''').run(timeout=15)
    assert not app.exception
    table = next(t.value for t in app.dataframe if '问财日期' in t.value.columns)
    row = table.iloc[0]
    assert row['数值']=='0.7970%' and row['数据日期']=='2026-11-13'
    assert row['问财值']=='0.8000%' and row['问财日期']==('2026-11-12' if original_date else '日期未提供')
    assert next(m.value for m in app.metric if m.label=='七日年化收益率')=='0.7970%'
