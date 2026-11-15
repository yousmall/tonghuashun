"""超过50个自选的容量、跨页行情、账号隔离和翻页操作。"""
from datetime import datetime, timezone
import asyncio

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from backend.app import main
from backend.app.database import Database, WatchlistCapacityExceeded
from backend.app.models.schemas import ComparisonItem, ComparisonResponse
from backend.app.models import FactRecord
from backend.app.services.comparison import fetch_snapshots
from backend.app.services.overview_cache import OverviewCache
from backend.app.watchlist_policy import WATCHLIST_CAPACITY


@pytest.fixture
def accounts(monkeypatch):
    database = Database("sqlite+pysqlite:///:memory:", "test-secret-that-is-longer-than-thirty-two-characters")
    database.initialize()
    first = database.create_user("many-watchlist", "hashed")
    second = database.create_user("other-watchlist", "hashed")
    monkeypatch.setattr(main, "database", database)
    original = dict(main.app.dependency_overrides)
    main.app.dependency_overrides[main.customer_user] = lambda: {"id":int(first["id"])}
    try:
        yield database, int(first["id"]), int(second["id"]), TestClient(main.app)
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(original)


def test_more_than_fifty_persist_and_capacity_is_enforced_through_api(accounts, monkeypatch):
    database, owner, other, client = accounts
    for index in range(WATCHLIST_CAPACITY-1):
        database.add_watchlist_item(owner, str(600000+index), "股票", f"测试标的{index+1}")
    async def identity(provider, target, kind):
        return "新增自选", target
    monkeypatch.setattr(main, "resolve_security_identity", identity)
    response = client.post("/api/v1/watchlist", json={"target":"601999","asset_type":"股票"})
    assert response.status_code == 200
    restored = client.get("/api/v1/watchlist").json()
    assert len(restored) == WATCHLIST_CAPACITY == 200
    assert restored[0]["target"] == "601999"
    assert restored[-1]["target"] == "600000"
    assert database.list_watchlist(other) == []
    blocked = client.post("/api/v1/watchlist", json={"target":"602000","asset_type":"股票"})
    assert blocked.status_code == 422 and "200" in blocked.json()["detail"]
    with pytest.raises(WatchlistCapacityExceeded):
        database.add_watchlist_item(owner, "602001", "股票")


def test_quotes_follow_current_page_order_and_reject_other_account_ids(accounts, monkeypatch):
    database, owner, other, client = accounts
    items = [database.add_watchlist_item(owner, str(600000+i), "股票", f"测试{i}") for i in range(65)]
    private = database.add_watchlist_item(other, "000001", "基金", "其他账号")
    calls = []
    async def fetch(cache, provider, targets, **kwargs):
        calls.append(targets)
        return ComparisonResponse(status="empty", fetched_at=datetime.now(timezone.utc), items=[
            ComparisonItem(target=target, asset_type=kind, watchlist_id=item_id, status="empty", facts=[])
            for target, kind, item_id in targets])
    monkeypatch.setattr(main, "fetch_snapshots", fetch)
    requested = [item["id"] for item in items[:50]]  # 包含原先最新20个以外的记录。
    response = client.post("/api/v1/data/watchlist-quotes", json={"ids":requested,"wait":True})
    assert response.status_code == 200
    assert [item["watchlist_id"] for item in response.json()["items"]] == requested
    assert len(calls[-1]) == 50
    assert response.json()["items"][0]["display_name"] == "测试0"
    count = len(calls)
    assert client.post("/api/v1/data/watchlist-quotes", json={"ids":[private["id"]]}).status_code == 404
    assert len(calls) == count
    assert client.post("/api/v1/data/watchlist-quotes", json={}).status_code == 200
    assert len(calls[-1]) == 20  # 旧客户端兼容。
    database.remove_watchlist_item(owner, requested[0])
    assert client.post("/api/v1/data/watchlist-quotes", json={"ids":[requested[0]]}).status_code == 404


@pytest.mark.parametrize("ids", [[],[1,1],[0],[-1],list(range(1,52))])
def test_quote_batch_input_is_bounded_and_distinct(accounts, ids):
    assert accounts[-1].post("/api/v1/data/watchlist-quotes", json={"ids":ids}).status_code == 422


@pytest.mark.asyncio
async def test_fifty_real_cache_loads_do_not_exhaust_the_pending_queue():
    class Provider:
        def __init__(self):
            self.active = self.peak = 0
        async def query(self, query, **kwargs):
            self.active += 1
            self.peak = max(self.peak,self.active)
            try:
                await asyncio.sleep(.001)
                code = query[:6]
                return [FactRecord(fact_id="quote-"+code,entity="测试"+code,entity_code=code+".SH",
                    field="close_price",value=10,snapshot_time=datetime.now(timezone.utc),source_id="TEST",quality=.9)]
            finally:
                self.active -= 1
        async def get_financial_metrics(self, target):
            await asyncio.sleep(.001)
            return [FactRecord(fact_id="financial-"+target,entity="测试"+target,entity_code=target+".SH",
                field="roe",value=12,snapshot_time=datetime.now(timezone.utc),source_id="TEST",quality=.9)]
    provider = Provider()
    cache = OverviewCache(max_pending=16)
    targets = [(str(600000+i),"股票",i+1) for i in range(50)]
    result = await fetch_snapshots(cache,provider,targets,wait=True)
    assert len(result.items) == 50 and all(item.facts for item in result.items)
    assert all(item.status == "ok" for item in result.items)
    assert provider.peak <= 8
    assert [item.watchlist_id for item in result.items] == list(range(1,51))
    await cache.aclose()


