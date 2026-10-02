"""微信开放平台网站应用扫码登录的短期握手状态。"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from urllib.parse import urlencode

import httpx


@dataclass
class PendingLogin:
    poll_token: str
    expires_at: float
    status: str = "pending"
    user_id: int | None = None
    error: str | None = None


class WechatLogin:
    """单进程短期状态；授权 state 与浏览器轮询凭据相互独立。"""

    def __init__(self, app_id: str, app_secret: str, callback_url: str) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.callback_url = callback_url
        self._pending: dict[str, PendingLogin] = {}
        self._lock = Lock()

    @classmethod
    def from_env(cls) -> "WechatLogin":
        return cls(os.getenv("WENCE_WECHAT_APP_ID", "").strip(),
                   os.getenv("WENCE_WECHAT_APP_SECRET", "").strip(),
                   os.getenv("WENCE_WECHAT_CALLBACK_URL", "").strip())

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret and self.callback_url.startswith("https://"))

    def start(self) -> dict[str, str]:
        if not self.configured:
            raise ValueError("微信登录尚未配置，请联系管理员")
        state = secrets.token_urlsafe(32)
        poll_token = secrets.token_urlsafe(32)
        with self._lock:
            self._expire_locked()
            if len(self._pending) >= 1000:
                raise RuntimeError("微信登录请求较多，请稍后重试")
            self._pending[state] = PendingLogin(poll_token, monotonic() + 300)
        params = urlencode({"appid": self.app_id, "redirect_uri": self.callback_url,
                            "response_type": "code", "scope": "snsapi_login", "state": state})
        return {"authorization_url": f"https://open.weixin.qq.com/connect/qrconnect?{params}#wechat_redirect",
                "poll_token": poll_token}

    def begin_callback(self, state: str) -> bool:
        with self._lock:
            self._expire_locked()
            pending = self._pending.get(state)
            if pending is None or pending.status != "pending":
                return False
            pending.status = "processing"
            return True

    def finish_callback(self, state: str, *, user_id: int | None = None, error: str | None = None) -> None:
        with self._lock:
            pending = self._pending.get(state)
            if pending is not None and pending.status == "processing":
                pending.user_id = user_id
                pending.error = error
                pending.status = "ready" if user_id is not None else "failed"

    def poll(self, poll_token: str) -> tuple[str, int | None, str | None]:
        with self._lock:
            self._expire_locked()
            for state, pending in self._pending.items():
                if secrets.compare_digest(pending.poll_token, poll_token):
                    if pending.status in {"ready", "failed"}:
                        del self._pending[state]
                    return pending.status, pending.user_id, pending.error
        return "expired", None, None

    def _expire_locked(self) -> None:
        now = monotonic()
        for state in [key for key, value in self._pending.items() if value.expires_at <= now]:
            del self._pending[state]

    def exchange_code(self, code: str) -> str:
        """code 仅由服务端换取 openid，微信令牌不会进入前端。"""
        response = httpx.get("https://api.weixin.qq.com/sns/oauth2/access_token",
                             params={"appid": self.app_id, "secret": self.app_secret,
                                     "code": code, "grant_type": "authorization_code"},
                             timeout=10.0)
        response.raise_for_status()
        payload = response.json()
        openid = payload.get("openid") if isinstance(payload, dict) else None
        if not isinstance(openid, str) or not openid or "snsapi_login" not in str(payload.get("scope", "")):
            raise ValueError("微信授权失败")
        return openid
