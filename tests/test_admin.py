"""管理员权限、聚合口径、在线排序及真实问财尝试统计。"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from streamlit.testing.v1 import AppTest

from backend.app import main
from backend.app.auth import hash_password
from backend.app.database import ConsultationRow, ConversationRow, Database, IwencaiCallRow, MessageRow
from backend.app.data_provider import IwencaiSkillHubProvider
from backend.app.services.admin import consultation_metadata
from backend.app.services.call_tracking import calling_user_id
from backend.app.session_pool import SessionThreadPool


@pytest.fixture
def admin_app(monkeypatch):
    database = Database("sqlite+pysqlite:///:memory:", "test-secret-that-is-longer-than-thirty-two-characters")
    database.initialize()
    pool = SessionThreadPool()
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "session_thread_pool", pool)
    database.create_user("admin", hash_password("admin-test-pass"), admin=True)
    with TestClient(main.app) as client:
        login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-test-pass"})
        assert login.json()["user"]["role"] == "admin"
        headers = {"Authorization": "Bearer " + login.json()["access_token"]}
        yield client, database, pool, headers
    pool.shutdown()


def test_admin_permissions_cannot_be_selected_at_registration(admin_app):
    client, database, pool, headers = admin_app
    ordinary = client.post("/api/v1/auth/register", json={"username": "alice", "password": "ordinary-pass", "role": "admin"})
    assert ordinary.json()["user"]["role"] == "user"
    user_headers = {"Authorization": "Bearer " + ordinary.json()["access_token"]}
    for path in ["/admin/users", "/admin/statistics", "/admin/iwencai", "/admin/users/alice/consultations"]:
        assert client.get("/api/v1" + path).status_code == 401
        assert client.get("/api/v1" + path, headers=user_headers).status_code == 403
        assert client.get("/api/v1" + path, headers=headers).status_code == 200
    for path in ["/history", "/watchlist", "/profile"]:
        assert client.get("/api/v1" + path, headers=headers).status_code == 403
    blocked = client.post("/api/v1/data/fetch", headers=headers, json={"target": "贵州茅台", "kind": "quote"})
    assert blocked.status_code == 403
    assert client.get("/api/v1/auth/me", headers=headers).json()["role"] == "admin"
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/v1/admin/users", headers=headers).status_code == 401


def test_online_sort_pagination_logout_and_multiple_sessions(admin_app):
    client, database, pool, headers = admin_app
    alice = database.create_user("alice", "hashed")
    database.create_user("newer-offline", "hashed")
    first, _ = pool.allocate(alice["id"])
    second, _ = pool.allocate(alice["id"])
    response = client.get("/api/v1/admin/users?limit=1", headers=headers).json()
    assert response["total"] == 3
    assert response["items"][0]["username"] == "alice"
    assert response["items"][0]["online_sessions"] == 2
    assert response["items"][0]["online"] is True
    page = client.get("/api/v1/admin/users?offset=1&limit=2", headers=headers).json()["items"]
    assert [row["username"] for row in page] == ["admin", "newer-offline"]
    assert "password_hash" not in response["items"][0]
    pool.release(first)
    assert pool.online_users()[alice["id"]] == 1
    pool.release(second)
    assert alice["id"] not in pool.online_users()
    found = client.get("/api/v1/admin/users?q=newer", headers=headers).json()
    assert found["total"] == 1


def test_online_snapshot_expires_idle_without_touching_activity():
    clock = [0.0]
    pool = SessionThreadPool(clock=lambda: clock[0], idle_timeout_seconds=10)
    try:
        session, _ = pool.allocate(1)
        clock[0] = 9
        assert pool.online_users() == {1: 1}
        clock[0] = 10
        assert pool.online_users() == {}
        assert not pool.is_active(session)
    finally:
        pool.shutdown()


def test_consultation_totals_top_five_domains_legacy_and_period(admin_app):
    client, database, _, headers = admin_app
    alice = database.create_user("alice", "hashed")
    bob = database.create_user("bob", "hashed")
    for index in range(7):
        for repeat in range(index + 1):
            database.save_exchange(alice["id"], "chat", f"问题{index}",
                                   {"_analytics": consultation_metadata("security_research", f"标的{index}", "问题")},
                                   "回答", {})
    database.save_exchange(bob["id"], "fund-chat", "基金问题", {"_analytics": consultation_metadata("fund_screening", "基金甲", "问题")}, "回答", {})
    database.save_exchange(bob["id"], "old-chat", "旧问题", {}, "回答", {})
    stats = client.get("/api/v1/admin/statistics", headers=headers).json()
    assert stats["consultations"] == 30
    assert stats["consulting_users"] == 2
    stocks = next(row for row in stats["domains"] if row["domain"] == "security_research")
    assert stocks["count"] == 28
    assert [row["count"] for row in stocks["top"]] == [7, 6, 5, 4, 3]
    assert [row["topic"] for row in stocks["top"]] == ["标的6", "标的5", "标的4", "标的3", "标的2"]
    assert next(row for row in stats["domains"] if row["domain"] == "unknown")["count"] == 1
    detail = client.get("/api/v1/admin/users/alice/consultations", headers=headers).json()
    assert detail["consultations"] == 28
    assert len(detail["recent"]) == 28
    assert client.get("/api/v1/admin/users/missing/consultations", headers=headers).status_code == 404
    assert database.admin_statistics(since=datetime.now(timezone.utc) + timedelta(days=1))["consultations"] == 0


def test_legacy_backfill_is_idempotent_and_uses_compact_statistics():
    database = Database("sqlite+pysqlite:///:memory:", "test-secret-that-is-longer-than-thirty-two-characters")
    database.initialize()
    user = database.create_user("legacy-user", "hashed")
    with database.session_factory.begin() as session:
        session.add(ConversationRow(id="legacy", user_id=user["id"], title="旧会话"))
        session.flush()
        session.add(MessageRow(conversation_id="legacy", user_id=user["id"], role="user",
                               content="历史问题", payload={"facts": "x" * 1_000_000}))
    database.initialize()
    database.initialize()
    assert database.initialization_error is None
    with database.session_factory() as session:
        rows = session.scalars(select(ConsultationRow)).all()
        assert len(rows) == 1
        assert rows[0].topic == "历史问题"
        assert rows[0].domain == "unknown"
    assert database.admin_statistics()["consultations"] == 1


@pytest.mark.asyncio
async def test_iwencai_retries_empty_and_failures_are_persisted(monkeypatch):
    database = Database("sqlite+pysqlite:///:memory:", "test-secret-that-is-longer-than-thirty-two-characters")
    database.initialize()
    monkeypatch.setattr(main, "database", database)
    alice = database.create_user("alice", "hashed")
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(502, json={"error": "temporary"})
        return httpx.Response(200, json={"data": []})
    provider = IwencaiSkillHubProvider("test-key-not-a-secret", transport=httpx.MockTransport(handler), max_retries=1)
    provider.call_observer = main.record_iwencai_attempt
    token = calling_user_id.set(alice["id"])
    try:
        assert await provider.query("测试") == []
        stats = database.admin_iwencai_statistics()
        assert stats["total"] == 2
        assert stats["successful"] == stats["failed"] == stats["empty"] == stats["retries"] == 1
        assert stats["errors"][0]["status_code"] == 502
        assert stats["average_ms"] >= 0
        assert stats["by_interface"][0]["skill_id"] == "hithink-market-query"
        with database.session_factory() as session:
            rows = session.scalars(select(IwencaiCallRow)).all()
            assert all(row.user_id == alice["id"] for row in rows)
            assert all("test-key" not in str(row.__dict__) for row in rows)
    finally:
        calling_user_id.reset(token)
        await provider.aclose()


@pytest.mark.asyncio
async def test_invalid_json_records_failure_without_mutating_retry_payload():
    calls, events = [], []
    def handler(request):
        calls.append(request.content)
        return httpx.Response(200, content=b"invalid")
    provider = IwencaiSkillHubProvider("test-key", transport=httpx.MockTransport(handler), max_retries=1)
    async def observe(event):
        events.append(event)
    provider.call_observer = observe
    try:
        with pytest.raises(RuntimeError):
            await provider.query("test")
        assert len(events) == 2
        assert calls[0] == calls[1]
        assert all(event["status"] == "failed" for event in events)
        provider._circuit_open_until = datetime.now(timezone.utc) + timedelta(seconds=20)
        with pytest.raises(RuntimeError, match="熔断"):
            await provider.query("test")
        assert len(events) == 2
    finally:
        await provider.aclose()


def test_analysis_and_stream_save_server_derived_consultation_metadata(admin_app, risk_questionnaire_payload):
    client, database, _, admin_headers = admin_app
    registered = client.post("/api/v1/auth/register", json={"username": "alice", "password": "ordinary-pass"}).json()
    headers = {"Authorization": "Bearer " + registered["access_token"]}
    profile = client.post("/api/v1/profile/confirm", headers=headers, json={"profile": risk_questionnaire_payload}).json()
    payload = {"query": "请诊断我的持仓组合", "profile": {"version": profile["version"], "confirmed": True},
               "auto_fetch": False, "conversation_id": "analytics-chat",
               "_analytics": {"domain": "fund_screening", "topic": "forged-topic"}}
    assert client.post("/api/v1/portfolio/analyze", headers=headers, json=payload).status_code == 200
    streamed = client.post("/api/v1/portfolio/analyze/stream", headers=headers, json=payload)
    assert streamed.status_code == 200
    assert '"type": "result"' in streamed.text
    stats = client.get("/api/v1/admin/statistics", headers=admin_headers).json()
    assert stats["consultations"] == 2
    portfolio = next(row for row in stats["domains"] if row["domain"] == "portfolio_review")
    assert portfolio["count"] == 2
    assert portfolio["top"][0]["topic"] == "请诊断我的持仓组合"
    assert portfolio["top"][0]["count"] == 2
    assert all(row["count"] == 0 for row in stats["domains"] if row["domain"] != "portfolio_review")


def test_iwencai_api_tracks_authenticated_and_anonymous_user_without_leak(admin_app, monkeypatch):
    client, database, _, admin_headers = admin_app
    registered = client.post("/api/v1/auth/register", json={"username": "alice", "password": "ordinary-pass"}).json()
    provider = IwencaiSkillHubProvider("test-key", transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": []})))
    provider.call_observer = main.record_iwencai_attempt
    monkeypatch.setattr(main, "data_provider", provider)
    headers = {"Authorization": "Bearer " + registered["access_token"]}
    for auth in [headers, {}]:
        assert client.post("/api/v1/data/fetch", headers=auth, json={"kind": "quote", "target": "示例"}).status_code == 200
    with database.session_factory() as session:
        rows = session.scalars(select(IwencaiCallRow).order_by(IwencaiCallRow.id)).all()
        assert [row.user_id for row in rows] == [registered["user"]["id"], None]
    stats = client.get("/api/v1/admin/iwencai", headers=admin_headers).json()
    assert stats["total"] == stats["successful"] == stats["empty"] == 2


def test_admin_frontend_routing_has_no_customer_features():
    script = """
