"""问策智投 Streamlit 前端。

本页面刻意不承担投资判断、数据抓取或合规决策：所有业务判断都通过 FastAPI
提交给后端。前端负责收集用户画像和可选事实快照，并把后端自动取数后返回的
``AdvicePackage`` 以可追溯、可解释的方式展示出来。

启动方式（先启动 backend，再在另一个终端执行）：

    streamlit run frontend/streamlit_app.py
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from html import escape
from ipaddress import ip_address
from pathlib import Path
from threading import Lock, Thread
from time import monotonic, sleep
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import streamlit as st
from frontend.risk_assessment import render_risk_assessment
from backend.app.risk_questionnaire import assessment_is_current
from frontend.result_views import render_advice, cached_answer_images, answer_export_payload, show_status
from frontend.api_client import backend_http_client, request_json, overview_fetch, snapshot_fetch_factory
from frontend.account_prefetch import (
    PENDING, NOT_PREFETCHED, cancel_account_prefetch, start_account_prefetch,
    take_account_result, poll_account_prefetch, discard_account_result,
)
from frontend.research_board import cancel_board_prefetch, render_research_board, restore_board_scroll, start_board_prefetch
from streamlit.runtime import get_instance
from streamlit.runtime.scriptrunner import get_script_run_ctx


from frontend.presentation import (
    display_value, fact_label, fact_source, fact_time, fact_period, friendly_fact_rows, advice_facts, _status_code, analysis_chain_stages, source_trace_rows, _facts_used_by_result, render_chain_overview, render_conclusion_panel, _render_conclusion_content, render_logic_chain, render_source_trace, render_profile_summary, plain_language, profile_evidence_language, render_points, render_empty_state, RISK_LEVELS, PROFILE_LABELS, FIELD_LABELS, NAVIGATION, DATA_KINDS, QUICK_ASKS, QUICK_ASK_PROMPTS, RESEARCH_PREFIX, WATCHLIST_RESEARCH_PREFIX, TOPIC_LABELS, PROGRESS_LABELS
)

DEFAULT_API_BASE = os.getenv("WENCE_API_BASE", "http://127.0.0.1:8000/api/v1")
BRAND_LOGO = Path(__file__).resolve().parent / "assets" / "brand-logo.png"


@st.cache_data(show_spinner=False)
def brand_logo_uri(asset_version: int) -> str:
    """为现有品牌行提供随项目发布的 Logo。"""

    return "data:image/png;base64," + base64.b64encode(BRAND_LOGO.read_bytes()).decode("ascii")


@st.cache_data(show_spinner=False)
def app_styles(asset_version: int) -> str:
    """读取随项目保存的样式，不依赖临时路径或外部资源。"""

    return (Path(__file__).resolve().parent / "assets" / "app.css").read_text(encoding="utf-8")


def render_app_styles() -> None:
    css_file = Path(__file__).resolve().parent / "assets" / "app.css"
    st.html(f"<style>{app_styles(css_file.stat().st_mtime_ns)}</style>")


def render_login_brand() -> None:
    """登录门禁的品牌提示；登录后的主内容区只保留侧栏品牌。"""

    st.html(f"""<div class="brand">{brand_lockup_html()}
        <div class="brand-note">让每一次投资，多一分理解</div></div>""")


def brand_lockup_html() -> str:
    """复用同一张品牌图，分别显示图形和字标，以便独立调整比例。"""

    logo_uri = brand_logo_uri(BRAND_LOGO.stat().st_mtime_ns)
    return f"""<div class="brand-lockup" role="img" aria-label="问策智投 Logo"
        style="--brand-image: url('{logo_uri}')">
        <span class="brand-symbol" aria-hidden="true"></span>
        <span class="brand-wordmark" aria-hidden="true"></span>
    </div>"""


def render_brand_block() -> None:
    """侧栏品牌区：与主内容之间用一条细线分隔。"""

    st.html(f"""<div class="side-brand">{brand_lockup_html()}</div>""")


def render_side_label(text: str) -> None:
    st.html(f"<div class='side-label'>{escape(text)}</div>")


def render_page_header(caption: str) -> None:
    """页面顶部条：位置提示 + 数据状态，让每一页都有终端式定位信息。"""

    with st.container(horizontal=True, vertical_alignment="center"):
        st.markdown(f"<span class='crumb'>{escape(caption)}</span>", unsafe_allow_html=True)
        st.space("stretch")
        count = len(st.session_state.get("facts", []))
        st.badge(
            f"{count} 条资料" if count else "暂无资料",
            icon=":material/database:" if count else ":material/database_off:",
            color="primary" if count else "gray",
        )
        st.badge(
            "偏好已确认" if profile_ready() else "偏好未确认",
            icon=":material/verified:" if profile_ready() else ":material/pending:",
            color="green" if profile_ready() else "orange",
        )


def render_stat_cards(cards: list[tuple[str, str, str]], *, accent_first: bool = False) -> None:
    """终端式数据块；cards 为 (标题, 数值, 注释) 三元组。"""

    if not cards:
        return
    blocks = "".join(
        f"<div class='stat-card{' accent' if accent_first and index == 0 else ''}'>"
        f"<div class='k'>{escape(label)}</div><div class='v'>{escape(value)}</div>"
        f"<div class='n'>{escape(note)}</div></div>"
        for index, (label, value, note) in enumerate(cards)
    )
    st.html(f"<div class='stat-grid'>{blocks}</div>")


def toggle_preference_navigation() -> None:
    """父入口只控制子菜单的展开状态，不改变当前页面。"""
    st.session_state.preference_navigation_open = not st.session_state.get("preference_navigation_open", False)


def page_navigation() -> str:
    """侧栏主入口与投资偏好子菜单；页面状态仍由 navigation 保存。"""
    st.session_state.setdefault("navigation", "主页")
    current = st.session_state.navigation
    if current == "投资偏好":  # 兼容旧浏览器会话
        current = "风险评估"
        st.session_state.navigation = current
    st.session_state.setdefault("preference_navigation_open", current in {"风险评估", "风险调整"})
    with st.container(key="nav"):
        st.button("主页", key="nav_home", width="stretch",
                  type="primary" if current == "主页" else "tertiary",
                  on_click=go_to, args=("主页",))
        st.button("投资偏好", key="nav_preference", width="stretch",
                  icon=":material/expand_less:" if st.session_state.preference_navigation_open else ":material/expand_more:",
                  on_click=toggle_preference_navigation)
        if st.session_state.preference_navigation_open:
            with st.container(key="nav-preference-children"):
                for page, key in (("风险评估", "nav_assessment"), ("风险调整", "nav_adjustment")):
                    st.button(page, key=key, width="stretch",
                              type="primary" if current == page else "tertiary",
                              on_click=go_to, args=(page,))
        for page, key in (("投资问答", "nav_questions"), ("自选研究", "nav_watchlist"),
                          ("持仓分析", "nav_portfolio")):
            st.button(page, key=key, width="stretch",
                      type="primary" if current == page else "tertiary",
                      on_click=go_to, args=(page,))
    return str(st.session_state.get("navigation", current))


def render_side_user(api_base: str) -> None:
    """侧栏账号卡：只展示账号名与偏好状态，不暴露任何后端标识。"""

    user = st.session_state.auth_user or {}
    account_name = str(user.get("username", ""))
    username = "微信用户" if re.fullmatch(r"wx_[0-9a-f]{40}", account_name) else escape(account_name)
    ready = profile_ready()
    state = "投资偏好已确认" if ready else "待确认投资偏好"
    dot = "dot" if ready else "dot pending"
    st.html(
        f"<div class='side-user'><div class='who'>{username}</div>"
        f"<div class='state'><span class='{dot}'></span>{state}</div></div>"
    )


def invalidate_recent_conversations(api_base: str | None = None) -> None:
    base = api_base or st.session_state.get("api_base")
    if base:
        discard_account_result(base, "/history?limit=20")
    st.session_state.recent_conversations_loaded_at = 0.0


def recent_conversations(api_base: str) -> list[dict[str, Any]] | None:
    """按浏览器会话短暂缓存当前账号的最近咨询，避免每次控件重跑都查库。"""

    now = monotonic()
    loaded_at = st.session_state.get("recent_conversations_loaded_at", 0.0)
    if now - loaded_at < 30 and "recent_conversations" in st.session_state:
        return st.session_state.recent_conversations
    result = take_account_result(api_base, "/history?limit=20")
    if result is PENDING:
        return st.session_state.get("recent_conversations")
    if result is NOT_PREFETCHED:
        result = api_request(api_base, "GET", "/history?limit=20", quiet=True)
    if isinstance(result, list):
        st.session_state.recent_conversations = result
        st.session_state.recent_conversations_loaded_at = monotonic()
        return result
    st.session_state.setdefault("recent_conversations", None)
    st.session_state.recent_conversations_loaded_at = monotonic()
    return st.session_state.get("recent_conversations")


def restore_conversation(detail: dict[str, Any]) -> None:
    """恢复选中聊天及其最近分析，让后续提问沿用原会话上下文。"""

    direction = st.session_state.get("conversation_directions", {}).get(detail["id"])
    if direction not in QUICK_ASKS:
        first_question = next((message["content"] for message in detail["messages"] if message["role"] == "user"), "")
        direction = next((kind for kind, prefix in RESEARCH_PREFIX.items() if first_question.startswith(prefix)), QUICK_ASKS[0])
    activate_research(direction)
    st.session_state.research_direction = direction
    st.session_state.conversation_id = detail["id"]
    st.session_state.history_before_id = detail.get("next_before_id")
    st.session_state.conversation = [
        {"role": message["role"], "content": message["content"], "created_at": message["created_at"],
         "payload": message.get("payload")}
        for message in detail["messages"]
    ]
    assistant_messages = [message for message in st.session_state.conversation if message["role"] == "assistant"]
    st.session_state.advice = assistant_messages[-1].get("payload") if assistant_messages else None
    if st.session_state.advice and "facts" in st.session_state.advice:
        st.session_state.facts = [dict(fact) for fact in st.session_state.advice["facts"]]
    save_research_session()
    st.session_state.pending_navigation = "投资问答"
    st.session_state.history_view = False


def render_recent_chats(api_base: str) -> None:
    with st.expander("最近咨询", expanded=True, key="recent-chats", type="compact",
                     icon=":material/history:", on_change="rerun") as section:
        if not section.open:
            return
        histories = recent_conversations(api_base)
        if histories is None:
            st.caption("正在加载咨询记录。" if st.session_state.get("account_prefetch")
                       else "咨询记录暂不可用，请稍后重试。")
            return
        if not histories:
            st.caption("暂无咨询记录。")
            return
        with st.container(height=360, border=False, key="recent-chat-list"):
            for item in histories:
                conversation_id = item.get("id")
                if not conversation_id:
                    continue
                title = " ".join(str(item.get("title") or "新对话").split())
                label = title
                active = conversation_id == st.session_state.conversation_id
                if st.button(label, key=f"recent_chat_{conversation_id}", help=title,
                             icon=":material/chat_bubble_outline:", width="stretch",
                             type="primary" if active else "tertiary"):
                    detail = api_request(api_base, "GET", f"/history/{conversation_id}")
                    if isinstance(detail, dict) and detail.get("id") == conversation_id:
                        restore_conversation(detail)
                        st.rerun()
        if st.button("查看全部咨询", key="all_recent_chats", icon=":material/search:", width="stretch"):
            st.session_state.history_view = True
            st.rerun()


def render_sidebar(api_base: str, *, full: bool) -> str | None:
    """构建侧栏；``full=False`` 时只保留品牌与退出入口，用于后端不可达的兜底界面。"""

    with st.sidebar:
        render_brand_block()
        if not full:
            if st.button("退出登录", width="stretch", icon=":material/logout:"):
                reset_user_session()
                st.rerun()
            if st.button("重试连接", width="stretch", icon=":material/refresh:"):
                st.rerun()
            return None
        st.button("发起咨询", width="stretch", type="primary", on_click=new_conversation,
                  icon=":material/add_comment:")
        render_side_label("投资")
        page = page_navigation()
        background_analysis_status()
        render_recent_chats(api_base)
        render_side_user(api_base)
        if st.button("退出登录", width="stretch", icon=":material/logout:"):
            api_request(api_base, "POST", "/auth/logout")
            reset_user_session()
            st.rerun()
    return page


def loopback_api_base(api_base: str) -> str | None:
    """只接受无用户信息的本机 HTTP API 地址。"""

    parsed = urlparse(api_base)
    host = parsed.hostname
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        return None
    try:
        is_loopback = ip_address(host).is_loopback
    except ValueError:
        is_loopback = host.lower() == "localhost"
    return api_base.rstrip("/") if is_loopback else None


@dataclass(slots=True)
class BrowserSessionRegistration:
    """Streamlit 浏览器连接与本机后端回收凭据的服务端映射。"""

    api_base: str
    beacon_token: str
    disconnected_since: float | None = None


class BrowserSessionReclaimer:
    """用一个后台观察线程回收已断开的 Streamlit 浏览器会话。"""

    def __init__(self, check_interval: float = 1.0, disconnect_grace: float = 3.0) -> None:
        self._check_interval = check_interval
        self._disconnect_grace = disconnect_grace
        self._lock = Lock()
        self._registrations: dict[str, BrowserSessionRegistration] = {}
        Thread(target=self._watch, name="wence-browser-session-reclaimer", daemon=True).start()

    def register(self, session_id: str, api_base: str, beacon_token: str) -> None:
        """登记当前浏览器连接；只允许本机 API 目标。"""

        safe_base = loopback_api_base(api_base)
        if safe_base is None:
            return
        with self._lock:
            self._registrations[session_id] = BrowserSessionRegistration(
                api_base=safe_base,
                beacon_token=beacon_token,
            )

    def unregister(self, session_id: str) -> None:
        """用户显式退出或登录失效时移除浏览器连接登记。"""

        with self._lock:
            self._registrations.pop(session_id, None)

    def _watch(self) -> None:
        while True:
            sleep(self._check_interval)
            now = monotonic()
            releases: list[BrowserSessionRegistration] = []
            try:
                runtime = get_instance()
            except RuntimeError:
                continue
            with self._lock:
                for session_id, registration in list(self._registrations.items()):
                    if runtime.is_active_session(session_id):
                        registration.disconnected_since = None
                    elif registration.disconnected_since is None:
                        registration.disconnected_since = now
                    elif now - registration.disconnected_since >= self._disconnect_grace:
                        releases.append(self._registrations.pop(session_id))
            for registration in releases:
                try:
                    httpx.post(
                        f"{registration.api_base}/auth/logout/beacon",
                        content=registration.beacon_token,
                        headers={"Content-Type": "text/plain;charset=UTF-8"},
                        timeout=2.0,
                    )
                except httpx.HTTPError:
                    # 本机后端不可达时由 10 分钟空闲回收任务兜底。
                    pass


@st.cache_resource(show_spinner=False)
def browser_session_reclaimer() -> BrowserSessionReclaimer:
    """返回跨 Streamlit 会话共享的单一浏览器连接观察器。"""

    return BrowserSessionReclaimer()


def current_browser_session_id() -> str | None:
    """读取当前 Streamlit 会话 ID；非脚本上下文返回 None。"""

    context = get_script_run_ctx(suppress_warning=True)
    return context.session_id if context is not None else None


def register_browser_session(api_base: str) -> None:
    """把当前浏览器连接登记到本机服务端观察器。"""

    session_id = current_browser_session_id()
    beacon_token = st.session_state.get("session_beacon_token")
    if session_id and beacon_token:
        browser_session_reclaimer().register(session_id, api_base, beacon_token)


@st.fragment(run_every=30)
def enforce_session_timeout(api_base: str) -> None:
    """定时检查服务端租约；同时保留后端断开时的兜底状态。"""

    if st.session_state.get("auth_token"):
        now = monotonic()
        if now - st.session_state.get("session_status_checked_at", 0.0) < 30:
            return
        st.session_state.session_status_checked_at = now
        st.session_state.service_unavailable = (
            api_request(api_base, "GET", "/auth/session/status") is None
        )


def utc_now() -> str:
    """生成 API 要求的带时区 ISO-8601 时间，避免浏览器本地时区造成核验歧义。"""
    return datetime.now(timezone.utc).isoformat()


def init_session() -> None:
    """初始化浏览器会话；登录后的对话会另外持久化到 MySQL。"""
    st.session_state.setdefault(
        "profile",
        {
            "risk_level": None,
            "risk_score": None,
            "horizon_months": None,
            "max_drawdown": None,
            "liquidity_need": None,
            "constraints": [],
            "target": None,
            "single_security_limit": 0.20,
            "industry_limit": 0.30,
            "version": 1,
            "confirmed": False,
        },
    )
    st.session_state.setdefault("facts", [])
    st.session_state.setdefault("portfolio", [])
    st.session_state.setdefault("watchlist", [])
    st.session_state.setdefault("watchlist_loaded", False)
    st.session_state.setdefault("price_history", None)
    st.session_state.setdefault("advice", None)
    st.session_state.setdefault("last_error", None)
    st.session_state.setdefault("conversation", [])
    st.session_state.setdefault("questionnaire", {})
    st.session_state.setdefault("auth_token", None)
    st.session_state.setdefault("session_beacon_token", None)
    st.session_state.setdefault("auth_user", None)
    st.session_state.setdefault("conversation_id", str(uuid.uuid4()))
    st.session_state.setdefault("api_base", DEFAULT_API_BASE)


def format_api_error(detail: Any) -> str:
    """把 FastAPI/Pydantic 错误结构转换为简洁、可操作的中文提示。"""

    if isinstance(detail, str):
        # 这些文案由后端的数据适配器生成，不含调用栈、URL 或密钥值，可以直接
        # 告诉用户如何修复。先匹配它们，避免 IWENCAI_API_KEY 中的 ``API`` 被
        # 下方的通用脱敏规则误判为内部错误并替换成无从排查的兜底文案。
        if detail.startswith("尚未配置 IWENCAI_API_KEY"):
            return "尚未配置问财访问密钥。请在 .env 中设置 IWENCAI_API_KEY，并重启后端。"
        if detail.startswith(("问财", "连接问财", "无法建立问财")):
            return plain_language(detail)
        if re.search(r"Traceback|SQL|https?://|API|Error|Exception|[A-Za-z_]+\.[A-Za-z_]+", detail, re.I):
            return "服务暂时无法完成请求，请稍后重试。"
        return plain_language(detail)
    if not isinstance(detail, list):
        return "请求未通过校验，请检查填写内容后重试。"

    field_labels = {
        "username": "账号",
        "password": "密码",
        "confirmation": "确认密码",
        "query": "问题",
        "profile": "投资画像",
    }
    messages: list[str] = []
    for error in detail:
        if not isinstance(error, dict):
            continue
        location = error.get("loc") or []
        field = str(location[-1]) if location else "填写内容"
        label = field_labels.get(field, "填写内容")
        error_type = str(error.get("type", ""))
        context = error.get("ctx") or {}

        if error_type == "missing":
            message = f"请填写{label}。"
        elif error_type == "string_too_short":
            minimum = context.get("min_length")
            message = f"{label}至少需要 {minimum} 个字符。" if minimum else f"{label}内容太短。"
        elif error_type == "string_too_long":
            maximum = context.get("max_length")
            message = f"{label}最多允许 {maximum} 个字符。" if maximum else f"{label}内容太长。"
        elif error_type == "string_pattern_mismatch" and field == "username":
            message = "账号只能包含英文字母、数字、下划线或连字符。"
        else:
            message = f"{label}格式不正确，请检查后重试。"
        if message not in messages:
            messages.append(message)

    return " ".join(messages) or "请求未通过校验，请检查填写内容后重试。"


def api_request(
    api_base: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    quiet: bool = False,
) -> Any | None:
    """请求后端并将网络/服务异常转成可理解的前端提示。

    不把异常详情显示给普通用户，防止内部 URL、调用栈或代理配置泄漏；开发者
    仍可在 Streamlit 终端看到完整异常。请求失败后返回 ``None``，调用方不要把
    它错误当作一个空的业务响应。``quiet=True`` 用于可选能力探测，不打扰用户。
    """
    result = request_json(backend_http_client(api_base), api_base, method, path, payload,
                          st.session_state.get("auth_token"))
    if result.data is not None:
        return result.data
    if quiet:
        return None
    if result.status == 409 and path == "/profile/confirm":
        st.session_state.profile_restored = False
    message = format_api_error(result.detail)
    st.error(message)
    if result.status == 401 and path not in {"/auth/login", "/auth/register"}:
        reset_user_session()
        st.session_state.auth_notice = message
        st.rerun()
    return None


def profile_ready() -> bool:
    """个性化分析只接受经确认画像；前端在调用前再次提示，但后端仍是最终闸门。"""
    return assessment_is_current(st.session_state.profile)


def add_fact(fact: dict[str, Any]) -> None:
    """按 fact_id 替换同名快照，防止证据中心展示同一事实的重复版本。"""
    facts = [row for row in st.session_state.facts if row["fact_id"] != fact["fact_id"]]
    facts.append(fact)
    st.session_state.facts = facts


def stream_analysis(api_base: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """显示后端真实执行阶段，最终核验完成前不渲染投资观点。"""

    headers = {"Authorization": f"Bearer {st.session_state.auth_token}"} if st.session_state.get("auth_token") else {}
    advice = None
    with st.status("正在准备分析…", expanded=True) as status:
        try:
            with backend_http_client(api_base).stream(
                "POST", f"{api_base}/portfolio/analyze/stream", json=payload, headers=headers,
                timeout=httpx.Timeout(240.0, connect=10.0),
            ) as response:
                if response.status_code >= 400:
                    if response.status_code == 409:
                        st.session_state.profile_restored = False
                    try:
                        response.read()
                        message = format_api_error(response.json().get("detail"))
                    except (ValueError, httpx.HTTPError):
                        message = "分析暂时无法完成，请稍后重试。"
                    st.error(message)
                    status.update(label="分析未完成", state="error")
                    return None
                for line in response.iter_lines():
                    if not line:
                        continue
                    event = json.loads(line)
                    if event.get("type") == "progress":
                        stage = str(event.get("stage") or "处理中")
                        status.update(label=f"正在{stage}…")
                        st.write(stage)
                    elif event.get("type") == "result":
                        advice = event.get("advice")
                    elif event.get("type") == "error":
                        st.error(format_api_error(event.get("message")))
                        status.update(label="分析未完成", state="error")
                        return None
        except (httpx.HTTPError, ValueError, KeyError):
            st.error("分析连接暂时中断，请稍后再试。")
            status.update(label="分析未完成", state="error")
            return None
        if not isinstance(advice, dict):
            st.error("分析结果暂未返回，请稍后再试。")
            status.update(label="分析未完成", state="error")
            return None
        status.update(label="分析与风险检查已完成", state="complete", expanded=False)
    return advice


def run_analysis(api_base: str, query: str) -> dict[str, Any] | None:
    """携带最近多轮上下文调用统一分析端点，并更新会话历史。"""
    if not profile_ready():
        st.warning("请先在“投资偏好”中确认您的情况，再开始分析。")
        return None
    if not query.strip():
        st.warning("请输入研究问题。")
        return None
    user_turn = {"role": "user", "content": query, "created_at": utc_now()}
    payload = {
        "query": query,
        "profile": st.session_state.profile,
        "facts": st.session_state.facts,
        "auto_fetch": True,
        "portfolio": st.session_state.portfolio,
        "conversation_id": st.session_state.conversation_id,
        "context_messages": [
            {key: turn[key] for key in ("role", "content", "created_at") if key in turn}
            for turn in [*st.session_state.conversation, user_turn][-20:]
        ],
    }
    advice = stream_analysis(api_base, payload)
    if advice:
        advice.setdefault("facts", [dict(fact) for fact in st.session_state.facts])
        st.session_state.conversation.append(user_turn)
        for fact in advice.get("facts", []):
            add_fact(fact)
        st.session_state.advice = advice
        st.session_state.last_error = None
        st.session_state.conversation.append(
            {"role": "assistant", "content": advice["conclusion"], "created_at": utc_now(), "payload": advice}
        )
        invalidate_recent_conversations(api_base)
        st.session_state.pop("history_detail_cache", None)
    return advice


@st.cache_resource(show_spinner=False)
def analysis_executor() -> ThreadPoolExecutor:
    """共享有界线程池；任务只接收提交时复制的数据与鉴权令牌。"""
    return ThreadPoolExecutor(max_workers=4, thread_name_prefix="wence-analysis")


def submit_chat_analysis(api_base: str, direction: str, query: str) -> None:
    """立即显示问题，后台完成分析，避免等待期间锁住整页交互。"""
    if not profile_ready():
        st.warning("请先确认投资偏好，再开始分析。")
        return
    jobs = st.session_state.setdefault("analysis_jobs", {})
    if direction in jobs:
        return
    user_turn = {"role": "user", "content": query, "created_at": utc_now()}
    conversation_id = st.session_state.conversation_id
    payload = {
        "query": query,
        "research_direction": direction,
        "profile": dict(st.session_state.profile),
        "facts": [dict(fact) for fact in st.session_state.facts],
        "auto_fetch": True,
        "portfolio": [dict(item) for item in st.session_state.portfolio],
        "conversation_id": conversation_id,
        "context_messages": [
            {key: turn[key] for key in ("role", "content", "created_at") if key in turn}
            for turn in [*st.session_state.conversation, user_turn][-20:]
        ],
    }
    token = st.session_state.auth_token
    client = backend_http_client(api_base)
    future = analysis_executor().submit(request_json, client, api_base, "POST", "/portfolio/analyze", payload, token)
    jobs[direction] = {"future": future, "conversation_id": conversation_id}
    st.session_state.conversation.append(user_turn)
    save_research_session()


def finish_chat_analyses() -> bool:
    """仅在 Streamlit 脚本线程合并结果；旧会话结果绝不写入新会话。"""
    jobs = st.session_state.get("analysis_jobs", {})
    changed = False
    for direction, job in list(jobs.items()):
        future = job["future"]
        if not future.done():
            continue
        changed = True
        jobs.pop(direction, None)
        try:
            result = future.result()
        except Exception:
            result = None
        advice = result.data if result is not None and isinstance(result.data, dict) else None
        same_page = (direction == st.session_state.get("active_research_direction", QUICK_ASKS[0])
                     and job["conversation_id"] == st.session_state.get("conversation_id"))
        saved = st.session_state.get("research_sessions", {}).get(direction)
        target = st.session_state if same_page else saved if saved and saved.get("conversation_id") == job["conversation_id"] else None
        if advice:
            if target is not None:
                advice.setdefault("facts", [dict(fact) for fact in target["facts"]])
                facts_by_id = {fact.get("fact_id"): fact for fact in target["facts"]}
                for fact in advice.get("facts", []):
                    facts_by_id[fact.get("fact_id")] = fact
                target["facts"] = list(facts_by_id.values())
                target["advice"] = advice
                target["conversation"].append({"role": "assistant", "content": advice["conclusion"],
                    "created_at": utc_now(), "payload": advice})
            invalidate_recent_conversations()
            st.session_state.pop("history_detail_cache", None)
            st.session_state.analysis_notice = "后台分析已完成，可在最近咨询中查看。"
        else:
            st.session_state.analysis_notice = format_api_error(result.detail) if result is not None else "分析暂时无法完成，请稍后重试。"
        if same_page:
            save_research_session()
    return changed


@st.fragment(run_every=2)
def background_analysis_status() -> None:
    if finish_chat_analyses():
        st.rerun()
    jobs = st.session_state.get("analysis_jobs", {})
    if jobs:
        st.caption(f"{len(jobs)} 个问题正在后台分析，您可以继续使用其他功能。")
    notice = st.session_state.pop("analysis_notice", None)
    if notice:
        st.info(notice)


def close_history_view() -> None:
    st.session_state.history_view = False


def go_to(page: str) -> None:
    if page == "投资偏好":
        page = "风险评估"
    if page in {"风险评估", "风险调整"}:
        st.session_state.preference_navigation_open = True
    if page == "历史记录":
        st.session_state.history_view = True
    else:
        st.session_state.history_view = False
        st.session_state.navigation = page


RESEARCH_SESSION_FIELDS = ("conversation", "conversation_id", "advice", "facts", "history_before_id")


def save_research_session() -> None:
    """保存当前方向；共享的投资偏好、自选和持仓不属于聊天上下文。"""
    direction = st.session_state.get("active_research_direction", QUICK_ASKS[0])
    sessions = st.session_state.setdefault("research_sessions", {})
    sessions[direction] = {key: st.session_state.get(key) for key in RESEARCH_SESSION_FIELDS}
    st.session_state.setdefault("conversation_directions", {})[st.session_state.conversation_id] = direction


def activate_research(direction: str) -> None:
    """切换完整研究上下文，避免问题、证据和结果跨方向混用。"""
    if direction == st.session_state.get("active_research_direction", QUICK_ASKS[0]):
        return
    save_research_session()
    sessions = st.session_state.research_sessions
    if direction not in sessions:
        sessions[direction] = {
            "conversation": [], "conversation_id": str(uuid.uuid4()),
            "advice": None, "facts": [], "history_before_id": None,
        }
    for key, value in sessions[direction].items():
        st.session_state[key] = value
    st.session_state.active_research_direction = direction


def select_research_direction() -> None:
    activate_research(st.session_state.research_direction)


def continue_last_research(api_base: str, *, request) -> None:
    """优先继续当前对话；重新登录后恢复账号最近保存的研究。"""
    recent = st.session_state.get("recent_conversations") or []
    if not st.session_state.get("conversation") and recent:
        detail = request(api_base, "GET", f"/history/{recent[0]['id']}")
        if isinstance(detail, dict) and detail.get("messages"):
            restore_conversation(detail)
    go_to("投资问答")


def page_home(api_base: str) -> None:
    from frontend.home_page import render_home
    render_home(api_base, ready=profile_ready(), header=render_page_header, go_to=go_to,
                recent=st.session_state.get("recent_conversations", []), fetch=create_board_fetch(api_base),
                resume=partial(continue_last_research, request=api_request))


def page_questions(api_base: str) -> None:
    render_quick_ask(api_base)


def render_quick_ask(api_base: str) -> None:
    """五个方向共用布局，各自拥有独立页面和底部输入框。"""
    st.session_state.setdefault("research_direction", st.session_state.get("active_research_direction", QUICK_ASKS[0]))
    direction = st.segmented_control(
        "研究方向", QUICK_ASKS, key="research_direction", required=True,
        on_change=select_research_direction, width="stretch", persist_state="session",
    )
    activate_research(direction or QUICK_ASKS[0])
    render_research_page(api_base, direction or QUICK_ASKS[0])


def create_board_fetch(api_base: str):
    return overview_fetch(backend_http_client(api_base), st.session_state.get("auth_token"))


def prefetch_research_board_data(api_base: str) -> None:
    """捕获令牌，账号资料与五个方向数据同时预取；不阻塞登录。"""
    start_account_prefetch(api_base, st.session_state.get("auth_token"), backend_http_client(api_base))
    start_board_prefetch(api_base, create_board_fetch(api_base))


def fetch_research_board_data(api_base: str, direction: str, target: str | None) -> dict | None:
    return create_board_fetch(api_base)(api_base, direction, target)


def render_research_page(api_base: str, direction: str) -> None:
    render_page_header(direction)
    st.title(direction)
    # 空白页先展示本方向的数据；已有对话中将数据折叠，方便继续阅读回答。
    if st.session_state.conversation:
        with st.expander("本页领域数据", expanded=False, on_change="rerun", icon=":material/monitoring:") as section:
            if section.open:
                render_research_board(api_base, direction, create_board_fetch(api_base))
    else:
        render_research_board(api_base, direction, create_board_fetch(api_base))
    if not profile_ready():
        st.info("请先确认投资偏好，再开始分析。", icon=":material/person_edit:")
        st.button("填写投资偏好", type="primary", on_click=go_to, args=("投资偏好",))
    if st.session_state.conversation:
        with st.container(horizontal=True, vertical_alignment="center"):
            st.markdown(f"**对话中 · {len(st.session_state.conversation)} 条消息**")
            st.space("stretch")
            st.button("发起咨询", icon=":material/add_comment:", on_click=new_conversation)
            st.button("查看分析详情", icon=":material/fact_check:", on_click=go_to, args=("历史记录",))
    before_id = st.session_state.get("history_before_id")
    if before_id and st.button("加载更早消息", key="load_older_chat"):
        older = api_request(api_base, "GET", f"/history/{st.session_state.conversation_id}?before_id={before_id}")
        if isinstance(older, dict):
            st.session_state.conversation = [
                {"role": item["role"], "content": item["content"],
                 "created_at": item["created_at"], "payload": item.get("payload")}
                for item in older.get("messages", [])
            ] + st.session_state.conversation
            st.session_state.history_before_id = older.get("next_before_id")
            save_research_session()
            st.rerun()
    latest_answer = next((index for index in range(len(st.session_state.conversation)-1, -1, -1)
                          if st.session_state.conversation[index].get("role") == "assistant"), -1)
    for turn_index, turn in enumerate(st.session_state.conversation):
        with st.chat_message(turn["role"]):
            if turn["role"] == "assistant" and turn.get("payload"):
                previous = st.session_state.conversation[turn_index - 1] if turn_index else {}
                render_advice(turn["payload"], export_key=f"chat_{turn_index}", compact=turn_index != latest_answer,
                              question=previous.get("content", "") if previous.get("role") == "user" else "")
            else:
                content = turn["content"]
                if turn["role"] == "user":
                    content = content.removeprefix(RESEARCH_PREFIX[direction])
                st.write(plain_language(content) if turn["role"] == "assistant" else content)
    # 必须在页面顶层调用，Streamlit 才会将唯一输入框固定在底部。
    query = st.chat_input(QUICK_ASK_PROMPTS[direction], key=f"research_chat_{direction}",
                          disabled=not profile_ready() or direction in st.session_state.get("analysis_jobs", {}),
                          width="stretch")
    if query and query.strip():
        submit_chat_analysis(api_base, direction, RESEARCH_PREFIX[direction] + query.strip())
        st.rerun()
    if direction in st.session_state.get("analysis_jobs", {}):
        st.info("正在后台核对资料与风险。您可以切换页面，完成后会自动显示结果。")
    st.html("<div class='legal-strip'>分析仅供参考，不保证收益。投资前，请结合自己的情况判断。</div>")
    restore_board_scroll(direction, has_conversation=bool(st.session_state.conversation))


def render_materials_panel(api_base: str) -> None:
    """问答页内嵌的资料面板：取数、补充和整理都在这里完成，不再单独占一个入口。"""

    count = len(st.session_state.facts)
    if count:
        st.caption(f"本次提问会一并参考这 {count} 条研究资料。")
    else:
        st.caption("暂无研究资料，可查询行情、资讯或补充外部资料。")
    label = f"研究资料（{count}）· 查询与整理" if count else "研究资料 · 查询与整理"
    with st.expander(label, expanded=not count, icon=":material/library_books:"):
        render_materials_body(api_base)


def render_materials_body(api_base: str) -> None:
    """资料区主体：查询、补充与整理三个页签；由问答页内嵌渲染，不再有独立页面。"""

    st.caption("查询行情、新闻与公告，或补充自己的资料；整理结果会自动用于投资问答。")
    total = len(st.session_state.facts)
    query_tab, manual_tab, list_tab = st.tabs([
        "查询资料", "补充资料", f"已有资料（{total}）" if total else "已有资料",
    ])
    with query_tab:
        with st.form("material_query"):
            kind_label = st.selectbox("资料类型", list(DATA_KINDS))
            target = st.text_input("股票、基金或查询条件",
                                   placeholder="例如：600519、新能源行业近一个月、低费率宽基 ETF", max_chars=500)
            query_submitted = st.form_submit_button("查询并加入资料", type="primary")
        if query_submitted:
            if not target.strip():
                st.warning("请填写名称、代码或查询条件。")
            else:
                kind = DATA_KINDS[kind_label]
                with st.spinner("正在查询资料…"):
                    result = api_request(api_base, "POST", "/data/fetch", {
                        "kind": kind, "target": target.strip(),
                        "filters": {"query": target.strip()} if kind == "fund" else {},
                    })
                if result is not None:
                    facts = result.get("facts", [])
                    if result.get("status") == "unavailable":
                        message = result.get("message") or "问财数据暂时不可用，请稍后重试。"
                        st.warning(format_api_error(message), icon=":material/cloud_off:")
                    elif facts:
                        preserve_analysis_materials()
                        for fact in facts:
                            add_fact(fact)
                        st.success(f"已加入 {len(facts)} 条资料，可以直接提问了。")
                    else:
                        st.info("没有找到符合条件的资料，可以换一个名称或查询条件。")
    with manual_tab:
        with st.form("manual_material", clear_on_submit=True):
            st.caption("手工补充资料：请填写真实来源和日期，百分比带 %（例如费率填 0.5%）。")
            entity = st.text_input("资料涉及的对象", placeholder="股票、基金或行业名称")
            field = st.selectbox("指标或资料类型", list(FIELD_LABELS), index=list(FIELD_LABELS).index("close_price"),
                                 format_func=FIELD_LABELS.get)
            value_text = st.text_area("数值或内容", placeholder="输入原始数值、新闻或公告内容")
            source = st.text_input("资料来源", placeholder="例如：公司年报、公告名称或原文链接", max_chars=300)
            local_today = datetime.now(timezone(timedelta(hours=8))).date()
            as_of = st.date_input("资料日期", value=local_today, max_value=local_today)
            submitted = st.form_submit_button("保存资料")
        if submitted:
            if not entity.strip() or not source.strip():
                st.warning("请填写资料对象和来源。")
            else:
                try:
                    value = manual_fact_value(field, value_text)
                except ValueError as exc:
                    st.warning(str(exc))
                else:
                    preserve_analysis_materials()
                    add_fact({"fact_id": f"MANUAL-{uuid.uuid4().hex[:12]}", "entity": entity.strip(),
                              "field": field, "value": value,
                              "snapshot_time": datetime.combine(as_of, datetime.min.time(),
                                                                tzinfo=timezone(timedelta(hours=8))).isoformat(),
                              "source_id": f"USER_SUPPLIED:{source.strip()}", "quality": 0.8})
                    st.success("资料已保存，后续分析会一并参考。")
    with list_tab:
        facts = st.session_state.facts
        if not facts:
            render_empty_state("暂无研究资料", "请通过“查询资料”或“补充资料”添加研究依据。",
                               ":material/library_add:")
            return
        search = st.text_input("在资料里查找", placeholder="按名称、指标或内容搜索", icon=":material/search:")
        visible = [fact for fact in facts if search.casefold() in
                   f"{fact.get('entity', '')} {fact_label(str(fact.get('field', '')))} {fact.get('value', '')}".casefold()]
        if visible:
            st.caption(f"共 {len(facts)} 条资料，当前显示 {len(visible)} 条。")
            st.dataframe(friendly_fact_rows(visible), width="stretch", hide_index=True)
        else:
            render_empty_state("未检索到匹配资料", "请调整标的名称、指标或检索关键词。", ":material/search_off:")
        manageable = [fact for fact in facts if fact.get("source_id") != "USER_PORTFOLIO_SNAPSHOT"]
        if manageable:
            with st.expander("整理资料"):
                st.caption("移除只影响之后的分析；已有分析的依据仍会保留。持仓请在“持仓分析”中管理。")
                labels = {fact["fact_id"]: f"{fact['entity']} · {fact_label(fact['field'])} · {str(fact.get('snapshot_time', ''))[:10]} · {index + 1}"
                          for index, fact in enumerate(manageable)}
                selected = st.multiselect("选择要移除的资料", list(labels), format_func=labels.get, key="data_selected")
                if st.button("移除所选资料", disabled=not selected):
                    preserve_analysis_materials()
                    st.session_state.facts = [fact for fact in facts if fact["fact_id"] not in selected]
                    st.session_state.pop("data_selected", None)
                    st.rerun()
                if st.button("清空研究资料"):
                    preserve_analysis_materials()
                    st.session_state.facts = [fact for fact in facts if fact.get("source_id") == "USER_PORTFOLIO_SNAPSHOT"]
                    st.session_state.pop("data_selected", None)
                    st.rerun()


def page_profile(api_base: str, *, view: str = "风险评估") -> None:
    """投资偏好统一使用十九题风险测评。"""
    render_page_header(view)
    st.title(view)
    render_risk_assessment(api_base, view=view, api_request=api_request)


def available_analyses() -> list[tuple[str, dict[str, Any]]]:
    """收集当前会话各次分析，使用每次结果自身的资料包。"""
    entries = []
    question = "投资分析"
    for turn in st.session_state.conversation:
        if turn.get("role") == "user":
            question = turn.get("content", question)
        elif turn.get("payload"):
            entries.append((question, turn["payload"]))
    if st.session_state.get("portfolio_advice"):
        entries.append(("持仓分析", st.session_state.portfolio_advice))
    if st.session_state.advice:
        entries.append(("最近一次分析", st.session_state.advice))
    unique = []
    seen = set()
    for title, advice in entries:
        identity = advice.get("trace_id") or id(advice)
        if identity not in seen:
            unique.append((title, advice))
            seen.add(identity)
    return list(reversed(unique))


def preserve_analysis_materials() -> None:
    """修改当前资料前为旧版结果补上快照，历史依据不随当前列表改变。"""
    for _, advice in available_analyses():
        advice.setdefault("facts", [dict(fact) for fact in st.session_state.facts])


def manual_fact_value(field: str, value_text: str) -> Any:
    raw = value_text.strip()
    if not raw:
        raise ValueError("请填写指标值或资料内容。")
    percent = raw.endswith(("%", "％"))
    try:
        numeric = float(raw.rstrip("%％").replace(",", ""))
    except ValueError:
        if field in {"fee_rate", "weight", "close_price"} or field.endswith("_score"):
            raise ValueError("这个指标需要填写数字；百分比请写成 0.5% 这样的形式。") from None
        return raw
    if not math.isfinite(numeric):
        raise ValueError("请填写有效数字。")
    if field in {"fee_rate", "weight"} and percent:
        return numeric / 100
    # 其他供应商百分比的口径不统一，保留显式单位，避免猜测并转换错误。
    return f"{numeric:g}%" if percent else numeric


def render_professional_views(advice: dict[str, Any]) -> None:
    results = advice.get("agent_results", [])
    if not results:
        st.caption("本次暂无分项分析结果。")
    for result in results:
        st.markdown(f"**{TOPIC_LABELS.get(result.get('agent_id'), '相关分析')}**")
        st.write(plain_language(result.get("opinion")) or "资料不足，暂未形成结论。")
        status = result.get("status")
        if status != "completed":
            st.caption(PROGRESS_LABELS.get(status, "待确认"))
        render_points("相关风险", result.get("risk_flags", []))
        render_points("哪些变化需要重新判断", result.get("invalidation_conditions", []))


def page_insights(api_base: str) -> None:
    """历史记录与分析详情合并为一页：同一份数据，两个回看角度。"""

    st.title("历史记录")
    st.button("返回投资问答", icon=":material/arrow_back:", on_click=go_to,
              args=("投资问答",))
    render_page_header("回看 · 对话与分析详情")
    st.caption("查看已保存的对话，或回看某次分析的观点、依据与完成情况。")
    history_tab, analysis_tab = st.tabs(
        ["对话记录", "分析详情"], key="insights_view", on_change="rerun"
    )
    if history_tab.open:
        with history_tab:
            page_conversations(api_base)
    if analysis_tab.open:
        with analysis_tab:
            render_analysis_details()


def render_analysis_details() -> None:
    entries = available_analyses()
    if not entries:
        render_empty_state("暂无分析记录", "投资问答及持仓分析完成后，结果将在此展示。",
                           ":material/history:")
        return
    index = st.selectbox("选择一次分析", list(range(len(entries))),
                         format_func=lambda i: f"{i + 1}. {entries[i][0][:70]}")
    _, advice = entries[index]
    summary, chain, evidence, progress = st.tabs(
        ["结论面板", "投资逻辑链", "数据溯源", "执行记录"],
        key="analysis_detail_view",
        on_change="rerun",
    )
    if summary.open:
        with summary:
            show_status(advice.get("compliance", {}).get("status", "REVIEW"))
            render_conclusion_panel(advice)
            render_chain_overview(advice)
    if chain.open:
        with chain:
            render_logic_chain(advice)
    if evidence.open:
        with evidence:
            render_source_trace(advice, collapsed=False)
            facts = advice_facts(advice)
            used_ids = set(advice.get("evidence", []))
            unused = [f for f in facts if f.get("fact_id") not in used_ids]
            if unused:
                with st.expander("未被这次分析引用的资料"):
                    st.caption("可能与当前问题无关，或日期、来源尚待确认；未引用不代表资料错误。")
                    st.dataframe(friendly_fact_rows(unused), width="stretch", hide_index=True)
    if progress.open:
        with progress:
            plan = advice.get("task_plan", {})
            if plan.get("clarification_question"):
                st.info(plain_language(plan["clarification_question"]))
            nodes = plan.get("nodes", [])
            if nodes:
                st.dataframe([{"分析环节": TOPIC_LABELS.get(node.get("agent_id"), "相关分析"),
                               "完成情况": PROGRESS_LABELS.get(node.get("status"), "待确认")} for node in nodes],
                             width="stretch", hide_index=True)
                st.caption("这里显示所选分析的完成记录。")
            else:
                st.caption("本次暂无可展示的分项记录。")


def ensure_watchlist_loaded(api_base: str) -> None:
    """每个登录会话只加载一次账号自选；失败时保留重试机会。"""

    if st.session_state.get("watchlist_loaded"):
        return
    if monotonic() < st.session_state.get("watchlist_retry_at", 0.0):
        return
    result = take_account_result(api_base, "/watchlist")
    if result is PENDING:
        return
    if result is NOT_PREFETCHED:
        result = api_request(api_base, "GET", "/watchlist", quiet=True)
    if isinstance(result, list):
        st.session_state.watchlist = result
        st.session_state.watchlist_loaded = True
        st.session_state.pop("watchlist_retry_at", None)
    else:
        st.session_state.watchlist_retry_at = monotonic() + 30


def _watchlist_compare_prefix(items: list[dict[str, Any]]) -> str:
    types = {str(item.get("asset_type", "股票")) for item in items}
    if len(types) == 1:
        return WATCHLIST_RESEARCH_PREFIX.get(types.pop(), "个股研究：")
    raise ValueError("横向比较需选择相同类型的标的。")


def page_watchlist(api_base: str) -> None:
    from frontend.watchlist_page import render_watchlist
    render_watchlist(api_base, {"api": api_request, "analysis": run_analysis, "activate": activate_research,
        "save": save_research_session, "ready": profile_ready, "go_to": go_to, "add_fact": add_fact,
        "header": render_page_header, "history": render_price_history_panel,
        "factory": snapshot_fetch_factory(backend_http_client(api_base), st.session_state.get("auth_token"))})


def render_price_history_panel(api_base: str, items: list[dict[str, Any]]) -> None:
    """用户选择标的后才取数；走势不进入投资建议的事实与合规链。"""
    import altair as alt
    import pandas as pd

    eligible = [item for item in items if item.get("asset_type", "股票") in {"股票", "基金", "可转债"}]
    st.subheader("历史走势")
    if not eligible:
        st.info("当前自选中没有可展示收盘价或单位净值走势的标的。")
        return
    options = {int(item["id"]): item for item in eligible}
    with st.container(key="price-history-panel", border=True):
        selected_id = st.selectbox(
            "查看标的", list(options),
            format_func=lambda item_id: f"{options[item_id].get('target', '—')} · {options[item_id].get('asset_type', '股票')}",
        )
        selected = options[selected_id]
        target = str(selected.get("target", "")).strip()
        asset_type = str(selected.get("asset_type", "股票"))
        if st.button("加载 / 刷新近 30 期走势", icon=":material/show_chart:", key="load_price_history"):
            st.session_state.price_history = None
            with st.spinner("正在从问财获取历史数据…"):
                st.session_state.price_history = api_request(
                    api_base, "POST", "/data/price-history",
                    {"target": target, "asset_type": asset_type, "limit": 30},
                )
        result = st.session_state.get("price_history")
        if not isinstance(result, dict) or result.get("target") != target or result.get("asset_type") != asset_type:
            st.caption("选择标的后点击加载；只有问财返回带日期的数值才会显示曲线。")
            return
        if result.get("status") == "unavailable":
            st.warning(format_api_error(result.get("message") or "问财历史数据暂时不可用。"))
            return
        if result.get("status") != "ok" or len(result.get("points") or []) < 2:
            st.info(result.get("message") or "带日期的数据不足，暂时无法绘制走势。")
            return
        rows = [
            {"日期": str(point["date"]), "数值": float(point["value"])}
            for point in result["points"]
        ]
        metric_label = str(result.get("metric_label") or "收盘价")
        chart = (
            alt.Chart(pd.DataFrame(rows))
            .mark_line(color="#ad384e", point=True, strokeWidth=2.5)
            .encode(
                x=alt.X("日期:T", title="日期", axis=alt.Axis(format="%m/%d")),
                y=alt.Y("数值:Q", title=metric_label, scale=alt.Scale(zero=False)),
                tooltip=[alt.Tooltip("日期:T", title="日期", format="%Y-%m-%d"),
                         alt.Tooltip("数值:Q", title=metric_label, format=",.4f")],
            )
            .properties(height=250)
        )
        st.altair_chart(chart, width="stretch")
        st.caption(
            f"{result.get('source_name') or '同花顺问财'} · 数据日期 {rows[0]['日期']} 至 {rows[-1]['日期']}"
            f" · 查询于 {fact_time(result.get('fetched_at'))}"
        )
        if result.get("source_url"):
            st.link_button("查看数据来源", str(result["source_url"]), icon=":material/open_in_new:")


def portfolio_risk_snapshot(
    portfolio: list[dict[str, Any]], profile: dict[str, Any]
) -> dict[str, Any]:
    """按用户填写权重生成可复算的集中度摘要，不作涨跌预测。"""

    holdings = [
        {"name": str(item.get("name", "—")), "weight": max(0.0, float(item.get("weight", 0)))}
        for item in portfolio
    ]
    weights = sorted((item["weight"] for item in holdings), reverse=True)
    total = sum(weights)
    limit = float(profile.get("single_security_limit") or 0.20)
    over_limit = [item["name"] for item in holdings if item["weight"] > limit]
    return {
        "holdings": holdings,
        "total": total,
        "largest": weights[0] if weights else 0.0,
        "top_two": sum(weights[:2]),
        "unallocated": max(0.0, 1.0 - total),
        "single_limit": limit,
        "over_limit": over_limit,
    }


def render_portfolio_risk_dashboard(portfolio: list[dict[str, Any]]) -> None:
    """展示权重分布、集中度边界和机械压力情景。"""
    import altair as alt
    import pandas as pd

    snapshot = portfolio_risk_snapshot(portfolio, st.session_state.profile)
    st.subheader("持仓风险驾驶舱")
    if snapshot["holdings"] and snapshot["total"] <= 1.0:
        combined: dict[str, float] = {}
        for item in snapshot["holdings"]:
            combined[item["name"]] = combined.get(item["name"], 0.0) + item["weight"]
        ranked = sorted(((name, weight) for name, weight in combined.items() if weight > 0),
                        key=lambda item: item[1], reverse=True)
        slices = [{"项目": f"持仓 · {name}", "占比": weight} for name, weight in ranked[:5] if weight > 0]
        if len(ranked) > 5:
            slices.append({"项目": "其他持仓", "占比": sum(weight for _, weight in ranked[5:])})
        if snapshot["unallocated"] > 0:
            slices.append({"项目": "待配置", "占比": snapshot["unallocated"]})
        names = [item["项目"] for item in slices]
        colors = ["#8e2c40", "#c15f70", "#d89479", "#706080", "#477e86", "#b78b46"]
        palette = ["#e7e2df" if name == "待配置" else colors[index % len(colors)]
                   for index, name in enumerate(names)]
        with st.container(key="portfolio-donut", border=True):
            st.markdown("**持仓构成**")
            st.caption("按您录入的组合占比计算；灰色部分表示尚未配置的比例。"
                       + ("其余持仓已合并显示，完整权重见下方。" if len(ranked) > 5 else ""))
            donut = (
                alt.Chart(pd.DataFrame(slices))
                .mark_arc(innerRadius=67, outerRadius=108, stroke="#ffffff", strokeWidth=2)
                .encode(
                    theta=alt.Theta("占比:Q", stack=True),
                    color=alt.Color("项目:N", scale=alt.Scale(domain=names, range=palette),
                                    legend=alt.Legend(title=None, orient="right")),
                    tooltip=[alt.Tooltip("项目:N", title="持仓"),
                             alt.Tooltip("占比:Q", title="组合占比", format=".1%")],
                )
                .properties(height=230)
            )
            st.altair_chart(donut, width="stretch")
    elif snapshot["total"] > 1.0:
        st.info("持仓占比超过 100%，请修正比例后查看构成图。")
    render_stat_cards(
        [
            ("前两大持仓", f"{snapshot['top_two']:.0%}", "用于观察组合集中程度"),
            ("单标的上限", f"{snapshot['single_limit']:.0%}", "来自已确认的投资偏好"),
            ("待配置资金", f"{snapshot['unallocated']:.0%}", "按当前录入比例计算"),
            ("超限标的", str(len(snapshot["over_limit"])), "需要重点复核"),
        ]
    )
    chart, stress = st.columns([1.25, 1], gap="medium")
    with chart, st.container(border=True):
        st.markdown("**持仓权重分布**")
        chart_data = {
            "持仓": [item["name"] for item in snapshot["holdings"]],
            "组合占比": [item["weight"] * 100 for item in snapshot["holdings"]],
        }
        st.bar_chart(chart_data, x="持仓", y="组合占比", horizontal=True)
    with stress, st.container(border=True):
        st.markdown("**组合压力情景**")
        shock = st.slider(
            "假设所有已录入持仓同步下跌",
            min_value=1,
            max_value=30,
            value=10,
            format="%d%%",
            key="portfolio_stress_shock",
        )
        estimated_loss = snapshot["total"] * shock / 100
        st.metric("组合资产估算变化", f"-{estimated_loss:.1%}", border=True)
        st.caption("按所有持仓同比例变动机械计算，仅用于压力测试，不是价格预测。")
        if snapshot["over_limit"]:
            st.warning("超过单标的上限：" + "、".join(snapshot["over_limit"]))


def portfolio_stat_cards(portfolio: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """持仓概览：条数、合计占比与集中度，都是前端可复算的展示口径。"""

    total = sum(float(item.get("weight", 0)) for item in portfolio)
    largest = max(portfolio, key=lambda item: float(item.get("weight", 0)))
    limit = st.session_state.profile.get("single_security_limit") or 0.20
    cards = [
        ("持仓数量", f"{len(portfolio)}", "已加入组合的标的"),
        ("占比合计", f"{total:.0%}", "超过 100% 需要检查" if total > 1 else "在合理范围内"),
        ("第一大持仓", f"{float(largest.get('weight', 0)):.0%}",
         f"{largest.get('name', '—')} · 单标的上限 {float(limit):.0%}"),
    ]
    over = [str(item["name"]) for item in portfolio if float(item.get("weight", 0)) > float(limit)]
    if over:
        cards.append(("超过单标的上限", str(len(over)), "、".join(over)[:18]))
    return cards


def page_portfolio(api_base: str) -> None:
    """组合页只收集权重和发起诊断；任何调整建议均由后端合规层控制。"""
    st.title("持仓分析")
    render_page_header("持仓 · 集中度与资产分配")
    st.caption("录入持仓标的及权重，评估组合集中度与风险分布。")
    if st.session_state.portfolio:
        render_stat_cards(portfolio_stat_cards(st.session_state.portfolio))
    with st.form("portfolio_form"):
        name = st.text_input("股票或基金名称", placeholder="输入名称或代码", icon=":material/search:")
        weight = st.number_input("占总投资的比例（%）", min_value=0.1, max_value=100.0, value=10.0, step=1.0) / 100
        add_holding = st.form_submit_button("加入持仓", icon=":material/add:")
    if add_holding:
        if not name.strip():
            st.warning("请输入持仓名称。")
        else:
            holding = {"name": name.strip(), "weight": float(weight)}
            st.session_state.portfolio.append(holding)
            st.session_state.pop("portfolio_advice", None)
            add_fact(
                {
                    "fact_id": f"PORTFOLIO-{len(st.session_state.portfolio):03d}",
                    "entity": holding["name"],
                    "field": "weight",
                    "value": holding["weight"],
                    "snapshot_time": utc_now(),
                    "source_id": "USER_PORTFOLIO_SNAPSHOT",
                    "quality": 1.0,
                }
            )
            st.success("持仓已添加。")
    if st.session_state.portfolio:
        st.dataframe(
            [{"持仓": item["name"], "组合占比": f"{item['weight']:.1%}"} for item in st.session_state.portfolio],
            width="stretch",
            hide_index=True,
        )
        total = sum(item["weight"] for item in st.session_state.portfolio)
        if total > 1.0:
            st.warning("持仓占比超过 100%，请检查填写的比例。")
        render_portfolio_risk_dashboard(st.session_state.portfolio)
    else:
        render_empty_state("暂无持仓记录", "添加持仓标的后，可查看组合集中度并发起风险分析。",
                           ":material/pie_chart:")
    if st.button("分析我的持仓", type="primary", disabled=not st.session_state.portfolio or not profile_ready(),
                 icon=":material/analytics:"):
        previous_direction = st.session_state.get("active_research_direction", QUICK_ASKS[0])
        activate_research("持仓分析")
        try:
            result = run_analysis(api_base, "请诊断我的持仓组合")
        finally:
            activate_research(previous_direction)
        if result:
            st.session_state.portfolio_advice = result
    if st.session_state.get("portfolio_advice"):
        result = st.session_state.portfolio_advice
        render_advice(result, export_key="portfolio", question="请诊断我的持仓组合")
        if result.get("allocation"):
            st.subheader("资产分配参考")
            st.dataframe(
                [
                    {
                        "资产类别": item.get("asset_class", "—"),
                        "建议区间": f"{float(item.get('min_weight', 0)):.0%} — {float(item.get('max_weight', 0)):.0%}",
                        "说明": plain_language(item.get("basis", "仅作研究参考")),
                    }
                    for item in result["allocation"]
                ],
                width="stretch",
                hide_index=True,
            )


def _restore_profile(api_base: str) -> bool:
    """登录后恢复服务端保存的画像，避免每次重新登录都要重填问卷。"""

    result = take_account_result(api_base, "/profile")
    if result is PENDING:
        return False
    if result is NOT_PREFETCHED:
        result = api_request(api_base, "GET", "/profile", quiet=True)
    if not isinstance(result, dict) or "profile" not in result:
        # 旧版后端没有画像接口：保持未确认状态，用户主动填写即可。
        st.session_state.profile["confirmed"] = False
        return True
    profile = result["profile"]
    if isinstance(profile, dict):
        st.session_state.profile = profile
    st.session_state.profile.pop("user_id", None)
    if profile.get("confirmed"):
        note = (result.get("evidence") or ["已恢复上次保存的投资偏好。"])[-1]
        st.session_state.auth_notice = plain_language(str(note))
    return True


def reset_user_session() -> None:
    """退出时清理仅属于当前账号的浏览器状态。"""
    cancel_board_prefetch()
    cancel_account_prefetch()
    for job in st.session_state.get("analysis_jobs", {}).values():
        job["future"].cancel()
    for key in list(st.session_state):
        if key.startswith("snapshot_"):
            value = st.session_state.pop(key, None)
            if key.endswith("_task") and value is not None:
                value.cancel()
    session_id = current_browser_session_id()
    if session_id:
        browser_session_reclaimer().unregister(session_id)
    for key in (
        "service_unavailable", "last_error", "watchlist_compare_ids", "session_status_checked_at", "profile_restored",
        "auth_token", "session_beacon_token", "auth_user", "conversation", "advice", "facts", "portfolio",
        "watchlist", "watchlist_loaded", "watchlist_retry_at", "price_history", "watchlist_focus", "watchlist_comparison_request", "watchlist_comparison_advice", "comparison_chart_choice", "comparison_polling",
        "risk_assessment_index", "risk_assessment_answers", "risk_assessment_editing", "risk_assessment_view",
        "profile", "profile_draft", "profile_plan", "questionnaire", "portfolio_advice", "navigation", "preference_navigation_open", "pending_navigation",
        "history_view", "research_sessions", "active_research_direction", "research_direction", "conversation_directions", "research_board_cache",
        "research_results", "market_query_result", "data_selected", "auth_notice",
        "recent_conversations", "recent_conversations_loaded_at", "history_detail_cache",
        "history_before_id", "history_offset", "history_last_search", "history_search",
        "analysis_jobs", "analysis_notice",
    ):
        st.session_state.pop(key, None)
    for key in list(st.session_state):
        if key.startswith(("risk_score_", "risk_answer_", "research_chat_", "board_target_", "board_refresh_", "board_poll_", "admin_")) or key == "profile_narrative":
            st.session_state.pop(key, None)
    st.session_state.conversation_id = str(uuid.uuid4())
    # 账号标识只来自服务端令牌，浏览器状态里不保存用户 id。
    st.session_state.profile = {"risk_level": None, "risk_score": None,
                                "horizon_months": None, "max_drawdown": None, "liquidity_need": None,
                                "constraints": [], "target": None, "single_security_limit": 0.20,
                                "industry_limit": 0.30, "version": 1, "confirmed": False}


@st.fragment(run_every=2)
def poll_wechat_login_status(api_base: str) -> None:
    """只刷新扫码状态，避免轮询时反复加载微信二维码。"""
    pending = st.session_state.get("wechat_login_pending")
    if not pending:
        return
    result = request_json(backend_http_client(api_base), api_base, "POST", "/auth/wechat/poll",
                          {"poll_token": pending["poll_token"]}, None)
    if result.status == 200 and result.data.get("status") == "ready":
        st.session_state.pop("wechat_login_pending", None)
        complete_login(result.data["auth"])
        st.rerun()
    if result.status == 200 and result.data.get("status") == "failed":
        st.session_state.pop("wechat_login_pending", None)
        st.session_state.auth_notice = result.data.get("detail") or "微信登录未完成，请重新扫码。"
        st.rerun()
    if result.status == 410:
        st.session_state.pop("wechat_login_pending", None)
        st.session_state.auth_notice = "二维码已过期，请重新扫码。"
        st.rerun()


def render_wechat_login(api_base: str) -> None:
    """在原浏览器显示微信二维码；轮询密钥不进入二维码或页面 URL。"""
    pending = st.session_state.get("wechat_login_pending")
    if not pending:
        if st.button("使用微信扫码登录", width="stretch", icon=":material/qr_code_2:"):
            result = api_request(api_base, "POST", "/auth/wechat/start")
            if result:
                st.session_state.wechat_login_pending = result
                st.rerun()
        st.caption("请使用微信扫一扫。首次扫码将创建独立的普通账号。")
        return

    st.caption("使用微信扫一扫扫描下方二维码，并在手机上确认登录。")
    st.iframe(pending["authorization_url"], height=430)
    st.link_button("二维码无法显示？在新页面打开", pending["authorization_url"], width="stretch")
    if st.button("重新生成二维码", key="wechat_login_refresh", width="stretch"):
        st.session_state.pop("wechat_login_pending", None)
        st.rerun()
    poll_wechat_login_status(api_base)


def page_login(api_base: str) -> None:
    """登录门禁；注册成功后同样直接进入系统。"""
    render_login_brand()
    story, panel = st.columns([1.15, 1], gap="large")
    with panel, st.container(key="login-panel"):
        st.subheader("欢迎来到问策智投")
        st.caption("登录，开始您的投资研究。")
        with st.container(horizontal=True, gap="xsmall"):
            st.badge("资料可追溯", icon=":material/rule:", color="primary")
            st.badge("仅提供研究服务", icon=":material/block:", color="gray")
        notice = st.session_state.pop("auth_notice", None)
        if notice:
            st.warning(notice)
        login_tab, wechat_tab, register_tab = st.tabs(["账号登录", "微信扫码", "注册"], on_change="rerun")
        if login_tab.open:
            with login_tab:
                with st.form("login_form"):
                    username = st.text_input("账号", key="login_username")
                    password = st.text_input("密码", type="password", key="login_password")
                    submitted = st.form_submit_button("登录", type="primary", width="stretch")
                if submitted:
                    result = api_request(
                        api_base, "POST", "/auth/login", {"username": username.strip(), "password": password}
                    )
                    if result:
                        complete_login(result)
                        st.rerun()
        if wechat_tab.open:
            with wechat_tab:
                render_wechat_login(api_base)
        if register_tab.open:
            with register_tab:
                with st.form("register_form"):
                    username = st.text_input("新账号", help="3-50 位字母、数字、下划线或连字符")
                    password = st.text_input("新密码", type="password", help="至少 8 位")
                    confirmation = st.text_input("确认密码", type="password")
                    submitted = st.form_submit_button("注册并登录", type="primary", width="stretch")
                if submitted:
                    if password != confirmation:
                        st.error("两次输入的密码不一致。")
                    else:
                        result = api_request(
                            api_base, "POST", "/auth/register", {"username": username.strip(), "password": password}
                        )
                        if result:
                            complete_login(result)
                            st.rerun()
    with story:
        st.html("""<section class="login-story"><div class="eyebrow">投资研究与风险分析平台</div>
            <h1>系统研究，<br>审慎决策。</h1><p>结合市场数据与投资偏好，提供可追溯的研究结论和风险分析。</p>
            <div class="story-points">
            <div class="story-point"><span>01</span><div><strong>明确研究需求</strong><p>输入标的、研究范围与具体问题，启动系统分析。</p></div></div>
            <div class="story-point"><span>02</span><div><strong>核验结论与风险</strong><p>核对分析依据、数据来源与风险提示，辅助独立判断。</p></div></div>
            <div class="story-point"><span>03</span><div><strong>持续跟踪研究</strong><p>保存咨询记录与研究资料，支持后续复核与追踪。</p></div></div>
            </div></section>""")


def complete_login(result: dict[str, Any]) -> None:
    """保存登录令牌并重置本次浏览器会话的对话状态。

    账号归属由后端根据令牌判定，前端不保存、也不展示用户标识。
    """

    identity = {"username": result["user"]["username"], "role": result["user"].get("role", "user")}
    if (st.session_state.get("auth_token") == result["access_token"]
            and st.session_state.get("session_beacon_token") == result["session_beacon_token"]
            and st.session_state.get("auth_user") == identity):
        return
    reset_user_session()
    for key in ("service_unavailable", "pending_navigation", "watchlist_compare_ids", "profile_restored", "last_error"):
        st.session_state.pop(key, None)
    for key in ("research_sessions", "active_research_direction", "research_direction", "conversation_directions", "research_board_cache"):
        st.session_state.pop(key, None)
    st.session_state.navigation = "主页"
    st.session_state.auth_token = result["access_token"]
    st.session_state.session_beacon_token = result["session_beacon_token"]
    st.session_state.auth_user = identity
    # 登录响应已验证租约；首屏无需立即重复查询。
    st.session_state.session_status_checked_at = monotonic()
    st.session_state.conversation = []
    st.session_state.advice = None
    st.session_state.watchlist = []
    st.session_state.watchlist_loaded = False
    st.session_state.conversation_id = str(uuid.uuid4())
    st.session_state.history_view = False
    st.session_state.pop("recent_conversations", None)
    st.session_state.pop("recent_conversations_loaded_at", None)
    st.session_state.pop("history_detail_cache", None)
    st.session_state.history_before_id = None
    st.session_state.profile_restored = False
    if st.session_state.auth_user["role"] != "admin":
        prefetch_research_board_data(st.session_state.api_base)


def page_conversations(api_base: str) -> None:
    """按需查看历史会话，可搜索、重命名并加载更早消息。"""

    search = st.text_input("搜索对话名称", key="history_search", placeholder="输入对话标题关键词")
    offset = int(st.session_state.get("history_offset", 0))
    if search != st.session_state.get("history_last_search", ""):
        offset = 0
        st.session_state.history_offset = 0
        st.session_state.history_last_search = search
    path = f"/history?limit=21&offset={offset}&q={quote(search)}"
    page = api_request(api_base, "GET", path)
    if not isinstance(page, list):
        return
    histories = page[:20]
    if not histories:
        st.info("未检索到匹配的咨询记录。" if search else "暂无已保存的咨询记录。分析完成后，系统将自动保存。")
        if offset and st.button("上一页", key="history_empty_previous"):
            st.session_state.history_offset = max(0, offset - 20)
            st.rerun()
        return
    labels = {
        item["id"]: f"{item['title']} · {item['message_count']} 条消息 · {item['updated_at'][:19]}"
        for item in histories
    }
    selected_id = st.selectbox("选择历史会话", list(labels), format_func=lambda item_id: labels[item_id])
    previous, following = st.columns(2)
    if previous.button("上一页", disabled=offset == 0, key="history_previous"):
        st.session_state.history_offset = max(0, offset - 20)
        st.rerun()
    if following.button("下一页", disabled=len(page) <= 20, key="history_next"):
        st.session_state.history_offset = offset + 20
        st.rerun()
    detail = st.session_state.get("history_detail_cache")
    if not isinstance(detail, dict) or detail.get("id") != selected_id:
        detail = api_request(api_base, "GET", f"/history/{selected_id}")
        if isinstance(detail, dict):
            st.session_state.history_detail_cache = detail
    if not isinstance(detail, dict):
        return
    left, right = st.columns([3, 1])
    left.subheader(detail["title"])
    if right.button("恢复并继续对话", type="primary", width="stretch"):
        restore_conversation(detail)
        st.rerun()
    renamed = st.text_input("对话名称", value=detail["title"], key=f"rename_{selected_id}")
    if st.button("保存名称", key=f"save_name_{selected_id}", disabled=not renamed.strip()):
        updated = api_request(api_base, "PATCH", f"/history/{selected_id}", {"title": renamed})
        if isinstance(updated, dict):
            detail["title"] = updated["title"]
            invalidate_recent_conversations(api_base)
            st.rerun()
    if detail.get("next_before_id") and st.button("加载更早消息", key="history_load_older"):
        older = api_request(api_base, "GET", f"/history/{selected_id}?before_id={detail['next_before_id']}")
        if isinstance(older, dict):
            detail["messages"] = older.get("messages", []) + detail["messages"]
            detail["next_before_id"] = older.get("next_before_id")
            st.session_state.history_detail_cache = detail
            st.rerun()
    for message_index, message in enumerate(detail["messages"]):
        with st.chat_message(message["role"]):
            if message["role"] == "assistant" and message.get("payload"):
                previous = detail["messages"][message_index - 1] if message_index else {}
                render_advice(message["payload"], export_key=f"history_{message_index}",
                              question=previous.get("content", "") if previous.get("role") == "user" else "")
            else:
                st.write(plain_language(message["content"]) if message["role"] == "assistant" else message["content"])


def _render_service_unavailable(api_base: str) -> None:
    """后端暂时不可用时的兜底界面：保留侧栏，给出重试与退出入口。"""

    render_sidebar(api_base, full=False)
    st.warning("暂时连接不上分析服务，请稍后重试。")
    st.caption("您的登录状态仍保留在本机；服务恢复后重新打开页面即可继续。")


def main() -> None:
    """配置登录门禁、侧栏状态和产品页面。"""
    st.set_page_config(page_title="问策智投", page_icon=str(BRAND_LOGO), layout="wide")
    init_session()
    render_app_styles()
    # 登录页固定在一个可替换的槽位，切换登录态时先清理旧表单。
    login_screen = st.empty()
    if not st.session_state.get("auth_token"):
        with login_screen.container():
            page_login(st.session_state.api_base)
        return
    login_screen.empty()
    api_base = st.session_state.api_base
    # 登录响应已经保存了展示所需的账号信息；普通组件交互无需每次重跑都再查
    # 一遍数据库。旧会话缺少该字段时才补查，令牌有效性仍由下方定时片段校验。
    if not st.session_state.get("auth_user"):
        current_user = api_request(api_base, "GET", "/auth/me")
        if current_user is None:
            # 会话失效或后端不可用时保留侧栏，避免整页空白无从操作。
            _render_service_unavailable(api_base)
            return
        st.session_state.auth_user = current_user
    register_browser_session(api_base)
    enforce_session_timeout(api_base)
    if st.session_state.pop("service_unavailable", False):
        # 状态接口不查询用户表，比每次调用 /auth/me 更轻；连接失败时仍沿用原兜底页。
        _render_service_unavailable(api_base)
        return
    if st.session_state.auth_user.get("role") == "admin":
        from frontend.admin import render_admin
        render_admin(api_base, api_request, reset_user_session)
        return
    if not st.session_state.get("profile_restored"):
        st.session_state.profile_restored = _restore_profile(api_base)
    if st.session_state.get("account_prefetch"):
        poll_account_prefetch()
    if st.session_state.get("pending_navigation"):
        st.session_state.navigation = st.session_state.pop("pending_navigation")
        st.session_state.history_view = False
    # 旧浏览器会话可能仍保存着原来的导航值；将其迁移为隐藏的详情视图。
    if st.session_state.get("navigation") == "历史记录":
        st.session_state.navigation = "投资问答"
        st.session_state.history_view = True
    page = render_sidebar(api_base, full=True)
    if not st.session_state.get("profile_restored") and page in {"风险评估", "风险调整"}:
        st.info("正在恢复投资偏好，请稍候。", icon=":material/sync:")
        return
    if st.session_state.get("history_view"):
        page = "历史记录"
    if page in {"主页", "自选研究"}:
        ensure_watchlist_loaded(api_base)
    if page == "自选研究" and (api_base, "/watchlist") in st.session_state.get("account_prefetch", {}):
        render_page_header("自选研究")
        st.info("正在加载自选标的，请稍候。", icon=":material/sync:")
        return
    pages = {
        "主页": lambda: page_home(api_base),
        "投资问答": lambda: page_questions(api_base),
        "自选研究": lambda: page_watchlist(api_base),
        "持仓分析": lambda: page_portfolio(api_base),
        "历史记录": lambda: page_insights(api_base),
        "风险评估": lambda: page_profile(api_base),
        "风险调整": lambda: page_profile(api_base, view="风险调整"),
    }
    pages.get(page, lambda: page_home(api_base))()


def new_conversation() -> None:
    direction = st.session_state.get("research_direction", QUICK_ASKS[0])
    activate_research(direction)
    st.session_state.conversation = []
    st.session_state.advice = None
    st.session_state.conversation_id = str(uuid.uuid4())
    st.session_state.history_before_id = None
    st.session_state.history_view = False
    st.session_state.navigation = "投资问答"
    save_research_session()


if __name__ == "__main__":
    main()
