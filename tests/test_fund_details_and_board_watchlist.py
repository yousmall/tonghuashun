"""基金详情和研究子页自选：字段口径、身份、交互与持久化回归。"""
from datetime import datetime, timezone
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from backend.app.data_provider.iwencai import IwencaiSkillHubProvider
from backend.app.database import Database
from backend.app.models import FactRecord
from backend.app.models.schemas import DataOverviewRequest, ComparisonRequest
from backend.app.services.market_overview import fetch_overview
from backend.app.services.security_names import resolve_security_identity
from frontend.research_board import format_fact


def fact(field="fund_nav", value=1.25, code="510300.SH", name="测试基金A", period="2026-11-13", unit=None):
    return FactRecord(fact_id="fund-" + field, entity=name, entity_code=code, field=field, value=value,
                      period=period, unit=unit, snapshot_time=datetime.now(timezone.utc), source_id="TEST", quality=.9)


@pytest.mark.asyncio
async def test_fund_fields_preserve_windows_units_dates_and_structured_holdings():
    source = {"基金代码": "510300.SH", "基金简称": "测试基金A", "单位净值[20261113]": 1.25,
              "累计单位净值[20261113]": 1.8, "净值涨跌幅[20261113]": -.2,
              "近1月单位净值增长率[20261113]": 3.4, "近三个月净值增长率[20261113]": 8.1,
              "基金@区间涨跌幅[20260514-20261113]": 10.2,
              "基金@区间涨跌幅[20231114-20261113]": 30.3,
              "基金@最大回撤率[20251114-20261113]": 12.4,
              "基金@诊基-近1年夏普率": .82,
              "今年以来收益率[20261113]": 12, "近1年最大回撤[20261113]": 15,
              "近1年夏普比率[20261113]": .82, "托管费率(%)": .1,
              "股票仓位[20260930]": 93.2, "基金管理人": "测试管理人",
              "重仓证券[20260930]": [{"名称": "证券甲", "代码": "600519", "占比": "8%"}]}
    provider = IwencaiSkillHubProvider("test-only", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"datas": [source]})))
    try:
        facts = await provider.query("基金详情测试", skill_id="hithink-fund-query")
    finally:
        await provider.aclose()
    fields = {row.field: row for row in facts}
    assert fields["return_1m"].value == 3.4 and fields["return_3m"].value == 8.1
    assert fields["nav_change"].value == -.2
    assert fields["accumulated_nav"].value == 1.8
    assert fields["return_6m"].value == 10.2 and fields["return_3y"].value == 30.3
    assert fields["return_6m"].period == "2026-05-14/2026-11-13"
    assert fields["max_drawdown_1y"].unit == "percent"
    assert fields["stock_position"].period == "2026-09-30"
    assert fields["top_holdings"].value[0]["代码"] == "600519"
    assert format_fact(fields["custody_fee_rate"].model_dump()) == "0.10%"
    assert format_fact(fields["return_1m"].model_dump()) == "3.40%"
    assert format_fact(fields["sharpe_ratio_1y"].model_dump()) == "0.82"
    assert format_fact(fact("return_1y", .12, unit="ratio").model_dump()) == "12.00%"
    assert "单位待确认" in format_fact(fact("return_1y", .12).model_dump())


def test_stock_interval_returns_and_unspecified_fund_year_are_not_relabelled():
    from backend.app.data_provider.iwencai import _canonical_field
    assert _canonical_field("涨跌幅[20261014-20261113]") == "interval_change"
    assert _canonical_field("基金@区间涨跌幅[20261101-20261113]") == "interval_change"
    assert _canonical_field("基金@年收益率") != "return_1y"


@pytest.mark.asyncio
async def test_industry_records_keep_both_name_and_code():
    provider = IwencaiSkillHubProvider("test-only", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"datas": [{"板块简称": "半导体", "板块代码": "881121.TI", "涨跌幅": 1.2}]})))
    try:
        facts = await provider.query("行业测试")
    finally:
        await provider.aclose()
    assert facts and all(row.entity == "半导体" and row.entity_code == "881121.TI" for row in facts)


