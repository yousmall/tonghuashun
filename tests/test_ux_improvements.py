"""历史引用、公共缓存、非阻塞加载、比较身份及期间口径的功能回归。"""
import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event
from time import monotonic

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from backend.app.models import FactRecord
from backend.app.models.schemas import DataOverviewRequest
from backend.app.services.history import summarise_advice
from backend.app.services.overview_cache import OverviewCache
from backend.app.services.comparison import fetch_snapshots
from frontend.answer_report import chart_groups
from frontend.comparison import comparison_matrix
from frontend.presentation import source_trace_rows, _facts_used_by_result
from frontend import research_board as board


def fact(code="600001.SH", entity="公司甲", field="roe", value=7, period="2026Q2", fact_id="a", **kwargs):
    return FactRecord(fact_id=fact_id, entity=entity, entity_code=code, field=field, value=value,
        period=period, snapshot_time=datetime.now(timezone.utc), source_id="TEST", quality=.9,
        source_field="净资产收益率[20260630]", **kwargs)


def test_history_preserves_citations_identity_and_derivation_after_roundtrip():
    original = {"evidence": ["a"], "facts": [fact(derived_from=["raw"]).model_dump(mode="json")],
                "agent_results": [{"agent_id": "stock", "facts_used": ["a"], "opinion": "分析"}]}
    saved = summarise_advice(original)
    assert saved["history_version"] == 2
    assert len(_facts_used_by_result(saved, saved["agent_results"][0])) == 1
    assert saved["facts"][0]["entity_code"] == "600001.SH"
    assert saved["facts"][0]["source_field"] == "净资产收益率[20260630]"
    assert saved["facts"][0]["derived_from"] == ["raw"]
    assert source_trace_rows(saved)[0]["支持分析"] == "个股研究"


class Provider:
    def __init__(self):
        self.calls = []
        self.news_gate = asyncio.Event()
        self.news_gate.set()
    async def query(self, query, **kwargs):
        self.calls.append("quote")
        return [fact(field="change", value=1, period="2026-09-24")]
    async def get_macro_data(self, query):
        self.calls.append("macro")
        return [fact(field="pmi", value=50)]
    async def get_news(self, query):
        self.calls.append("news")
        await self.news_gate.wait()
        return [fact(field="news", value="新闻")]
    async def get_financial_metrics(self, target):
        self.calls.append("financial")
        return [fact()]


@pytest.mark.asyncio
async def test_progressive_cache_shows_quotes_before_news_and_merges_concurrent_requests():
    provider, cache = Provider(), OverviewCache()
    provider.news_gate.clear()
    request = DataOverviewRequest(direction="market", wait=False)
    initial = await cache.get(provider, request)
    assert initial.status == "loading"
    await asyncio.sleep(.02)
    partial = await cache.get(provider, request)
    assert next(section for section in partial.sections if section.key == "overview").status == "ok"
    assert next(section for section in partial.sections if section.key == "news").status == "loading"
    provider.news_gate.set()
    first, second = await asyncio.gather(cache.get(provider, request.model_copy(update={"wait": True})), cache.get(provider, request.model_copy(update={"wait": True})))
    assert first.status == second.status == "ok"
    assert provider.calls.count("quote") == provider.calls.count("macro") == provider.calls.count("news") == 1
    first.sections[0].facts.clear()
    assert (await cache.get(provider, request)).sections[0].facts


@pytest.mark.asyncio
async def test_overview_cache_is_bounded_and_limits_refresh_requests():
    provider, cache = Provider(), OverviewCache(max_entries=1, max_pending=2)
    result = await cache.get(provider, DataOverviewRequest(direction="market"))
    assert len(cache.entries) == 1
    assert any(section.status == "unavailable" for section in result.sections)
    await cache.get(provider, DataOverviewRequest(direction="market", refresh=True))
    count = len(provider.calls)
    await cache.get(provider, DataOverviewRequest(direction="market", refresh=True))
    assert len(provider.calls) == count
    await cache.get(provider, DataOverviewRequest(direction="fund"))
    assert len(cache.entries) == 1


