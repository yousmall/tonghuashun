"""账号认证、对话落库和用户隔离的端到端接口测试。"""

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import event

from backend.app import main as main_module
from backend.app.agents.coordinator import CoordinatorAgent, basic_compliance_check, verify_facts
from backend.app.agents.rule_agents import make_rule_agents
from backend.app.database import Database
from backend.app.session_pool import SessionThreadPool


def test_history_list_uses_one_query_for_last_messages() -> None:
    """会话数增长时，列表 SQL 次数必须保持常数而不是退化为 N+1。"""

    database = Database("sqlite+pysqlite:///:memory:", "test-secret-that-is-longer-than-thirty-two-characters")
    database.initialize()
    user = database.create_user("query-count-user", "hashed")
    for index in range(6):
        database.save_exchange(
            int(user["id"]),
            f"conversation-{index}",
            f"问题 {index}",
            {"query": f"问题 {index}"},
            f"回答 {index}",
            {"conclusion": f"回答 {index}"},
        )

    statements: list[str] = []
    assert database.engine is not None

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many) -> None:
        statements.append(statement)

    event.listen(database.engine, "before_cursor_execute", record_statement)
    try:
        histories = database.list_conversations(int(user["id"]), limit=6)
    finally:
        event.remove(database.engine, "before_cursor_execute", record_statement)

    selects = [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1
    assert {item["last_message"] for item in histories} == {f"回答 {index}" for index in range(6)}


def test_register_login_persist_history_and_isolate_users(monkeypatch, request, semantic) -> None:
    database = Database("sqlite+pysqlite:///:memory:", "test-secret-that-is-longer-than-thirty-two-characters")
    database.initialize()
    monkeypatch.setattr(main_module, "database", database)
    session_pool = SessionThreadPool()
    request.addfinalizer(session_pool.shutdown)
    monkeypatch.setattr(main_module, "session_thread_pool", session_pool)
    monkeypatch.setattr(
        main_module,
        "coordinator",
        CoordinatorAgent(
            agents=make_rule_agents(),
            verifier=verify_facts,
            compliance_checker=basic_compliance_check,
            semantic=semantic,
        ),
    )

    with TestClient(main_module.app) as client:
        registered = client.post(
            "/api/v1/auth/register", json={"username": "alice", "password": "strong-pass-1"}
        )
        assert registered.status_code == 200
        token = registered.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        assert client.post(
            "/api/v1/auth/login", json={"username": "ALICE", "password": "strong-pass-1"}
        ).status_code == 200
        assert client.post(
            "/api/v1/auth/login", json={"username": "alice", "password": "wrong-pass"}
        ).status_code == 401

        analyzed = client.post(
            "/api/v1/portfolio/analyze",
            headers=headers,
            json={
                "query": "请诊断我的持仓组合",
                "profile": {"user_id": "untrusted-client-value", "risk_level": "R3", "confirmed": True},
                "facts": [
                    {
                        "fact_id": "F-AUTH-1",
                        "entity": "示例ETF",
                        "field": "weight",
                        "value": 0.2,
                        "snapshot_time": datetime.now(timezone.utc).isoformat(),
                        "source_id": "TEST",
                        "quality": 0.9,
                    }
                ],
                "auto_fetch": False,
                "conversation_id": "conversation-alice",
            },
        )
        assert analyzed.status_code == 200

        histories = client.get("/api/v1/history", headers=headers)
        assert histories.status_code == 200
        assert histories.json()[0]["id"] == "conversation-alice"
        assert histories.json()[0]["message_count"] == 2

        detail = client.get("/api/v1/history/conversation-alice", headers=headers)
        assert detail.status_code == 200
        assert [message["role"] for message in detail.json()["messages"]] == ["user", "assistant"]
        assert detail.json()["messages"][1]["payload"]["trace_id"] == analyzed.json()["trace_id"]

        added = client.post(
            "/api/v1/watchlist",
            headers=headers,
            json={"target": "贵州茅台", "asset_type": "股票"},
        )
        assert added.status_code == 200
        watchlist_id = added.json()["id"]
        assert added.json()["target"] == "贵州茅台"
        assert "user_id" not in added.json()
        assert client.post(
            "/api/v1/watchlist",
            headers=headers,
            json={"target": "贵州茅台", "asset_type": "股票"},
        ).status_code == 409
        assert [item["target"] for item in client.get("/api/v1/watchlist", headers=headers).json()] == [
            "贵州茅台"
        ]

        bob = client.post(
            "/api/v1/auth/register", json={"username": "bob-user", "password": "strong-pass-2"}
        )
        bob_headers = {"Authorization": f"Bearer {bob.json()['access_token']}"}
        assert client.get("/api/v1/watchlist", headers=bob_headers).json() == []
        assert client.delete(f"/api/v1/watchlist/{watchlist_id}", headers=bob_headers).status_code == 404
        assert client.get("/api/v1/history/conversation-alice", headers=bob_headers).status_code == 404
        assert client.get("/api/v1/history").status_code == 401
        assert bob.json()["session_beacon_token"] != bob.json()["access_token"]
        assert client.post(
            "/api/v1/auth/logout/beacon",
            content=bob.json()["session_beacon_token"],
            headers={"Content-Type": "text/plain"},
        ).status_code == 204
        assert client.get("/api/v1/history", headers=bob_headers).status_code == 401
        assert client.delete(f"/api/v1/watchlist/{watchlist_id}", headers=headers).status_code == 200
        assert client.get("/api/v1/watchlist", headers=headers).json() == []
        assert client.post("/api/v1/auth/logout", headers=headers).status_code == 200
        assert client.get("/api/v1/history", headers=headers).status_code == 401
