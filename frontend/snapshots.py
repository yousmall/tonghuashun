"""有界后台任务与会话内快照；工作线程不写入用户状态。"""
from time import monotonic
import streamlit as st
from frontend.research_board import board_prefetch_executor


def clear_snapshot(name):
    prefix = "snapshot_" + name
    task = st.session_state.pop(prefix + "_task", None)
    if task:
        task.cancel()
    for suffix in ["_signature", "_value", "_loaded"]:
        st.session_state.pop(prefix + suffix, None)


def poll_snapshot(name, signature, fetch, *, refresh=False):
    prefix = "snapshot_" + name
    if st.session_state.get(prefix + "_signature") != signature:
        clear_snapshot(name)
        st.session_state[prefix + "_signature"] = signature
    value = st.session_state.get(prefix + "_value")
    task = st.session_state.get(prefix + "_task")
    if refresh:
        if task:
            task.cancel()
        task = None
    ttl = 0.8 if isinstance(value, dict) and value.get("status") == "loading" else 60 if isinstance(value, dict) and value.get("status") in {"ok", "partial"} else 30
    if not refresh and value is not None and monotonic() - st.session_state.get(prefix + "_loaded", 0) < ttl:
        return value
    if task is None:
        task = board_prefetch_executor().submit(fetch, refresh)
        st.session_state[prefix + "_task"] = task
    if not task.done():
        return {**value, "refreshing": True} if isinstance(value, dict) else {"status": "loading", "items": []}
    st.session_state.pop(prefix + "_task", None)
    try:
        value = task.result()
    except Exception:
        value = None
    if not isinstance(value, dict):
        value = {"status": "unavailable", "items": []}
    st.session_state[prefix + "_value"], st.session_state[prefix + "_loaded"] = value, monotonic()
    return value
