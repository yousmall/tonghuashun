"""启动关键路径：慢资料读取和历史统计不阻塞首屏或服务就绪。"""
import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event
from time import monotonic

import httpx
import pytest
import streamlit as st

from backend.app import main
from frontend import research_board, streamlit_app as ui
from frontend.account_prefetch import start_account_prefetch, take_account_result, NOT_PREFETCHED
from frontend.api_client import ApiResult


class State(dict):
    def __getattr__(self, key):
        return self[key]

    def __setattr__(self, key, value):
        self[key] = value


def test_slow_account_reads_are_parallel_and_do_not_block_or_duplicate(monkeypatch):
    state = State()
    monkeypatch.setattr(st, "session_state", state)
    ui.init_session()
    gate, started = Event(), [Event() for _ in range(3)]
    paths = ["/profile", "/history?limit=20", "/watchlist"]
    calls = []

    def transport(request):
        path = request.url.raw_path.decode()
        calls.append(request)
        started[paths.index(path)].set()
        assert gate.wait(5)
        return httpx.Response(200, json={"profile": {"confirmed": True, "version": 4}}
                              if path == "/profile" else [])

    with ThreadPoolExecutor(max_workers=3) as executor, httpx.Client(transport=httpx.MockTransport(transport)) as client:
        monkeypatch.setattr(research_board, "board_prefetch_executor", lambda: executor)
        start_account_prefetch("http://test-only", "original-token", client)
        try:
            assert all(event.wait(2) for event in started), "All reads must start before any finishes"
            state.auth_token = "changed-token"
            monkeypatch.setattr(ui, "api_request", lambda *a, **kw: pytest.fail("Duplicate request"))
            assert ui._restore_profile("http://test-only") is False
            assert not ui.profile_ready()
            assert ui.recent_conversations("http://test-only") is None
            ui.ensure_watchlist_loaded("http://test-only")
            assert not state.get("watchlist_loaded")
        finally:
            gate.set()
        for future in state["account_prefetch"].values():
            future.result(timeout=3)
        assert ui._restore_profile("http://test-only") is True
        assert ui.profile_ready() and state.profile["version"] == 4
        assert ui.recent_conversations("http://test-only") == []
        ui.ensure_watchlist_loaded("http://test-only")
        assert state.watchlist_loaded
        assert len(calls) == 3
        assert all(request.headers["Authorization"] == "Bearer original-token" for request in calls)


def test_logout_discards_late_account_results(monkeypatch):
    state, gate = State(), Event()
    monkeypatch.setattr(st, "session_state", state)
    monkeypatch.setattr(ui, "current_browser_session_id", lambda: None)
    started = Event()

    def transport(request):
        started.set()
        assert gate.wait(5)
        return httpx.Response(200, json={"profile": {"confirmed": True}})

    with ThreadPoolExecutor(max_workers=3) as executor, httpx.Client(transport=httpx.MockTransport(transport)) as client:
        monkeypatch.setattr(research_board, "board_prefetch_executor", lambda: executor)
        start_account_prefetch("http://test-only", "old-account", client)
        try:
            assert started.wait(2)
            futures = list(state.account_prefetch.values())
            ui.reset_user_session()
        finally:
            gate.set()
        for future in futures:
            if not future.cancelled():
                future.result(timeout=3)
        assert take_account_result("http://test-only", "/profile") is NOT_PREFETCHED
        assert not state.profile["confirmed"]


def test_session_check_reuses_recent_login_and_still_revalidates(monkeypatch):
    state = State(auth_token="token", session_status_checked_at=monotonic())
    monkeypatch.setattr(st, "session_state", state)
    calls = []
    monkeypatch.setattr(ui, "api_request", lambda *a: calls.append(a) or {"active": True})
    ui.enforce_session_timeout.__wrapped__("http://test-only")
    assert calls == []
    state.session_status_checked_at -= 31
    ui.enforce_session_timeout.__wrapped__("http://test-only")
    assert len(calls) == 1 and calls[0][2] == "/auth/session/status"


def test_successful_history_write_discards_old_prefetch(monkeypatch):
    old = Future()
    old.set_running_or_notify_cancel()
    state = State(api_base="http://test-only", account_prefetch={
        ("http://test-only", "/history?limit=20"): old,
    })
    monkeypatch.setattr(st, "session_state", state)
    ui.invalidate_recent_conversations()
    old.set_result(ApiResult(data=[{"id": "old", "title": "旧标题"}]))
    monkeypatch.setattr(ui, "api_request", lambda *a, **kw: [{"id": "new", "title": "最新标题"}])
    assert ui.recent_conversations("http://test-only")[0]["id"] == "new"


def test_watchlist_editing_waits_for_initial_snapshot():
    from streamlit.testing.v1 import AppTest
    script = """
from concurrent.futures import Future
from unittest.mock import patch
import streamlit as st
from frontend import streamlit_app as ui
ui.init_session()
st.session_state.auth_token = 'isolated-test'
st.session_state.auth_user = {'username': 'test-user', 'role': 'user'}
st.session_state.profile_restored = True
st.session_state.profile['confirmed'] = True
st.session_state.navigation = '自选研究'
if 'initial_watchlist_read' not in st.session_state:
    st.session_state.initial_watchlist_read = Future()
    st.session_state.account_prefetch = {
        (st.session_state.api_base, '/watchlist'): st.session_state.initial_watchlist_read
    }
with patch.object(ui, 'api_request', return_value=[]), patch.object(ui, 'register_browser_session'), patch.object(ui, 'enforce_session_timeout'):
    ui.main()
"""
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    assert not any(button.label == "加入自选" for button in app.button)
    assert any("正在加载自选标的" in item.value for item in app.info)
    app.session_state["initial_watchlist_read"].set_result(ApiResult(data=[]))
    app.run(timeout=15)
    assert not app.exception
    assert any(button.label == "加入自选" for button in app.button)


@pytest.mark.asyncio
async def test_backend_ready_does_not_wait_for_slow_history_backfill(monkeypatch):
    started, release = Event(), Event()
    observed = {}

    class Database:
        def initialize(self, *, backfill):
            observed["inline_backfill"] = backfill

        def backfill_consultations(self, stop_event):
            observed["stop_event"] = stop_event
            started.set()
            assert release.wait(5)

    monkeypatch.setattr(main, "database", Database())
    monkeypatch.setattr(main, "data_provider", None)
    context = main.lifespan(main.app)
    try:
        await asyncio.wait_for(context.__aenter__(), timeout=2)
        assert observed["inline_backfill"] is False
        assert await asyncio.to_thread(started.wait, 2)
        assert not release.is_set(), "Service is ready while backfill is still blocked"
    finally:
        release.set()
        await context.__aexit__(None, None, None)
    assert observed["stop_event"].is_set()