@pytest.mark.asyncio
async def test_comparison_rejects_wrong_security_and_duplicate_resolved_security():
    provider, cache = Provider(), OverviewCache()
    result = await fetch_snapshots(cache, provider, [("600001", "股票", None), ("600002", "股票", None)])
    assert result.items[0].facts
    assert not result.items[1].facts
    assert "不一致" in result.items[1].message
    duplicate = await fetch_snapshots(cache, provider, [("600001", "股票", None), ("公司甲", "股票", None)])
    assert duplicate.items[0].facts
    assert duplicate.items[1].facts == []
    assert "相同" in duplicate.items[1].message


def test_data_routes_require_login():
    import backend.app.main as main
    with TestClient(main.app) as client:
        assert client.post("/api/v1/data/overview", json={"direction": "market"}).status_code == 401
        assert client.post("/api/v1/data/compare", json={"asset_type": "股票", "targets": ["600001", "600002"]}).status_code == 401
        assert client.post("/api/v1/data/watchlist-quotes", json={}).status_code == 401


def test_quarter_charts_and_matrix_choose_latest_common_reporting_period():
    a = fact().model_dump(mode="json")
    b = fact(code="600002.SH", entity="公司乙", fact_id="b", value=8).model_dump(mode="json")
    assert chart_groups({"facts": [a,b], "evidence": ["a","b"]})[0]["period"] == "2026Q2"
    newer = {**a, "period": "2026Q3", "value": 9}
    rows = comparison_matrix([{"target": "甲", "facts": [a,newer]}, {"target": "乙", "facts": [b]}])
    assert rows[0]["期间"] == "2026Q2"
    assert rows[0]["可比性"] == "同期间、同单位"
    assert rows[0]["甲"] == "7.00"


@pytest.mark.parametrize("case", ["missing", "period", "unit", "conflict", "invalid"])
def test_comparison_never_fills_missing_or_incompatible_values_with_zero(case):
    a = fact(field="close_price", value=10, period="2026-09-24").model_dump(mode="json")
    b = fact(code="600002.SH", entity="公司乙", fact_id="b", field="close_price", value=20, period="2026-09-24").model_dump(mode="json")
    bfacts = [b]
    if case == "missing":
        bfacts = []
    elif case == "period":
        b["period"] = "2026-09-23"
    elif case == "unit":
        b["source_field"] = "收盘价（港元）"
    elif case == "invalid":
        b["value"] = None
    else:
        bfacts.append({**b, "value": 21})
    row = comparison_matrix([{"target": "甲", "facts": [a]}, {"target": "乙", "facts": bfacts}])[0]
    assert row["可比性"] != "同期间、同单位"
    assert row["乙"] != "0.00"
    if case == "missing":
        assert row["乙"] == "未取得"


def test_pending_overview_does_not_block_or_duplicate_page_request(monkeypatch):
    import streamlit as st
    state = {"facts": []}
    monkeypatch.setattr(st, "session_state", state)
    gate, started = Event(), Event()
    calls = []
    def fetch(*args):
        calls.append(args)
        started.set()
        assert gate.wait(5)
        return {"direction": "market", "status": "ok", "sections": []}
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(board, "board_prefetch_executor", lambda: executor)
        try:
            stamp = monotonic()
            loaded = board.load_board("test", "market", None, fetch, nonblocking=True)
            assert monotonic()-stamp < 1
            assert loaded["status"] == "loading"
            assert started.wait(1)
            again = board.load_board("test", "market", None, fetch, nonblocking=True)
            assert again["status"] == "loading" and len(calls) == 1
        finally:
            gate.set()
        for task in state["research_board_prefetch"].values():
            task.result(timeout=5)
        assert board.load_board("test", "market", None, fetch, nonblocking=True)["status"] == "ok"
        assert state["facts"] == []


