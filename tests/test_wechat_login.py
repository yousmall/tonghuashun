"""微信扫码授权的状态隔离、账号复用与会话签发。"""

from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from backend.app import main as main_module
from backend.app.database import Database
from backend.app.session_pool import SessionThreadPool
from backend.app.wechat_login import WechatLogin


def test_wechat_scan_creates_and_reuses_customer_account(monkeypatch, request) -> None:
    database = Database("sqlite+pysqlite:///:memory:", "test-secret-that-is-longer-than-thirty-two-characters")
    database.initialize()
    database.create_user("administrator", "hashed", admin=True)
    monkeypatch.setattr(main_module, "database", database)
    pool = SessionThreadPool()
    request.addfinalizer(pool.shutdown)
    monkeypatch.setattr(main_module, "session_thread_pool", pool)
    login = WechatLogin("test-app", "test-secret", "https://example.com/api/v1/auth/wechat/callback")
    monkeypatch.setattr(login, "exchange_code", lambda code: "same-wechat-openid")
    monkeypatch.setattr(main_module, "wechat_login", login)

    with TestClient(main_module.app) as client:
        start = client.post("/api/v1/auth/wechat/start")
        assert start.status_code == 200
        data = start.json()
        params = parse_qs(urlparse(data["authorization_url"]).query)
        assert params["scope"] == ["snsapi_login"]
        assert params["redirect_uri"] == ["https://example.com/api/v1/auth/wechat/callback"]
        state = params["state"][0]
        assert state != data["poll_token"]
        assert client.post("/api/v1/auth/wechat/poll", json={"poll_token": data["poll_token"]}).json() == {"status": "pending"}
        assert client.get("/api/v1/auth/wechat/callback", params={"state": "invalid", "code": "ok"}).status_code == 400
        assert client.post("/api/v1/auth/wechat/poll", json={"poll_token": state}).status_code == 410

        callback = client.get("/api/v1/auth/wechat/callback", params={"state": state, "code": "ok"})
        assert callback.status_code == 200
        assert "access_token" not in callback.text
        assert client.get("/api/v1/auth/wechat/callback", params={"state": state, "code": "ok"}).status_code == 400
        claimed = client.post("/api/v1/auth/wechat/poll", json={"poll_token": data["poll_token"]})
        assert claimed.status_code == 200
        auth = claimed.json()["auth"]
        assert auth["user"]["role"] == "user"
        assert auth["user"]["username"].startswith("wx_")
        headers = {"Authorization": f"Bearer {auth['access_token']}"}
        assert client.get("/api/v1/auth/me", headers=headers).json()["id"] == auth["user"]["id"]
        assert client.post("/api/v1/auth/wechat/poll", json={"poll_token": data["poll_token"]}).status_code == 410

        second = client.post("/api/v1/auth/wechat/start").json()
        second_state = parse_qs(urlparse(second["authorization_url"]).query)["state"][0]
        assert client.get("/api/v1/auth/wechat/callback", params={"state": second_state, "code": "ok"}).status_code == 200
        second_auth = client.post("/api/v1/auth/wechat/poll", json={"poll_token": second["poll_token"]}).json()["auth"]
        assert second_auth["user"]["id"] == auth["user"]["id"]


def test_wechat_cancel_and_unconfigured(monkeypatch) -> None:
    database = Database("sqlite+pysqlite:///:memory:", "test-secret-that-is-longer-than-thirty-two-characters")
    database.initialize()
    monkeypatch.setattr(main_module, "database", database)
    login = WechatLogin("id", "secret", "https://example.com/api/v1/auth/wechat/callback")
    monkeypatch.setattr(main_module, "wechat_login", login)
    with TestClient(main_module.app) as client:
        start = client.post("/api/v1/auth/wechat/start")
        assert start.status_code == 200
        data = start.json()
        state = parse_qs(urlparse(data["authorization_url"]).query)["state"][0]
        assert client.get("/api/v1/auth/wechat/callback", params={"state": state}).status_code == 200
        failed = client.post("/api/v1/auth/wechat/poll", json={"poll_token": data["poll_token"]})
        assert failed.json()["status"] == "failed"
        assert "auth" not in failed.json()

        monkeypatch.setattr(main_module, "wechat_login", WechatLogin("", "", ""))
        assert client.post("/api/v1/auth/wechat/start").status_code == 503

    assert not WechatLogin("", "", "").configured
