"""登录预取：非阻塞、鉴权快照、页面复用与账号隔离。"""
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event
from time import monotonic

import httpx
import pytest
import streamlit as st

from frontend import research_board as board
from frontend import streamlit_app as ui

BASE = "http://test-backend/api/v1"


def result(direction, target=None):
    return {"direction": direction, "target": target, "status": "ok", "sections": [], "fetched_at": None}


def login(token="customer-token", role="user"):
    return {"access_token": token, "session_beacon_token": "test-beacon",
            "user": {"username": "test-user", "role": role}}


class SessionState(dict):
    def __getattr__(self, key):
        return self[key]

    def __setattr__(self, key, value):
        self[key] = value


@pytest.fixture
def state(monkeypatch):
    values = SessionState(api_base=BASE)
    monkeypatch.setattr(st, "session_state", values)
    monkeypatch.setattr(ui, "current_browser_session_id", lambda: None)
    return values


def test_login_starts_all_default_boards_without_waiting_and_pages_reuse_them(state, monkeypatch):
    gate = Event()
    requests = []

    def transport(request):
        requests.append(request)
        assert gate.wait(5), "Login must return before data loading completes"
        import json
        data = json.loads(request.content)
        return httpx.Response(200, json=result(data["direction"], data["target"]))

    with httpx.Client(transport=httpx.MockTransport(transport)) as client, ThreadPoolExecutor(max_workers=5) as executor:
        monkeypatch.setattr(ui, "backend_http_client", lambda base: client)
        monkeypatch.setattr(board, "board_prefetch_executor", lambda: executor)
        try:
            ui.complete_login(login())
            pending = state["research_board_prefetch"]
            assert state["navigation"] == "主页"
            assert len(pending) == 5
            assert all(not future.done() for future in pending.values())
            # Worker authorization must keep the login snapshot, not read current session state.
            state["auth_token"] = "another-session-token"
        finally:
            gate.set()
        for base, direction, target in list(pending):
            data = board.load_board(base, direction, target, lambda *args: pytest.fail("Duplicate page fetch"))
            assert data["status"] == "ok"
        assert len(requests) == 5
        assert all(request.headers["Authorization"] == "Bearer customer-token" for request in requests)
        import json
        assert {(json.loads(request.content)["direction"], json.loads(request.content)["target"]) for request in requests} == {
            ("market", None), ("industry", None), ("stock", "600519"), ("fund", None), ("convertible", None)}
        assert state["research_board_prefetch"] == {}
        assert not state.get("facts") and not state["conversation"]
        board.load_board(BASE, "market", None, lambda *args: pytest.fail("Cached page fetched again"))


@pytest.mark.parametrize("refresh,age", [(True, 0), (False, 301)])
def test_refresh_or_expired_prefetch_fetches_current_data(state, refresh, age):
    stale = Future()
    old = result("market")
    old["fetched_at"] = "old"
    stale.set_result((monotonic() - age, old))
    key = (BASE, "market", None)
    state["research_board_prefetch"] = {key: stale}
    current = result("market")
    current["fetched_at"] = "current"
    calls = []
    loaded = board.load_board(BASE, "market", None, lambda *args: calls.append(args) or current, refresh=refresh)
    assert loaded["fetched_at"] == "current"
    assert len(calls) == 1
    assert not state["research_board_prefetch"]


def test_failed_prefetch_is_unavailable_and_manual_refresh_recovers(state, monkeypatch):
    def transport(request):
        raise httpx.ConnectError("test-only unavailable", request=request)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client, ThreadPoolExecutor(max_workers=2) as executor:
        monkeypatch.setattr(ui, "backend_http_client", lambda base: client)
        monkeypatch.setattr(board, "board_prefetch_executor", lambda: executor)
        ui.complete_login(login())
        loaded = board.load_board(BASE, "market", None, lambda *args: pytest.fail("Failed prefetch was duplicated"))
        assert loaded["status"] == "unavailable" and loaded["sections"] == []
        loaded = board.load_board(BASE, "market", None, lambda *args: result("market"), refresh=True)
        assert loaded["status"] == "ok"


def test_logout_cancels_queued_tasks_and_late_results_cannot_restore_old_cache(state):
    queued, running = Future(), Future()
    running.set_running_or_notify_cancel()
    state["research_board_prefetch"] = {(BASE, "market", None): queued, (BASE, "fund", None): running}
    state["research_board_cache"] = {(BASE, "stock", "600519"): {"result": result("stock")}}
    ui.reset_user_session()
    assert queued.cancelled()
    running.set_result((monotonic(), result("fund")))
    assert "research_board_prefetch" not in state and "research_board_cache" not in state


def test_admin_login_cancels_previous_prefetch_and_does_not_fetch_customer_data(state, monkeypatch):
    queued = Future()
    state["research_board_prefetch"] = {(BASE, "market", None): queued}
    state["research_board_cache"] = {"previous": {}}
    monkeypatch.setattr(ui, "prefetch_research_board_data", lambda base: pytest.fail("Admin fetched customer data"))
    ui.complete_login(login(role="admin"))
    assert queued.cancelled()
    assert "research_board_prefetch" not in state and "research_board_cache" not in state


def test_login_form_prefetches_before_user_opens_investment_questions(monkeypatch):
    from streamlit.testing.v1 import AppTest
    import json

    requests = []

    def transport(request):
        data = json.loads(request.content)
        requests.append(data)
        return httpx.Response(200, json=result(data["direction"], data["target"]))

    script = """
from unittest.mock import patch
from frontend import streamlit_app as ui

def fake_api(base, method, path, payload=None, **kwargs):
    if path == '/auth/login':
        return {'access_token':'test-login-token', 'session_beacon_token':'test-beacon',
                'user':{'username':'test-user','role':'user'}}
    return None

with patch.object(ui, 'api_request', side_effect=fake_api), patch.object(ui, 'register_browser_session'), patch.object(ui, 'enforce_session_timeout'):
    ui.main()
"""
    with httpx.Client(transport=httpx.MockTransport(transport)) as client, ThreadPoolExecutor(max_workers=5) as executor:
        monkeypatch.setattr(ui, "backend_http_client", lambda base: client)
        monkeypatch.setattr(board, "board_prefetch_executor", lambda: executor)
        app = AppTest.from_string(script).run(timeout=15)
        assert not app.exception and not requests
        app.text_input(key="login_username").input("test-user")
        app.text_input(key="login_password").input("test-password")
        next(button for button in app.button if button.label == "登录").click().run(timeout=15)
        assert not app.exception
        assert app.session_state["navigation"] == "主页"
        for future in app.session_state["research_board_prefetch"].values():
            future.result(timeout=5)
        assert len(requests) == 5
        next(button for button in app.button if button.label == "投资问答").click().run(timeout=15)
        assert not app.exception
        assert len(requests) == 5
        assert app.session_state["facts"] == []