from unittest.mock import patch
import streamlit as st
from frontend import streamlit_app as ui
ui.init_session()
ui.complete_login({'access_token': 'admin-token', 'session_beacon_token': 'beacon',
                   'user': {'username': 'admin', 'role': 'admin'}})
paths = []
def fake_api(base, method, path, payload=None, **kwargs):
    paths.append(path)
    if path.startswith('/admin/users?'):
        return {'total': 1, 'items': [{'username': 'admin', 'role': 'admin', 'online': True,
                'online_sessions': 1, 'consultations': 0, 'conversations': 0,
                'created_at': '2026-09-26T00:00:00Z', 'last_consultation': None}]}
    if path.startswith('/admin/statistics'):
        return {'consultations': 0, 'online_users': 1, 'online_idle_seconds': 600,
                'consulting_users': 0, 'domains': [], 'daily': []}
    if path.startswith('/admin/iwencai'):
        return {'total': 0, 'successful': 0, 'failed': 0, 'empty': 0, 'retries': 0,
                'facts': 0, 'average_ms': 0, 'by_interface': [], 'daily': [], 'errors': []}
with patch.object(ui, 'api_request', side_effect=fake_api), patch.object(ui, 'register_browser_session'), patch.object(ui, 'enforce_session_timeout'):
    ui.main()
st.session_state.paths = paths
"""
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    assert app.radio[0].options == ['注册用户', '咨询统计', '问财调用']
    assert not app.chat_input
    assert '/profile' not in app.session_state['paths']
    assert '/watchlist' not in app.session_state['paths']
    assert not any(button.label == '发起咨询' for button in app.button)
    app.radio[0].set_value('问财调用').run(timeout=15)
    assert not app.exception
    assert any('暂无问财调用记录' in item.value for item in app.info)
