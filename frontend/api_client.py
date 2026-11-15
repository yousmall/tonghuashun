"""HTTP 传输层：捕获鉴权快照，后台线程不访问 Streamlit 会话。"""
from dataclasses import dataclass
from typing import Any
import json
from threading import Lock
from time import monotonic, sleep
import httpx
import streamlit as st
from backend.app.http_clients import verified_ssl_context


@st.cache_resource(show_spinner=False, max_entries=8)
def backend_http_client(api_base):
    return httpx.Client(limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
                        timeout=httpx.Timeout(30.0, connect=10.0), verify=verified_ssl_context())


@dataclass(frozen=True)
class ApiResult:
    data: Any = None
    status: int | None = None
    detail: Any = None
    retry_after: float | None = None


class AnalysisProgress:
    """提交线程与页面之间的进度快照；后台线程不接触 Streamlit 状态。"""
    def __init__(self):
        self._lock = Lock()
        self._stage = "正在提交问题"
        self._started = monotonic()

    def update(self, stage):
        with self._lock:
            self._stage = stage

    def snapshot(self):
        with self._lock:
            return self._stage, int(monotonic() - self._started)


def request_analysis(client, base, method, path, payload, token, progress):
    """Deep mode retries admission once; bounded mode and paid work never replay."""
    result = _request_analysis_once(client, base, method, path, payload, token, progress)
    if result.status == 429 and payload.get("research_mode") not in {"lite", "instant", "fast"}:
        progress.update("当前研究请求较多，正在等待重试")
        sleep(result.retry_after if result.retry_after is not None else 2)
        return _request_analysis_once(client, base, method, path, payload, token, progress)
    return result


def _retry_delay(value):
    try:
        delay = float(value)
        return delay if 0 <= delay <= 3 else 2
    except (TypeError, ValueError):
        return 2


def _request_analysis_once(client, base, method, path, payload, token, progress):
    """读取阶段流，完整闸门结束后才交付结果；断流不重复提交付费分析。"""
    try:
        with client.stream(method, f"{base}{path}/stream", json=payload,
                headers={"Authorization": f"Bearer {token}"} if token else {},
                timeout=httpx.Timeout(2.8, connect=0.5) if payload.get("research_mode") in {"lite", "instant"}
                else httpx.Timeout(None, connect=10) if payload.get("research_mode") == "deep"
                else httpx.Timeout(240, connect=10)) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get("type") == "progress" and isinstance(event.get("stage"), str):
                    progress.update(event["stage"])
                elif event.get("type") == "result" and isinstance(event.get("advice"), dict):
                    return ApiResult(data=event["advice"], status=response.status_code)
                elif event.get("type") == "error":
                    return ApiResult(status=event.get("status_code"),
                        detail=event.get("message") or "分析暂时无法完成，请稍后重试。",
                        retry_after=_retry_delay(event.get("retry_after")))
        return ApiResult(detail="分析连接已中断，尚未收到完整核验结果，请查看历史记录后再重试。")
    except httpx.HTTPStatusError as exc:
        exc.response.read()
        try:
            detail = exc.response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        return ApiResult(status=exc.response.status_code, detail=detail or "请求未成功，请稍后重试。",
                         retry_after=_retry_delay(exc.response.headers.get("Retry-After")))
    except (httpx.HTTPError, ValueError, AttributeError):
        return ApiResult(detail="分析服务暂时不可用，请稍后重试。")


def request_json(client, base, method, path, payload, token):
    timeout = (httpx.Timeout(None, connect=10) if path == "/portfolio/analyze" and payload.get("research_mode") == "deep"
               else httpx.Timeout(240 if path in {"/portfolio/analyze", "/data/watchlist-quotes"} else 65 if path in {"/profile/assess", "/data/compare"} else 30, connect=10))
    try:
        response = client.request(method, f"{base}{path}", json=payload,
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=timeout)
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
        return ApiResult(detail="分析服务暂时不可用，请稍后重试。")


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