@pytest.mark.asyncio
async def test_fund_detail_sections_fail_independently_and_reject_other_fund():
    class Provider:
        async def query(self, query, **kwargs):
            if "股票仓位" in query:
                raise RuntimeError("private diagnostic")
            if "近1月收益率" in query:
                return [fact("return_1m", 5, code="510500.SH")]
            return [fact()]

        async def get_price_history(self, *args, **kwargs):
            return [fact(period="2026-11-12"), fact(value=1.3)]

        async def get_news(self, *args):
            return [fact("news", "测试资讯")]

    result = await fetch_overview(Provider(), DataOverviewRequest(direction="fund", target="510300"))
    sections = {section.key: section for section in result.sections}
    assert sections["overview"].facts and sections["profile"].facts and sections["history"].facts
    assert sections["performance"].status == "empty"
    assert sections["allocation"].status == "unavailable"
    assert result.status == "partial" and "private diagnostic" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_fund_names_with_multiple_share_codes_are_not_arbitrarily_resolved():
    class Provider:
        async def query(self, *args, **kwargs):
            return [fact(code="000001", name="同名基金"), fact(code="000002", name="同名基金")]
    assert await resolve_security_identity(Provider(), "同名基金", "基金") is None


@pytest.mark.asyncio
async def test_fund_alias_loads_unique_full_name_details_but_not_ambiguous_results():
    from backend.app.services.market_overview import fund_snapshot
    class Provider:
        def __init__(self, ambiguous=False):
            self.ambiguous = ambiguous
        async def query(self, *args, **kwargs):
            assert kwargs['limit'] == 5
            return [fact()] + ([fact(code="510500.SH", name="另一只基金")] if self.ambiguous else [])
    assert (await fund_snapshot(Provider(), "基金别名", "基金规模"))[0].entity_code == "510300.SH"
    assert await fund_snapshot(Provider(True), "基金别名", "基金规模") == []


def test_name_query_persists_code_and_name_and_prevents_duplicate_code(monkeypatch):
    import backend.app.main as main

    database = Database("sqlite+pysqlite:///:memory:", "test-secret-that-is-longer-than-thirty-two-characters")
    database.initialize()
    user = database.create_user("fund-watch", "hashed")

    class Provider:
        async def query(self, *args, **kwargs):
            return [fact()]

    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "data_provider", Provider())
    main.app.dependency_overrides[main.customer_user] = lambda: {"id": int(user["id"])}
    try:
        client = TestClient(main.app)
        added = client.post("/api/v1/watchlist", json={"target": "测试基金A", "asset_type": "基金"})
        assert added.status_code == 200
        assert added.json()["target"] == "510300.SH" and added.json()["name"] == "测试基金A"
        assert client.get("/api/v1/watchlist").json()[0]["name"] == "测试基金A"
        assert client.post("/api/v1/watchlist", json={"target": "510300", "asset_type": "基金"}).status_code == 409
    finally:
        main.app.dependency_overrides.pop(main.customer_user, None)


def board_script(page="基金筛选", target="510300", mismatch=False):
    direction, kind, code, name = {
        "市场解读": ("market", "指数", "000001.SH", "上证指数"),
        "行业分析": ("industry", "行业", "881121.TI", "半导体"),
        "个股研究": ("stock", "股票", "600519.SH", "贵州茅台"),
        "基金筛选": ("fund", "基金", "510300.SH", "测试基金A"),
        "可转债分析": ("convertible", "可转债", "113001.SH", "测试转债"),
    }[page]
    fields = {"fund_nav": 1.25, "accumulated_nav": 1.8, "close_price": 10, "change": .2,
              "nav_change": .2, "return_1m": 3.4, "fund_size": 1e9, "fee_rate": .15,
              "custody_fee_rate": .05, "fund_manager": "测试经理", "fund_company": "测试公司",
              "stock_position": 93, "max_drawdown_1y": 12, "sharpe_ratio_1y": .8}
    facts = [fact(field, value, code="510500.SH" if mismatch else code, name=name,
                  unit="percent" if field in {"return_1m", "stock_position", "fee_rate", "custody_fee_rate", "max_drawdown_1y", "change", "nav_change"} else None).model_dump(mode="json")
             for field, value in fields.items()]
    payload = {"direction": direction, "target": target, "status": "ok", "sections": [
        {"key": "overview", "title": "概览", "status": "ok", "facts": facts},
        {"key": "history", "title": "历史", "status": "ok", "facts": [
            fact(code=code, name=name, period="2026-11-12").model_dump(mode="json"),
            fact(code=code, name=name, value=1.3).model_dump(mode="json")]}]}
    return f'''
from concurrent.futures import Future
from unittest.mock import patch
import json
import streamlit as st
from frontend import research_board as board
class ImmediateExecutor:
    def submit(self, fn, *args):
        future = Future()
        future.set_result(fn(*args))
        return future
st.session_state.setdefault('board_target_{direction}', {target!r})
st.session_state.setdefault('watchlist', [])
st.session_state.setdefault('writes', [])
data = json.loads({json.dumps(payload, ensure_ascii=False)!r})
def api(base, method, path, payload=None):
    if method == 'POST' and path == '/watchlist':
        st.session_state.writes.append(payload)
        return {{'id': 42, 'target': payload['target'], 'asset_type': payload['asset_type'], 'name': {name!r}}}
    return {{'items': []}}
with patch.object(board, 'board_prefetch_executor', return_value=ImmediateExecutor()):
    board._render_research_board('test-only', {page!r}, lambda *args: data, api)
'''