def render_many_watchlist():
    import streamlit as st
    from unittest.mock import patch
    from frontend.watchlist_page import render_watchlist
    st.session_state.setdefault("watchlist", [
        {"id":i,"target":str(600000+i),"name":f"测试标的{i:02d}","asset_type":"基金" if i%3 == 0 else "股票"}
        for i in range(1,58)])
    st.session_state.setdefault("requests", [])
    def factory(base, path, payload):
        st.session_state.requests.append(payload)
        def fetch(refresh=False):
            return {"status":"empty", "items":[]}
        return fetch
    def api(base, method, path, payload=None):
        if method == "DELETE":
            return {"deleted":True}
        if method == "POST":
            return {"id":100,"target":payload["target"],"asset_type":payload["asset_type"]}
    actions = {"header":lambda *args: None,"factory":factory,"api":api,"history":lambda *args: None,
               "ready":lambda: True}
    with patch("frontend.watchlist_page.poll_snapshot", side_effect=lambda name, signature, fetch, **kwargs: fetch()):
        render_watchlist("test-only", actions)


def test_fifty_seven_items_paginate_with_exact_page_quotes_and_cross_page_comparison():
    app = AppTest.from_function(render_many_watchlist).run(timeout=15)
    assert not app.exception
    assert len(app.dataframe[0].value) == 20
    assert app.session_state["requests"][-1]["ids"] == list(range(1,21))
    assert app.button(key="watchlist_page_previous").disabled
    app.multiselect(key="watchlist_compare_ids").set_value([1]).run(timeout=15)
    app.button(key="watchlist_page_next").click().run(timeout=15)
    assert not app.exception
    assert app.session_state["requests"][-1]["ids"] == list(range(21,41))
    assert app.selectbox(key="watchlist_focus").value == 21
    app.multiselect(key="watchlist_compare_ids").set_value([1,22]).run(timeout=15)
    app.button(key="watchlist_page_next").click().run(timeout=15)
    assert not app.exception and len(app.dataframe[0].value) == 17
    assert app.session_state["requests"][-1]["ids"] == list(range(41,58))
    assert app.button(key="watchlist_page_next").disabled
    assert app.multiselect(key="watchlist_compare_ids").value == [1,22]
    app.selectbox(key="watchlist_page_number").set_value(1).run(timeout=15)
    assert app.session_state["requests"][-1]["ids"] == list(range(1,21))


def test_page_size_search_filter_addition_and_last_page_removal_keep_valid_pages():
    app = AppTest.from_function(render_many_watchlist).run(timeout=15)
    app.selectbox(key="watchlist_page_size").set_value(30).run(timeout=15)
    assert not app.exception and len(app.dataframe[0].value) == 30
    app.button(key="watchlist_page_next").click().run(timeout=15)
    assert len(app.dataframe[0].value) == 27
    app.selectbox(key="watchlist_page_size").set_value(50).run(timeout=15)
    assert app.selectbox(key="watchlist_page_number").value == 1
    assert len(app.dataframe[0].value) == 50
    app.button(key="watchlist_page_next").click().run(timeout=15)
    app.text_input(key="watchlist_search").set_value("测试标的57").run(timeout=15)
    assert not app.exception and len(app.dataframe[0].value) == 1
    assert app.selectbox(key="watchlist_page_number").value == 1
    assert app.session_state["requests"][-1]["ids"] == [57]
    app.text_input(key="watchlist_search").set_value("").run(timeout=15)
    app.selectbox(key="watchlist_filter").set_value("基金").run(timeout=15)
    assert len(app.dataframe[0].value) == 19
    app.selectbox(key="watchlist_filter").set_value("全部").run(timeout=15)
    app.button(key="watchlist_page_next").click().run(timeout=15)
    app.session_state["watchlist"] = app.session_state["watchlist"][:51]
    app.run(timeout=15)
    app.button(key="watchlist_remove_51").click().run(timeout=15)
    assert not app.exception and app.selectbox(key="watchlist_page_number").value == 1
    assert len(app.session_state["watchlist"]) == 50
    # 新增记录回到首页，新增内容立即可见。
    app.selectbox(key="watchlist_page_size").set_value(20).run(timeout=15)
    app.button(key="watchlist_page_next").click().run(timeout=15)
    app.text_input(key="watchlist_target").set_value("601000")
    next(button for button in app.button if button.label == "加入自选").click().run(timeout=15)
    assert not app.exception and app.selectbox(key="watchlist_page_number").value == 1
    assert app.session_state["requests"][-1]["ids"][0] == 100


@pytest.mark.parametrize("count,label", [(0,"0"),(29,"29"),(30,"30+"),(49,"30+"),(50,"50+"),(57,"50+"),(200,"50+")])
def test_home_followed_count_uses_actual_thresholds(count, label):
    from frontend.home_page import watchlist_count_label
    assert watchlist_count_label(count) == label


@pytest.mark.parametrize("loaded,count,label", [(False,0,"未取得"),(True,0,"0"),(True,35,"30+"),(True,57,"50+")])
def test_home_metric_keeps_exact_total_in_help_and_unknown_is_not_zero(loaded, count, label):
    script = f'''
import streamlit as st
from unittest.mock import patch
from frontend.home_page import render_home
st.session_state.profile = {{'risk_level':'R3'}}
st.session_state.watchlist = [{{'id':i}} for i in range({count})]
st.session_state.watchlist_loaded = {loaded!r}
with patch('frontend.home_page.market_glance'):
    render_home('test-only',ready=True,header=lambda *args: None,go_to=lambda *args: None,
                recent=[],fetch=lambda *args: None,resume=lambda *args: None)
'''
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    metric = next(item for item in app.metric if item.label == "关注标的")
    assert metric.value == label
    if loaded:
        assert f"实际关注{count}个" in metric.proto.help
