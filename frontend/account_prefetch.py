"""登录资料并行预取：后台只返回结果，主线程应用当前账号的状态。"""
import httpx
import streamlit as st

from frontend.api_client import request_json
from frontend import research_board


PENDING = object()
NOT_PREFETCHED = object()


def cancel_account_prefetch() -> None:
    for future in st.session_state.pop("account_prefetch", {}).values():
        future.cancel()
    st.session_state.pop("account_prefetch_results", None)


def discard_account_result(api_base: str, path: str) -> None:
    """写入成功后丢弃旧读取，正在运行的任务也不能恢复旧列表。"""
    key = (api_base, path)
    future = st.session_state.get("account_prefetch", {}).pop(key, None)
    if future is not None:
        future.cancel()
    st.session_state.get("account_prefetch_results", {}).pop(key, None)


def start_account_prefetch(api_base: str, token: str | None, client: httpx.Client) -> None:
    cancel_account_prefetch()
    executor = research_board.board_prefetch_executor()
    st.session_state.account_prefetch = {
        (api_base, path): executor.submit(request_json, client, api_base, "GET", path, None, token)
        for path in ("/profile", "/history?limit=20", "/watchlist")
    }


def collect_account_prefetch() -> bool:
    pending = st.session_state.get("account_prefetch", {})
    completed = st.session_state.setdefault("account_prefetch_results", {})
    changed = False
    for key, future in list(pending.items()):
        if future.done():
            pending.pop(key)
            try:
                completed[key] = future.result().data
            except Exception:
                completed[key] = None
            changed = True
    return changed


def take_account_result(api_base: str, path: str):
    collect_account_prefetch()
    key = (api_base, path)
    if key in st.session_state.get("account_prefetch", {}):
        return PENDING
    return st.session_state.get("account_prefetch_results", {}).pop(key, NOT_PREFETCHED)


@st.fragment(run_every=1)
def poll_account_prefetch() -> None:
    if collect_account_prefetch():
        st.rerun()