@pytest.mark.parametrize("page,target,kind", [("市场解读", "000001.SH", "指数"), ("行业分析", "881121", "行业"),
                                            ("个股研究", "600519", "股票"), ("基金筛选", "510300", "基金"),
                                            ("可转债分析", "113001", "可转债")])
def test_every_research_page_can_save_and_reopen_a_verified_target(page, target, kind):
    app = AppTest.from_string(board_script(page, target)).run(timeout=15)
    assert not app.exception
    button = next(button for button in app.button if button.label == "加入自选")
    assert not button.disabled
    button.click().run(timeout=15)
    assert not app.exception
    assert app.session_state["writes"][-1]["asset_type"] == kind
    saved = app.session_state["watchlist"][0]
    assert saved["name"] and saved["target"].split(".")[0] == target.split(".")[0]
    assert next(button for button in app.button if button.label == "已在自选").disabled
    app.run(timeout=15)
    assert app.session_state["watchlist"][0] == saved


def test_fund_detail_shows_source_metrics_dates_and_missing_fields():
    app = AppTest.from_string(board_script()).run(timeout=15)
    assert not app.exception
    assert [tab.label for tab in app.tabs] == ["收益与风险", "概况与费率", "持仓与配置", "长周期与对比"]
    assert any("测试基金A · 510300.SH" in item.value for item in app.markdown)
    returns = next(table.value for table in app.dataframe if "指标" in table.value.columns and "近1月收益率" in table.value["指标"].tolist())
    assert returns.iloc[0]["数值"] == "3.40%" and returns.iloc[0]["数据日期"] == "2026-11-13"
    assert returns.iloc[1]["数值"] == "—"
    assert app.get("vega_lite_chart")


def test_wrong_fund_code_is_neither_shown_nor_saved():
    app = AppTest.from_string(board_script(mismatch=True)).run(timeout=15)
    assert not app.exception
    assert next(button for button in app.button if button.label == "加入自选").disabled
    assert not app.metric
    assert not app.session_state["writes"]


def test_fund_name_input_can_query_and_display_name_and_code():
    app = AppTest.from_string(board_script(target="测试基金A")).run(timeout=15)
    assert not app.exception
    assert any("测试基金A · 510300.SH" in item.value for item in app.markdown)
    assert not next(button for button in app.button if button.label == "加入自选").disabled


def test_live_history_shape_without_code_keeps_verified_query_curve():
    source = board_script().replace("def api(base, method, path, payload=None):", """
for section in data['sections']:
    if section['key'] == 'history':
        for row in section['facts']:
            row['entity_code'] = None
            row['entity'] = '510300'
def api(base, method, path, payload=None):""")
    app = AppTest.from_string(source).run(timeout=15)
    assert not app.exception
    assert app.get("vega_lite_chart")


def test_multiple_fund_shares_are_displayed_and_saved_separately():
    source = board_script(target="测试基金").replace("def api(base, method, path, payload=None):", """
import copy
other = copy.deepcopy(data['sections'][0]['facts'])
for row in other:
    row['entity_code'] = '510500.SH'
    row['entity'] = '测试基金C'
data['sections'][0]['facts'].extend(other)
def api(base, method, path, payload=None):""")
    app = AppTest.from_string(source).run(timeout=15)
    assert not app.exception
    assert next(button for button in app.button if button.label == "加入自选").disabled
    app.selectbox(key="board_favorite_fund").select("510500.SH").run(timeout=15)
    assert not app.exception
    next(button for button in app.button if button.label == "加入自选").click().run(timeout=15)
    assert app.session_state["writes"][-1]["target"] == "510500.SH"


def test_index_comparison_contract_accepts_new_watchlist_type():
    assert ComparisonRequest(asset_type="指数", targets=["000001.SH", "399001.SZ"]).asset_type == "指数"
