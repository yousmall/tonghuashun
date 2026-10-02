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
    calls, gate, started = [], Event(), Event()
    def request(client, base, method, path, payload, token):
        calls.append((path, token))
        started.set()
        assert gate.wait(3), "Session revalidation must not block the script"
        return ApiResult(data={"active": True}, status=200)
    monkeypatch.setattr(ui, "request_json", request)
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(ui, "account_read_executor", lambda: executor)
        ui._check_session_timeout("http://test-only", False)
        assert calls == []
        state.session_status_checked_at -= 31
        try:
            ui._check_session_timeout("http://test-only", False)
            assert started.wait(1)
            ui._check_session_timeout("http://test-only", False)
            assert len(calls) == 1
        finally:
            gate.set()
        state.session_status_task["future"].result(timeout=2)
        ui._check_session_timeout("http://test-only", False)
    assert calls == [("/auth/session/status", "token")]
    assert not state.service_unavailable and "session_status_task" not in state


def test_expired_history_refresh_keeps_cached_rows_and_never_blocks_navigation(monkeypatch):
    from frontend import account_prefetch
    state = State(auth_token="original-token", recent_conversations=[{"id": "cached"}],
                  recent_conversations_loaded_at=0.0)
    monkeypatch.setattr(st, "session_state", state)
    gate, started = Event(), Event()
    calls = []
    def request(client, base, method, path, payload, token):
        calls.append((path, token))
        started.set()
        assert gate.wait(3)
        return ApiResult(data=[{"id": "fresh"}], status=200)
    monkeypatch.setattr(account_prefetch, "request_json", request)
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(account_prefetch, "account_read_executor", lambda: executor)
        try:
            assert ui.recent_conversations("http://test-only", nonblocking=True) == [{"id": "cached"}]
            assert started.wait(1)
            state.auth_token = "changed-token"
            assert ui.recent_conversations("http://test-only", nonblocking=True) == [{"id": "cached"}]
            assert len(calls) == 1
        finally:
            gate.set()
        state.account_prefetch[("http://test-only", "/history?limit=20")].result(timeout=2)
        assert ui.recent_conversations("http://test-only", nonblocking=True) == [{"id": "fresh"}]
    assert calls == [("/history?limit=20", "original-token")]


def test_session_revalidation_unauthorized_clears_login_and_late_checks_are_discarded(monkeypatch):
    future = Future()
    future.set_result(ApiResult(status=401, detail="登录已失效"))
    state = State(auth_token="token", session_status_task={"future": future, "token": "token", "api_base": "test"})
    monkeypatch.setattr(st, "session_state", state)
    monkeypatch.setattr(ui, "current_browser_session_id", lambda: None)
    class Rerun(Exception):
        pass
    monkeypatch.setattr(st, "rerun", lambda: (_ for _ in ()).throw(Rerun()))
    with pytest.raises(Rerun):
        ui._check_session_timeout("test", True)
    assert not state.get("auth_token") and "登录已失效" in state.auth_notice
    state.auth_token = "new-account"
    state.session_status_task = {"future": future, "token": "old-account", "api_base": "test"}
    ui._check_session_timeout("test", False)
    assert state.auth_token == "new-account" and "session_status_task" not in state


def test_brand_logo_uses_media_url_and_registers_original_bytes_on_every_run(monkeypatch):
    from types import SimpleNamespace
    calls = []
    def add(data, mime, coordinates):
        calls.append((data, mime, coordinates))
        return "/media/original-logo.png"
    monkeypatch.setattr(ui, "get_instance", lambda: SimpleNamespace(media_file_mgr=SimpleNamespace(add=add)))
    monkeypatch.setattr(st, "get_option", lambda name: "research")
    version = ui.BRAND_LOGO.stat().st_mtime_ns
    assert ui.brand_logo_uri(version) == "/research/media/original-logo.png"
    assert ui.brand_logo_uri(version) == "/research/media/original-logo.png"
    assert len(calls) == 2
    assert all(data == ui.BRAND_LOGO.read_bytes() and mime == "image/png" for data, mime, _ in calls)


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
