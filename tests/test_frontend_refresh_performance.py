"""Refresh regressions: slow index reads and healthy leases never block the whole page."""
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event
from time import monotonic
from types import SimpleNamespace

import httpx
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from frontend import index_board, snapshots, streamlit_app as ui
from frontend.api_client import ApiResult, snapshot_fetch_factory


class State(dict):
    def __getattr__(self, key):
        return self[key]

    def __setattr__(self, key, value):
        self[key] = value


@pytest.fixture
def state(monkeypatch):
    value = State(auth_token="original-token")
    monkeypatch.setattr(st, "session_state", value)
    return value


def no_sync_request(*args, **kwargs):
    pytest.fail("Background index view must never make a synchronous API call")


def test_healthy_lease_submission_and_completion_do_not_rerun_page(state, monkeypatch):
    future = Future()
    monkeypatch.setattr(ui, "get_script_run_ctx", lambda **kw: SimpleNamespace(fragment_ids_this_run={"lease"}))
    monkeypatch.setattr(st, "rerun", lambda **kw: pytest.fail("Healthy lease redrew whole page"))
    monkeypatch.setattr(ui, "account_read_executor", lambda: SimpleNamespace(submit=lambda *a: future))
    monkeypatch.setattr(ui, "backend_http_client", lambda base: object())
    ui._check_session_timeout("http://test", False)
    assert state.session_status_task["future"] is future
    ui._check_session_timeout("http://test", True)
    future.set_result(ApiResult(data={"active": True}, status=200))
    ui._check_session_timeout("http://test", True)
    assert not state.service_unavailable
    assert "session_status_task" not in state
    assert monotonic() - state.session_status_checked_at < 1


def test_changed_service_status_still_reruns_page(state, monkeypatch):
    future = Future()
    future.set_result(ApiResult(detail="offline"))
    state.session_status_task = {"future": future, "token": state.auth_token, "api_base": "test"}
    class Rerun(Exception):
        pass
    monkeypatch.setattr(st, "rerun", lambda: (_ for _ in ()).throw(Rerun()))
    with pytest.raises(Rerun):
        ui._check_session_timeout("test", False)
    assert state.service_unavailable


def test_index_loading_coalesces_requests_and_keeps_auth_snapshot(state, monkeypatch):
    started, release = Event(), Event()
    calls = []
    def transport(request):
        calls.append(request)
        started.set()
        assert release.wait(5)
        return httpx.Response(200, json={"status": "ok", "items": [{"name": "test-index"}]})
    with ThreadPoolExecutor(max_workers=1) as pool, httpx.Client(transport=httpx.MockTransport(transport)) as client:
        monkeypatch.setattr(snapshots, "board_prefetch_executor", lambda: pool)
        factory = snapshot_fetch_factory(client, "original-token")
        try:
            assert index_board.load_index_board("http://test", no_sync_request, factory=factory)["status"] == "loading"
            assert started.wait(1)
            for _ in range(5):
                assert index_board.load_index_board("http://test", no_sync_request, factory=factory)["status"] == "loading"
            assert len(calls) == 1
        finally:
            release.set()
        state.snapshot_index_board_task.result(timeout=2)
        result = index_board.load_index_board("http://test", no_sync_request, factory=factory)
        assert result["items"][0]["name"] == "test-index"
        assert index_board.load_index_board("http://test", no_sync_request, factory=factory) == result
        assert len(calls) == 1
        assert calls[0].headers["Authorization"] == "Bearer original-token"


@pytest.mark.parametrize("refresh, age", [(False, 61), (True, 0), (True, 61)])
def test_index_refresh_retains_old_snapshot_and_labels_it(state, monkeypatch, refresh, age):
    future = Future()
    submitted = []
    monkeypatch.setattr(snapshots, "board_prefetch_executor", lambda: SimpleNamespace(
        submit=lambda *args: submitted.append(args) or future))
    old = {"status": "ok", "items": [{"name": "old"}]}
    state.update(snapshot_index_board_signature=("test", state.auth_token),
                 snapshot_index_board_value=old, snapshot_index_board_loaded=monotonic() - age)
    factory = lambda *a: lambda refresh=False: None
    result = index_board.load_index_board("test", no_sync_request, factory=factory, refresh=refresh)
    assert result["items"] == old["items"] and result["refreshing"] is True
    assert old.get("refreshing") is None
    assert index_board.load_index_board("test", no_sync_request, factory=factory)["refreshing"] is True
    assert len(submitted) == 1
    fresh = {"status": "partial", "items": [{"name": "new"}]}
    future.set_result(fresh)
    assert index_board.load_index_board("test", no_sync_request, factory=factory) == fresh


def test_logout_drops_pending_index_and_previous_sync_cache(state, monkeypatch):
    future = Future()
    future.set_running_or_notify_cancel()
    state.update(snapshot_index_board_task=future, snapshot_index_board_value={"items": ["old"]},
                 index_board_cache={"test": {"result": {"items": ["old"]}}})
    monkeypatch.setattr(ui, "current_browser_session_id", lambda: None)
    ui.reset_user_session()
    future.set_result({"status": "ok", "items": ["late"]})
    assert not any(key.startswith("snapshot_index_board") for key in state)
    assert "index_board_cache" not in state


def test_slow_index_endpoint_does_not_hold_chat_input(monkeypatch):
    started, release = Event(), Event()
    def transport(request):
        started.set()
        assert release.wait(10)
        return httpx.Response(200, json={"status": "ok", "items": []})
    script = """
import streamlit as st
from frontend.index_board import render_index_board
from frontend.api_client import backend_http_client, snapshot_fetch_factory
st.session_state.auth_token = 'test-token'
render_index_board('http://test', lambda *a, **kw: None,
    factory=snapshot_fetch_factory(backend_http_client('http://test'), 'test-token'))
st.chat_input('continue asking')
"""
    with ThreadPoolExecutor(max_workers=1) as pool, httpx.Client(transport=httpx.MockTransport(transport)) as client:
        from frontend import api_client
        monkeypatch.setattr(snapshots, "board_prefetch_executor", lambda: pool)
        monkeypatch.setattr(api_client, "backend_http_client", lambda base: client)
        try:
            app = AppTest.from_string(script).run(timeout=5)
            assert not app.exception
            assert started.wait(1)
            assert not release.is_set()
            assert len(app.chat_input) == 1
            assert any("后台加载" in item.value for item in app.caption)
        finally:
            release.set()
