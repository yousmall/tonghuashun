"""公开基金参考资料的身份、日期、单位、独立降级与页面对比。"""
import json
from datetime import datetime, timezone

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from backend.app.data_provider.fund_reference import parse_fund_profile, parse_fund_trend, fetch_fund_reference
from backend.app.data_provider.iwencai import _canonical_field
from backend.app.data_provider.public_recovery import PublicMarketRecovery
from frontend.research_board import entity_rows, format_fact
from tests.test_fund_details_and_board_watchlist import board_script

NOW = datetime(2026, 11, 15, tzinfo=timezone.utc)
STAMP = 1794499200000
PROFILE = '''<table class="menu"><tr><th>基金代码</th><td>999999</td></tr></table>
<table class="info w790"><tr><th>基金代码</th><td>510300（主代码）<th>基金简称</th><td>测试基金A</td></tr>
<tr><th>基金类型</th><td>指数型-股票</td><th>基金经理人</th><td><a>经理甲</a>、<a>经理乙</a></td></tr>
<tr><th>成立日期/规模</th><td>2012年05月04日 / 10亿份</td></tr>
<tr><th>净资产规模</th><td>1,068.79亿元（截止至：2026年09月30日）<th>份额规模</th><td>241亿份</td></tr></table>'''


def trend(code="510300", stamp=STAMP):
    values = {"fS_code": code, "fS_name": "测试基金A", "syl_1y": "-5.89", "syl_1n": "-12.82",
              "Data_netWorthTrend": [{"x": stamp, "y": 4.0919, "equityReturn": -.69}],
              "Data_ACWorthTrend": [[stamp, 1.8443]],
              "Data_assetAllocation": {"categories": ["2026-06-30", "2026-09-30"], "series": [
                  {"name": "股票占净比", "data": [80, 93.2]}, {"name": "现金占净比", "data": [20, 6.8]}]}}
    return "".join(f"var {key} = {json.dumps(value, ensure_ascii=False)};" for key, value in values.items())


def test_profile_handles_omitted_cell_tags_and_preserves_quarterly_size():
    facts = parse_fund_profile(PROFILE, "510300", now=NOW)
    fields = {fact.field: fact for fact in facts}
    assert "经理甲" in fields["fund_manager"].value and "经理乙" in fields["fund_manager"].value
    assert fields["fund_type"].value == "指数型-股票"
    assert fields["reported_fund_size"].period == "2026-09-30"
    assert format_fact(fields["reported_fund_size"].model_dump()) == "1,068.79 亿元"
    assert format_fact(fields["reported_inception_date"].model_dump()) == "2012-05-04"
    assert all(fact.entity_code == "510300" and fact.source_url.startswith("https://") for fact in facts)
    assert parse_fund_profile(PROFILE, "510500", now=NOW) == []


def test_reference_values_keep_nav_date_stage_label_and_allocation_period():
    fields = {fact.field: fact for fact in parse_fund_trend(trend(), "510300", now=NOW)}
    assert fields["reference_fund_nav"].value == 4.0919
    assert fields["reference_nav_change"].unit == "percent"
    assert fields["reference_return_1m"].value == -5.89
    assert fields["reference_return_1y"].value == -12.82
    assert fields["reference_return_1m"].period == "2026-11-13"
    assert fields["stock_position"].value == 93.2 and fields["stock_position"].period == "2026-09-30"
    assert parse_fund_trend(trend("510500"), "510300", now=NOW) == []
    assert parse_fund_trend(trend(stamp=STAMP+86400000*100), "510300", now=NOW) == []
    assert parse_fund_trend('var fS_code = evil();', "510300", now=NOW) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["510300.SH", "510300.OF"])
async def test_public_reference_has_no_skill_credentials_and_one_source_can_fail(target):
    calls = []
    def transport(request):
        calls.append(request)
        assert "authorization" not in request.headers
        return httpx.Response(503) if "jbgk" in str(request.url) else httpx.Response(200, text=trend())
    recovery = PublicMarketRecovery(transport=httpx.MockTransport(transport), now=lambda: NOW)
    try:
        facts = await fetch_fund_reference(recovery, target)
        assert facts and all(fact.entity_code == target for fact in facts)
        assert await fetch_fund_reference(recovery, "../../wrong") == []
        assert len(calls) == 4
    finally:
        await recovery.aclose()


def test_cumulative_change_and_interval_growth_do_not_hide_daily_change():
    assert _canonical_field("累计单位净值增长率[20261113]") == "accumulated_nav_change"
    assert _canonical_field("单位净值增长率[20261014-20261113]") == "return_1m"
    assert _canonical_field("单位净值增长率[20260814-20261113]") == "return_3m"
    assert _canonical_field("单位净值增长率[20261101-20261113]") == "interval_change"
    facts = [{"entity": "测试基金", "entity_code": "510300.SH", "field": _canonical_field(raw),
              "value": value, "period": "2026-11-13", "unit": "percent"}
             for raw, value in [("单位净值增长率", -.68927), ("累计单位净值增长率", -.571459)]]
    row = entity_rows(facts)[0]
    assert not row["conflicts"] and format_fact(row["fields"]["nav_change"]) == "-0.69%"


def test_page_shows_reference_stage_return_and_original_query_window_side_by_side():
    public = parse_fund_profile(PROFILE, "510300", now=NOW) + parse_fund_trend(trend(), "510300", now=NOW)
    section = {"key": "reference", "status": "ok", "title": "公开档案",
               "facts": [fact.model_dump(mode="json") for fact in public]}
    source = board_script().replace("def api(base, method, path, payload=None):",
        f"data['sections'].append(json.loads({json.dumps(section, ensure_ascii=False)!r}))\n"
        "def api(base, method, path, payload=None):")
    app = AppTest.from_string(source).run(timeout=15)
    assert not app.exception
    returns = next(table.value for table in app.dataframe if "问财区间值" in table.value.columns)
    assert returns.iloc[0]["数值"] == "-5.89%" and returns.iloc[0]["问财区间值"] == "3.40%"
    assert any(metric.label == "净资产规模（披露）" and metric.value == "1,068.79 亿元" for metric in app.metric)
    assert any("同日来源不一致" in warning.value for warning in app.warning)
