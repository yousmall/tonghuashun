"""HTTP 传输层：捕获鉴权快照，后台线程不访问 Streamlit 会话。"""
from dataclasses import dataclass
from typing import Any
import httpx
import streamlit as st


@st.cache_resource(show_spinner=False, max_entries=8)
def backend_http_client(api_base):
    return httpx.Client(limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
                        timeout=httpx.Timeout(30.0, connect=10.0))


@dataclass(frozen=True)
class ApiResult:
    data: Any = None
    status: int | None = None
    detail: Any = None


def request_json(client, base, method, path, payload, token):
    timeout = 240 if path == "/portfolio/analyze" else 65 if path in {"/profile/assess", "/data/compare"} else 30
    try:
        response = client.request(method, f"{base}{path}", json=payload,
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=httpx.Timeout(timeout, connect=10))
        response.raise_for_status()
        return ApiResult(data=response.json(), status=response.status_code)
    except httpx.HTTPStatusError as exc:
        try:
            body = exc.response.json()
            detail = body.get("detail") if isinstance(body, dict) else None
        except ValueError:
            detail = None
        return ApiResult(status=exc.response.status_code, detail=detail or "请求未成功，请稍后重试。")
    except (httpx.HTTPError, ValueError):
        return ApiResult(detail="暂时连接不上分析服务，请稍后再试。")


def overview_fetch(client, token):
    def fetch(base, direction, target, *, refresh=False):
        return request_json(client, base, "POST", "/data/overview",
            {"direction": direction, "target": target, "wait": False, "refresh": refresh}, token).data
    fetch.supports_refresh = True
    return fetch


def snapshot_fetch_factory(client, token):
    def factory(base, path, payload):
        captured = dict(payload)
        def fetch(refresh=False):
            return request_json(client, base, "POST", path, {**captured, "refresh": refresh}, token).data
        return fetch
    return factory