def test_returning_user_home_and_latest_answer_have_compact_native_visuals():
    script = """
import streamlit as st
from unittest.mock import patch
from frontend import streamlit_app as ui
ui.init_session()
st.session_state.auth_token='test-only'
st.session_state.profile['confirmed']=True
st.session_state.watchlist=[{'id':1,'target':'测试标的'}]
with patch.object(ui,'create_board_fetch',return_value=lambda *args: {'direction':'market','status':'unavailable','sections':[]}):
    ui.page_home('http://test-only')
"""
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    assert app.title[0].value == "继续您的投资研究"
    assert any(button.label == "继续上次研究" for button in app.button)
    assert any("使用流程与操作说明" in expander.label for expander in [*app.expander, *app.get("status")])


def test_account_logout_discards_late_snapshot_tasks(monkeypatch):
    import streamlit as st
    from frontend import streamlit_app as ui
    class State(dict):
        def __getattr__(self, key): return self[key]
        def __setattr__(self, key, value): self[key]=value
    running = Future()
    running.set_running_or_notify_cancel()
    state = State(snapshot_watchlist_quotes_task=running, snapshot_watchlist_quotes_value={"private": "old"},
                  watchlist_comparison_request={"targets": ["old"]}, watchlist_focus=1)
    monkeypatch.setattr(st, "session_state", state)
    monkeypatch.setattr(ui, "current_browser_session_id", lambda: None)
    ui.reset_user_session()
    running.set_result({"old": True})
    assert not any(key.startswith("snapshot_") for key in state)
    assert "watchlist_comparison_request" not in state and "watchlist_focus" not in state




def test_home_continue_restores_account_latest_research_without_changing_profile(monkeypatch):
    import streamlit as st
    from frontend import streamlit_app as ui
    class State(dict):
        def __getattr__(self, key): return self[key]
        def __setattr__(self, key, value): self[key] = value
    state = State()
    monkeypatch.setattr(st, "session_state", state)
    ui.init_session()
    state.profile["confirmed"] = True
    state.recent_conversations = [{"id": "saved-fund"}]
    calls = []
    def request(base, method, path):
        calls.append(path)
        return {"id": "saved-fund", "messages": [
            {"role": "user", "content": "基金筛选：比较基金甲", "created_at": "2026-09-25T08:00:00Z"},
            {"role": "assistant", "content": "历史结论", "created_at": "2026-09-25T08:00:01Z", "payload": {"conclusion": "历史结论", "facts": []}}]}
    ui.continue_last_research("test-only", request=request)
    assert calls == ["/history/saved-fund"]
    assert state.navigation == "投资问答"
    assert state.active_research_direction == "基金筛选"
    assert state.conversation_id == "saved-fund"
    assert state.profile["confirmed"] is True


def test_direct_login_switch_clears_old_account_snapshots_and_materials(monkeypatch):
    import streamlit as st
    from frontend import streamlit_app as ui
    class State(dict):
        def __getattr__(self, key): return self[key]
        def __setattr__(self, key, value): self[key]=value
    queued=Future()
    state=State(api_base="test-only", facts=[{"private":"old"}], snapshot_comparison_task=queued,
        snapshot_comparison_value={"private":"old"}, watchlist_comparison_advice={"conclusion":"old"})
    monkeypatch.setattr(st,"session_state",state)
    monkeypatch.setattr(ui,"current_browser_session_id",lambda:None)
    monkeypatch.setattr(ui,"prefetch_research_board_data",lambda base:pytest.fail("Admin fetched customer data"))
    ui.complete_login({"access_token":"admin-test","session_beacon_token":"test","user":{"username":"admin-test","role":"admin"}})
    assert queued.cancelled()
    assert "facts" not in state and "watchlist_comparison_advice" not in state
    assert not any(key.startswith("snapshot_") for key in state)
    assert state.profile["confirmed"] is False
