"""问策智投 Streamlit 前端。

本页面刻意不承担投资判断、数据抓取或合规决策：所有业务判断都通过 FastAPI
提交给后端。前端负责收集用户画像和可选事实快照，并把后端自动取数后返回的
``AdvicePackage`` 以可追溯、可解释的方式展示出来。

启动方式（先启动 backend，再在另一个终端执行）：

    streamlit run frontend/streamlit_app.py
"""

from __future__ import annotations

import math
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from ipaddress import ip_address
from pathlib import Path
from threading import Lock, Thread
from time import monotonic, sleep
from typing import Any
from urllib.parse import urlparse

import httpx
import streamlit as st
from streamlit.runtime import get_instance
from streamlit.runtime.scriptrunner import get_script_run_ctx


DEFAULT_API_BASE = os.getenv("WENCE_API_BASE", "http://127.0.0.1:8000/api/v1")
RISK_LEVELS = ["R1", "R2", "R3", "R4", "R5"]
PROFILE_LABELS = {
    "risk_level": "风险等级",
    "risk_score": "风险评分",
    "horizon_months": "投资期限",
    "max_drawdown": "最大可承受回撤",
    "liquidity_need": "流动性需求",
    "target": "投资目标",
    "single_security_limit": "单一标的上限",
    "industry_limit": "单一行业上限",
    "investment_experience_years": "投资经验",
    "expected_annual_return": "期望年化收益",
}
FIELD_LABELS = {
    "growth_score": "经济增长",
    "inflation_score": "通胀环境",
    "liquidity_score": "市场流动性",
    "policy_score": "政策环境",
    "risk_appetite_score": "风险偏好",
    "prosperity_score": "行业景气度",
    "valuation_score": "估值水平",
    "capital_flow_score": "资金流向",
    "crowding_score": "交易拥挤度",
    "fundamental_score": "基本面评分",
    "technical_score": "技术面评分",
    "fund_risk_level": "基金风险等级",
    "fund_score": "基金综合评分",
    "fee_rate": "费率",
    "weight": "持仓权重",
    "close_price": "最新价",
}


FIELD_LABELS.update({
    "change": "涨跌幅", "volume": "成交量", "turnover_rate": "换手率", "pe_ttm": "市盈率", "pb": "市净率",
    "roe": "净资产收益率", "revenue_growth": "营业收入增长率", "tracking_error": "跟踪误差",
    "news": "新闻", "announcement": "公告", "research_report": "研报", "provider_response": "查询摘要",
    "company_name": "公司全称", "industry": "所属行业", "main_business": "主营业务", "listing_date": "上市日期",
    "revenue_composition": "主营构成", "major_customer": "主要客户", "major_supplier": "主要供应商",
    "major_contract": "重大合同", "controlling_shareholder": "控股股东", "actual_controller": "实际控制人",
    "total_shares": "总股本", "float_shares": "流通股本", "shareholder_count": "股东人数",
    "event": "重要事件", "institution": "研究机构", "rating": "机构评级", "target_price": "目标价",
    "earnings_forecast": "盈利预测", "conversion_premium_rate": "转股溢价率", "pure_bond_premium_rate": "纯债溢价率",
    "yield_to_maturity": "到期收益率", "remaining_size": "剩余规模", "bond_rating": "债券评级", "conversion_price": "转股价",
    "cpi": "居民消费价格指数", "ppi": "工业生产者价格指数", "pmi": "采购经理指数",
    "social_financing": "社会融资", "interest_rate": "利率", "event_score": "事件影响评分", "governance_score": "公司治理评分",
})
NAVIGATION = ["投资问答", "查找资料", "持仓分析", "历史记录", "投资偏好"]
# 问答页只负责提问，资料页只负责取数与整理；两者通过这一入口互相跳转。
MATERIALS_PAGE = "查找资料"
DATA_KINDS = {"实时行情": "quote", "财务指标": "financial", "财经新闻": "news", "公告": "announcement",
              "研报": "research_report", "基金 / ETF": "fund", "行业排名": "industry", "可转债": "convertible"}
# 提问时的研究视角：由界面翻译成一句完整问题交给后端，用户不需要理解"智能体"。
QUICK_ASKS = ["市场解读", "行业分析", "个股研究", "基金筛选", "可转债分析"]
QUICK_ASK_PROMPTS = {
    "市场解读": "请解读当前市场环境、主要机会与风险",
    "行业分析": "请分析我关注行业的景气度与主要风险",
    "个股研究": "请分析这只股票的经营情况、估值与风险：",
    "基金筛选": "请帮我比较和筛选基金 / ETF：",
    "可转债分析": "请分析这只可转债的价格、对应股票与风险：",
}
# 把用户问题转成完整研究请求时使用的前缀，与后端意图识别保持一致。
RESEARCH_PREFIX = {"个股研究": "个股研究：", "行业分析": "行业分析：", "市场解读": "市场解读：",
                   "基金筛选": "基金筛选：", "可转债分析": "可转债分析："}
TOPIC_LABELS = {"market": "市场环境", "macro": "市场环境", "industry": "行业分析", "security": "个股研究",
                "stock": "个股研究", "fund": "基金筛选", "portfolio": "持仓分析", "fact_verifier": "资料核对", "compliance": "风险检查"}
PROGRESS_LABELS = {"completed": "已完成", "running": "正在分析", "pending": "等待处理", "degraded": "资料有限",
                   "unknown": "待补充资料", "failed": "暂未完成", "skipped": "本次无需处理"}


@st.cache_data(show_spinner=False)
def app_styles(asset_version: int) -> str:
    """读取随项目保存的样式表，不依赖临时路径或外部资源。

    页面底色改为 CSS 渐变：旧版把一张竖向渐变图拉伸到 100vh，会连正文区域
    一起染色，降低对比度；现在只在顶部留一层极淡的品牌余韵。
    """
    assets = Path(__file__).resolve().parent / "assets"
    return (assets / "app.css").read_text(encoding="utf-8")


def render_app_styles() -> None:
    assets = Path(__file__).resolve().parent / "assets"
    version = (assets / "app.css").stat().st_mtime_ns
    st.html(f"<style>{app_styles(version)}</style>")


def display_value(key: str, value: Any) -> str:
    """将服务端字段转换为面向用户的简洁文本，不暴露原始结构。"""

    if value is None or value == "":
        return "待补充"
    if key == "risk_level":
        return {"R1": "保守型", "R2": "稳健型", "R3": "平衡型", "R4": "成长型", "R5": "进取型"}.get(str(value), "待评估")
    if key in {
        "max_drawdown", "single_security_limit", "industry_limit", "expected_annual_return", "weight", "fee_rate"
    }:
        if isinstance(value, str) and value.strip().endswith(("%", "％")):
            return value.strip()
        try:
            return f"{float(value) * 100:g}%"
        except (ValueError, TypeError):
            return str(value)
    if key == "horizon_months":
        return f"{int(value)} 个月"
    if key == "investment_experience_years":
        return f"{float(value):g} 年"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, list):
        return "、".join(str(item) for item in value) or "无"
    return str(value)


def fact_label(field: str) -> str:
    return FIELD_LABELS.get(field) or (field if re.search(r"[\u4e00-\u9fff]", field) else "其他资料")


def fact_source(fact: dict[str, Any]) -> str:
    source = str(fact.get("source_id", ""))
    if source.startswith("USER_SUPPLIED:"):
        return "用户补充 · " + source.partition(":")[2]
    if fact.get("source_note"):
        return str(fact["source_note"])
    if source.startswith("IWENCAI"):
        return "同花顺问财"
    if source.startswith("DERIVED"):
        return "根据原始资料计算"
    if source == "DEMO_SNAPSHOT":
        return "示例数据"
    if source.startswith("USER_") or source == "MANUAL_SNAPSHOT":
        return "用户提供"
    return "市场数据服务" if source else "来源待确认"


def fact_time(value: Any) -> str:
    if not value:
        return "未提供"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        pass
    return str(value)


def fact_period(value: Any) -> str:
    """把报告期转成可读日期；内部逐条标识不展示给用户。"""

    text = str(value or "").strip()
    if not text:
        return "—"
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if re.fullmatch(r"\d{6}", text):
        return f"{text[:4]}-{text[4:]}"
    if text.startswith("REC-") or re.fullmatch(r"[A-Za-z0-9_\-]{8,}", text):
        return "近期"
    return text


def friendly_fact_rows(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """展示全部业务资料，新闻及未映射指标也保留，不暴露内部标识。"""
    rows = []
    for fact in facts:
        value = fact.get("value")
        if isinstance(value, dict):
            value = "；".join(f"{fact_label(str(key))}：{display_value(str(key), item)}"
                             for key, item in value.items() if key not in {"trace_id", "fact_id", "source_id", "quality"})
        rows.append({
            "对象": fact.get("entity", "—"), "指标": fact_label(str(fact.get("field", ""))),
            "内容 / 数值": display_value(str(fact.get("field", "")), value),
            "数据时间（北京时间）": fact_time(fact.get("snapshot_time")),
            "来源": fact_source(fact), "报告期": fact_period(fact.get("period")),
        })
    return rows


def advice_facts(advice: dict[str, Any]) -> list[dict[str, Any]]:
    # 显式空资料也属于该结果；只有旧格式缺少 facts 时兼容当前会话。
    return advice["facts"] if "facts" in advice else st.session_state.get("facts", [])


def render_profile_summary(profile: dict[str, Any]) -> None:
    labels = {"risk_level": "投资风格", "horizon_months": "计划投资多久", "max_drawdown": "最多接受亏损",
              "liquidity_need": "随时用钱的需要", "target": "投资目标", "expected_annual_return": "期望每年收益"}
    cards = "".join(
        f"<div class='profile-card'><div class='profile-card-label'>{label}</div>"
        f"<div class='profile-card-value'>{escape(display_value(key, profile.get(key)))}</div></div>"
        for key, label in labels.items()
    )
    st.html(f"<div class='profile-grid'>{cards}</div>")


def render_terminal_header() -> None:
    st.html("""<div class="brand"><div class="brand-mark">问</div>
        <div class="brand-name">问策智投</div><div class="brand-note">让每一次投资，多一分理解</div></div>""")


def render_brand_block() -> None:
    """侧栏品牌区：与主内容之间用一条细线分隔。"""

    st.html("""<div class="side-brand"><div class="mark">问</div>
        <div class="name">问策智投</div><div class="tag">投研助手</div></div>""")


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


def render_empty_state(title: str, detail: str, icon: str = ":material/inbox:") -> None:
    """带图标的空状态，替代一行裸提示。"""

    with st.container(border=True, horizontal_alignment="center"):
        st.markdown(icon)
        st.markdown(f"**{title}**")
        st.caption(detail)


def navigation_index() -> int:
    """当前所在入口在 NAVIGATION 中的下标，用于把旧状态映射到侧栏导航。"""

    current = st.session_state.get("navigation", NAVIGATION[0])
    return NAVIGATION.index(current) if current in NAVIGATION else 0


def page_navigation() -> str:
    """侧栏导航：整行胶囊，选中态用品牌色标出。

    控件键仍为 ``navigation``，这样"前往查找资料"等入口只需写
    ``st.session_state.navigation`` 就能在下一轮自动跳到目标页。
    """

    with st.container(key="nav"):
        choice = st.radio(
            "功能",
            NAVIGATION,
            index=navigation_index(),
            key="navigation",
            label_visibility="collapsed",
        )
    return str(choice)


def render_side_user(api_base: str) -> None:
    """侧栏账号卡：只展示账号名与偏好状态，不暴露任何后端标识。"""

    user = st.session_state.auth_user or {}
    username = escape(str(user.get("username", "")))
    ready = profile_ready()
    state = "投资偏好已确认" if ready else "待确认投资偏好"
    dot = "dot" if ready else "dot pending"
    st.html(
        f"<div class='side-user'><div class='who'>{username}</div>"
        f"<div class='state'><span class='{dot}'></span>{state}</div></div>"
    )


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
        render_side_label("研究")
        if st.button("开启新对话", width="stretch", type="primary", on_click=new_conversation,
                     icon=":material/add_comment:"):
            pass
        page = page_navigation()
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


@st.cache_resource
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
    """定时检查服务端租约；轮询本身不刷新用户活动时间。"""

    if st.session_state.get("auth_token"):
        api_request(api_base, "GET", "/auth/session/status")


def utc_now() -> str:
    """生成 API 要求的带时区 ISO-8601 时间，避免浏览器本地时区造成核验歧义。"""
    return datetime.now(timezone.utc).isoformat()


def init_session() -> None:
    """初始化浏览器会话；登录后的对话会另外持久化到 MySQL。"""
    st.session_state.setdefault(
        "profile",
        {
            "risk_level": "R3",
            "risk_score": None,
            "horizon_months": 24,
            "max_drawdown": 0.10,
            "liquidity_need": "中",
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
    try:
        headers = {}
        if st.session_state.get("auth_token"):
            headers["Authorization"] = f"Bearer {st.session_state.auth_token}"
        # 分析包含取数及理解、专业分析、复核三个模型阶段，客户端须覆盖整条链路。
        read_timeout = 240.0 if path == "/portfolio/analyze" else 65.0 if path == "/profile/assess" else 30.0
        with httpx.Client(timeout=httpx.Timeout(read_timeout, connect=10.0)) as client:
            response = client.request(method, f"{api_base}{path}", json=payload, headers=headers)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        if quiet:
            return None
        try:
            error_body = exc.response.json()
        except ValueError:
            error_body = {}
        detail = error_body.get("detail", "请求未成功，请稍后重试。")
        message = format_api_error(detail)
        st.error(message)
        if exc.response.status_code == 401 and path not in {"/auth/login", "/auth/register"}:
            reset_user_session()
            st.session_state.auth_notice = message
            st.rerun()
    except httpx.HTTPError:
        if not quiet:
            st.error("暂时连接不上分析服务，请稍后再试。")
    return None


def show_status(status: str) -> None:
    if status == "REVIEW":
        st.warning("这份分析还需要进一步确认，请勿直接据此买卖。", icon=":material/fact_check:")
    elif status != "PASS":
        st.error("暂时无法提供投资建议，请先查看下方原因。", icon=":material/report:")


def profile_ready() -> bool:
    """个性化分析只接受经确认画像；前端在调用前再次提示，但后端仍是最终闸门。"""
    return bool(st.session_state.profile.get("confirmed"))


def add_fact(fact: dict[str, Any]) -> None:
    """按 fact_id 替换同名快照，防止证据中心展示同一事实的重复版本。"""
    facts = [row for row in st.session_state.facts if row["fact_id"] != fact["fact_id"]]
    facts.append(fact)
    st.session_state.facts = facts


def plain_language(value: Any) -> str:
    """翻译已知技术词；保留数字、否定词、分歧和风险条件。"""
    text = str(value or "").strip()
    text = re.sub(r"协调器置信加权共识分为\s*[\d.]+。?", "", text)
    for code, label in {"market": "市场", "macro": "市场", "industry": "行业", "security": "个股", "stock": "个股",
                        "fund": "基金", "portfolio": "持仓", "fact_verifier": "数据核对", "compliance": "风险检查"}.items():
        text = re.sub(rf"\b{code}[：:]", f"{label}：", text)
    replacements = {
        "授权事实不足，暂不形成强结论。": "现有资料不足，暂时无法作出可靠判断。",
        "补齐各专业智能体列出的缺失字段": "补充所分析的股票、基金名称及相关资料，再重新分析",
        "补充有来源、含时间戳的事实后重新核验": "补充注明来源和日期的最新资料，再重新分析",
        "在执行任何调整前复核风险标记和证伪条件": "调整持仓前，先确认风险以及哪些变化会让结论不再适用",
        "关注证据时点与证伪条件，定期复核": "关注最新信息；情况变化时重新分析",
        "已确认画像": "投资偏好", "画像适配": "是否适合你", "画像": "投资偏好",
        "最大回撤": "最多可接受的阶段性亏损", "流动性需求": "随时用钱的需要",
        "基本面与技术面": "公司经营情况与短期价格走势", "证伪条件": "结论不再适用的情况",
        "授权事实": "已有资料", "事实不足": "资料不足", "证据不足": "资料不足",
        "安全降级": "仅作有限参考", "快照时点": "数据日期", "快照": "数据",
        "事实核验": "数据核对", "合规闸门": "风险检查", "适当性审核": "风险匹配检查",
    }
    replacements.update({
        "专业智能体评分分散度较高，协调器保留分歧并要求人工复核。": "不同分析的看法差异较大，仍需进一步确认。",
        "语义复核不可用或不确定，需要人工复核。": "部分判断尚未确认，请进一步核实后再作决定。",
        "组合已完成规则型集中度诊断；调整应分批执行并在新快照下复核。": "已检查持仓是否过于集中。如需调整，请分步进行，并根据最新持仓重新分析。",
        "单标的集中度超限": "某只股票或基金的占比超过了你设定的上限",
        "单标的上限": "单只股票或基金的比例上限",
        "可重算综合评分已生成；正反催化剂需随快照复核。": "已完成初步分析，利好和不利因素仍需结合最新资料确认。",
        "的可用研究维度已按授权快照汇总。": "的已有研究资料已整理，仍需关注最新变化。",
        "基于五个已授权宏观维度": "根据现有的经济、资金和政策资料",
        "证据不足，仅可展示教育性说明。": "资料不足，以下内容只帮助理解相关知识。",
    })
    for original, friendly in sorted(replacements.items(), key=lambda item: -len(item[0])):
        text = text.replace(original, friendly)
    for field, label in FIELD_LABELS.items():
        text = re.sub(rf"\b{re.escape(field)}\b", label, text)
    text = re.sub(r"\b(?:market|industry|security|fund|portfolio) 声称完成但没有通过核验的事实引用。", "部分分析缺少可核对的资料，暂时不能据此作出判断。", text)
    for code, name in zip(RISK_LEVELS, ["保守型", "稳健型", "平衡型", "成长型", "进取型"]):
        text = re.sub(rf"\b{code}\b", name, text)
    return text


def render_points(title: str, items: list[str], *, visible: int = 3) -> None:
    points = list(dict.fromkeys(plain_language(item) for item in items if item))
    if not points:
        return
    st.markdown(f"**{title}**")
    for point in points[:visible]:
        st.write(f"- {point}")
    if len(points) > visible:
        with st.expander(f"其余{title}（{len(points) - visible}项）"):
            for point in points[visible:]:
                st.write(f"- {point}")


def render_advice(advice: dict[str, Any]) -> None:
    """所有页面共享精简结果；风险与数据不足提示始终保留。"""
    compliance = advice.get("compliance", {})
    show_status(compliance.get("status", "REVIEW"))
    acquisition = advice.get("data_acquisition", {})
    if acquisition.get("mode") == "unavailable":
        st.warning("本次未能取得最新市场数据，分析可能不完整。")
    elif acquisition.get("failed_capabilities") or acquisition.get("empty_capabilities"):
        st.caption("部分资料暂未取得，相关判断仍需补充信息。")
    if acquisition.get("mode") == "demo" or any(f.get("source_id") == "DEMO_SNAPSHOT" for f in advice.get("facts", [])):
        st.warning("本结果含示例数据，仅用于演示。")
    if acquisition.get("reason_code") == "MODEL_UNAVAILABLE":
        st.caption("本次智能研判暂不可用，已改用可复算的规则口径给出结果。")
    elif acquisition.get("facts_truncated"):
        st.caption("本次资料较多，分析只选取了与问题最相关的部分，其余资料仍可在此查看。")
    if advice.get("snapshot_time"):
        st.caption(f"资料日期：{str(advice['snapshot_time'])[:10]}")
    facts = advice_facts(advice)
    used_ids = set(advice.get("evidence", []))
    used = [fact for fact in facts if fact.get("fact_id") in used_ids]
    with st.container(horizontal=True, gap="xsmall"):
        if compliance.get("status") == "PASS":
            st.badge("风险检查已通过", icon=":material/verified:", color="green")
        elif compliance.get("status") == "REVIEW":
            st.badge("需要人工复核", icon=":material/fact_check:", color="orange")
        else:
            st.badge("未形成结论", icon=":material/do_not_disturb:", color="gray")
        st.badge(f"引用 {len(used)} 条资料", icon=":material/rule:", color="primary" if used else "gray")
        if advice.get("agent_results"):
            st.badge(f"{len(advice['agent_results'])} 个分析维度", icon=":material/hub:", color="blue")
    st.markdown("**分析结论**")
    st.write(plain_language(advice.get("conclusion")) or "现有资料不足，暂时无法作出判断。")
    if compliance.get("status") != "PASS" and compliance.get("reason"):
        st.caption(plain_language(compliance["reason"]))
    issues = [item.get("message", "") for item in advice.get("cross_validation", {}).get("issues", [])]
    render_points("需要注意", [*advice.get("risks", []), *issues])
    render_points("接下来可以做", advice.get("next_steps", []))
    if advice.get("user_fit"):
        with st.expander("与你的投资偏好是否匹配", icon=":material/person_check:"):
            st.write(plain_language(advice["user_fit"]))
    if used:
        with st.expander(f"查看参考数据（{len(used)} 条）", icon=":material/table_view:"):
            st.dataframe(friendly_fact_rows(used), width="stretch", hide_index=True)
    if compliance.get("risk_notice"):
        st.caption(plain_language(compliance["risk_notice"]))


def run_analysis(api_base: str, query: str) -> dict[str, Any] | None:
    """携带最近多轮上下文调用统一分析端点，并更新会话历史。"""
    if not profile_ready():
        st.warning("请先在“投资偏好”中确认你的情况，再开始分析。")
        return None
    if not query.strip():
        st.warning("请先输入想了解的问题。")
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
    with st.spinner("正在查找资料、整理分析，请稍候…"):
        advice = api_request(api_base, "POST", "/portfolio/analyze", payload)
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
    return advice


def go_to(page: str) -> None:
    st.session_state.navigation = page


def home_stat_cards() -> list[tuple[str, str, str]]:
    """用会话里已有的资料生成概览卡片，不额外取数、不产生延迟。"""

    cards: list[tuple[str, str, str]] = []
    facts = st.session_state.get("facts", [])
    if facts:
        cards.append(("研究资料", str(len(facts)), "本次提问会一并参考"))
        latest = facts[-1]
        value = display_value(str(latest.get("field", "")), latest.get("value"))
        cards.append(("最近一条资料", value[:18], f"{latest.get('entity', '—')} · {fact_time(latest.get('snapshot_time'))}"))
    else:
        cards.append(("研究资料", "0", f"可在“{MATERIALS_PAGE}”中准备"))
    cards.append(("投资偏好", "已确认" if profile_ready() else "未确认",
                  "分析将按你的情况给出" if profile_ready() else "确认后即可开始分析"))
    completed = sum(
        1 for result in (st.session_state.get("advice") or {}).get("agent_results", [])
        if result.get("status") == "completed"
    )
    if completed:
        cards.append(("已完成分项", str(completed), "来自最近一次分析"))
    return cards


def page_home(api_base: str) -> None:
    render_terminal_header()
    render_page_header("投资问答 · 用日常语言提问")
    render_stat_cards(home_stat_cards(), accent_first=not st.session_state.facts)
    if not st.session_state.conversation:
        st.html("""<section class="hero"><div class="eyebrow">从一个问题开始</div>
            <h1>投资有疑问，<br>一起理清楚。</h1>
            <p>聊聊市场、基金或你的持仓。把复杂的信息，整理成看得懂的分析。</p></section>""")
        if not profile_ready():
            st.info("先花几分钟填写投资偏好，让分析更贴合你的情况。", icon=":material/person_edit:")
            st.button("填写投资偏好", type="primary", on_click=go_to, args=("投资偏好",))
        examples = ["现在的市场有哪些需要注意的风险？", "选择基金时，应该关注哪些方面？", "请帮我看看持仓是否过于集中"]
        st.markdown("**可以这样问**")
        for col, example in zip(st.columns(3), examples):
            if col.button(example, width="stretch", disabled=not profile_ready(),
                          icon=":material/help_outline:"):
                if run_analysis(api_base, example):
                    st.rerun()
    else:
        with st.container(horizontal=True, vertical_alignment="center"):
            st.markdown(f"**对话中 · {len(st.session_state.conversation)} 条消息**")
            st.space("stretch")
            if st.button("开启新对话", icon=":material/add_comment:"):
                # 导航控件本轮已创建，下一轮再切换，避免修改控件状态错误。
                st.session_state.conversation = []
                st.session_state.advice = None
                st.session_state.conversation_id = str(uuid.uuid4())
                st.rerun()
        for turn in st.session_state.conversation:
            with st.chat_message(turn["role"]):
                if turn["role"] == "assistant" and turn.get("payload"):
                    render_advice(turn["payload"])
                else:
                    st.write(plain_language(turn["content"]) if turn["role"] == "assistant" else turn["content"])
    render_quick_ask(api_base)
    query = st.chat_input("输入股票、基金名称，或直接说说你的疑问…", disabled=not profile_ready())
    if query and run_analysis(api_base, query.strip()):
        st.rerun()
    render_materials_summary()
    st.html("<div class='legal-strip'>分析仅供参考，不保证收益。投资前，请结合自己的情况判断。</div>")


def render_quick_ask(api_base: str) -> None:
    """把原来的"专题研究"并入问答页：选视角、填对象、直接提问。"""

    with st.expander("按研究方向提问", expanded=not st.session_state.conversation, icon=":material/travel_explore:"):
        st.caption("选一个方向，填写想了解的对象或问题，结果会出现在上面的对话里。")
        with st.form("quick_ask"):
            kind = st.segmented_control("研究方向", QUICK_ASKS, default=QUICK_ASKS[0], key="quick_ask_kind")
            target = st.text_input("关注的对象或想了解的问题",
                                   placeholder=QUICK_ASK_PROMPTS[kind or QUICK_ASKS[0]], max_chars=200)
            submitted = st.form_submit_button("开始分析", type="primary", disabled=not profile_ready(),
                                              icon=":material/play_arrow:")
        if submitted:
            chosen = kind or QUICK_ASKS[0]
            question = target.strip() or QUICK_ASK_PROMPTS[chosen]
            if run_analysis(api_base, RESEARCH_PREFIX[chosen] + question):
                st.rerun()


def render_materials_summary() -> None:
    """问答页只提示资料是否已备好；取数、补充和整理都在“查找资料”页完成。"""

    count = len(st.session_state.facts)
    if count:
        st.caption(f"本次提问会参考“{MATERIALS_PAGE}”中的 {count} 条资料。")
    else:
        st.caption(f"还没有研究资料；可以在“{MATERIALS_PAGE}”里查询行情、新闻或补充自己的资料。")
    st.button(f"前往{MATERIALS_PAGE}", icon=":material/library_books:", on_click=go_to, args=(MATERIALS_PAGE,))


def page_materials(api_base: str) -> None:
    """独立的资料页：只负责取数和整理资料，不承载对话。"""

    st.title(MATERIALS_PAGE)
    render_page_header("研究资料库 · 取数与整理")
    st.caption("查询行情、新闻与公告，或补充自己的资料；整理结果会自动用于投资问答。")
    total = len(st.session_state.facts)
    query_tab, manual_tab, list_tab = st.tabs([
        "查询资料", "补充资料", f"已有资料（{total}）" if total else "已有资料",
    ])
    with query_tab:
        with st.form("material_query"):
            kind_label = st.selectbox("想查什么", list(DATA_KINDS))
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
                    preserve_analysis_materials()
                    for fact in result.get("facts", []):
                        add_fact(fact)
                    if result.get("facts"):
                        st.success(f"已加入 {len(result['facts'])} 条资料，可以回到“投资问答”提问了。")
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
            render_empty_state("还没有研究资料", "可以在“查询资料”或“补充资料”中先添加。",
                               ":material/library_add:")
            return
        search = st.text_input("在资料里查找", placeholder="按名称、指标或内容搜索", icon=":material/search:")
        visible = [fact for fact in facts if search.casefold() in
                   f"{fact.get('entity', '')} {fact_label(str(fact.get('field', '')))} {fact.get('value', '')}".casefold()]
        if visible:
            st.caption(f"共 {len(facts)} 条资料，当前显示 {len(visible)} 条。")
            st.dataframe(friendly_fact_rows(visible), width="stretch", hide_index=True)
        else:
            render_empty_state("没有匹配的资料", "换一个名称、指标或关键词再试。", ":material/search_off:")
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


def page_profile(api_base: str) -> None:
    """画像中心：将抽取、用户核对和确认做成不可跳过的两步流程。"""
    st.title("投资偏好")
    render_page_header("投资偏好 · 目标与承受能力")
    st.caption("告诉我们你的目标与承受能力，核对后即可开始分析。")
    current = st.session_state.profile
    with st.form("profile_assessment"):
        narrative = st.text_area(
            "你的投资计划",
            placeholder="例如：我投资 3 年，2 年后买房，最多接受 8% 亏损，期望年化收益 8%。",
        )
        profile_left, profile_right = st.columns(2)
        experience_years = profile_left.number_input(
            "投资经验（年）",
            min_value=0.0,
            max_value=100.0,
            value=float(current.get("investment_experience_years") or 0),
            step=0.5,
        )
        expected_return_percent = profile_right.number_input(
            "希望每年获得的收益（%，不代表保证收益）",
            min_value=-100.0,
            max_value=500.0,
            value=float(current.get("expected_annual_return") or 0) * 100,
            step=0.5,
        )
        investment_history_text = st.text_area(
            "投资历史（每行一项）",
            value="\n".join(current.get("investment_history", [])),
            placeholder="例如：2024 年开始定投宽基 ETF",
        )
        with st.expander("了解你对风险的态度", expanded=True):
            st.caption("0 表示完全不符合，100 表示完全符合。请按真实情况选择。")
            questionnaire = {
                "financial_capacity": st.slider("即使这笔投资亏损，也不影响日常生活", 0, 100, 50),
                "loss_tolerance": st.slider("我能接受投资暂时亏损", 0, 100, 50),
                "investment_horizon": st.slider("这笔钱可以长期不用", 0, 100, 50),
                "knowledge_experience": st.slider("我了解股票、基金的收益和风险", 0, 100, 50),
                "behavior_stability": st.slider("市场下跌时，我仍能冷静判断", 0, 100, 50),
            }
        create_draft = st.form_submit_button("查看评估结果", type="primary")
    if create_draft:
        st.session_state.questionnaire = questionnaire
        result = api_request(
            api_base,
            "POST",
            "/profile/assess",
            {
                "narrative": narrative or None,
                "questionnaire": questionnaire,
                "investment_experience_years": experience_years,
                "investment_history": [line.strip() for line in investment_history_text.splitlines() if line.strip()],
                "holding_history": st.session_state.portfolio,
                "expected_annual_return": expected_return_percent / 100,
            },
        )
        if result:
            st.session_state.profile = result["profile"]
            st.session_state.profile["confirmed"] = False
            st.session_state.profile_draft = result

    draft = st.session_state.get("profile_draft")
    if draft:
        st.subheader("请核对你的投资偏好")
        render_profile_summary(draft["profile"])
        if draft.get("missing_fields"):
            st.warning(f"仍缺少：{'、'.join(PROFILE_LABELS.get(key, '投资计划') for key in draft['missing_fields'])}")
        if st.button("确认并保存", type="primary"):
            confirmed = api_request(api_base, "POST", "/profile/confirm", {"profile": st.session_state.profile})
            if confirmed:
                st.session_state.profile = confirmed
                # 关闭草稿卡片，避免用户在刷新前重复点击导致版本无意义地连续递增。
                st.session_state.pop("profile_draft", None)
                st.success("投资偏好已保存，可以开始提问了。")
                st.rerun()

    if draft and draft.get("evidence"):
        with st.expander("查看评估依据"):
            for item in draft["evidence"]:
                st.write(plain_language(item))
    with st.expander("更多投资偏好"):
        profile = st.session_state.profile
        st.write(f"单只股票或基金的比例上限：{display_value('single_security_limit', profile.get('single_security_limit'))}")
        st.write(f"单个行业的比例上限：{display_value('industry_limit', profile.get('industry_limit'))}")
        if profile.get("constraints"):
            render_points("你的限制条件", profile["constraints"])
    if not draft:
        status = "已确认" if profile_ready() else "未确认"
        with st.expander(f"当前投资偏好 · {status}", expanded=profile_ready()):
            render_profile_summary(st.session_state.profile)


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
        st.caption("本次还没有分项分析。")
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
    render_page_header("回看 · 对话与分析详情")
    st.caption("查看已保存的对话，或回看某次分析的观点、依据与完成情况。")
    history_tab, analysis_tab = st.tabs(["对话记录", "分析详情"])
    with history_tab:
        page_conversations(api_base)
    with analysis_tab:
        render_analysis_details()


def render_analysis_details() -> None:
    entries = available_analyses()
    if not entries:
        render_empty_state("还没有分析记录", "完成一次问答或持仓分析后，就可以在这里查看。",
                           ":material/history:")
        return
    index = st.selectbox("选择一次分析", list(range(len(entries))),
                         format_func=lambda i: f"{i + 1}. {entries[i][0][:70]}")
    _, advice = entries[index]
    views, evidence, progress = st.tabs(["分析观点", "数据依据", "完成情况"])
    with views:
        show_status(advice.get("compliance", {}).get("status", "REVIEW"))
        render_professional_views(advice)
        render_points("不同观点与待核实问题", [i.get("message", "") for i in advice.get("cross_validation", {}).get("issues", [])])
    with evidence:
        facts = advice_facts(advice)
        used_ids = set(advice.get("evidence", []))
        used = [f for f in facts if f.get("fact_id") in used_ids]
        if used:
            st.caption("以下是这次分析实际引用的资料。")
            st.dataframe(friendly_fact_rows(used), width="stretch", hide_index=True)
        else:
            render_empty_state("没有已引用的资料", "这次分析暂时没有可展示的已引用资料。",
                               ":material/rule:")
        unused = [f for f in facts if f.get("fact_id") not in used_ids]
        if unused:
            with st.expander("未被这次分析引用的资料"):
                st.caption("可能与当前问题无关，或日期、来源尚待确认；未引用不代表资料错误。")
                st.dataframe(friendly_fact_rows(unused), width="stretch", hide_index=True)
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
            st.caption("本次还没有可展示的分项记录。")


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
    st.caption("填入你持有的股票或基金，看看资金是否过于集中。")
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
    else:
        render_empty_state("还没有持仓", "在上方添加股票或基金后，就可以查看集中度并开始分析。",
                           ":material/pie_chart:")
    if st.button("分析我的持仓", type="primary", disabled=not st.session_state.portfolio or not profile_ready(),
                 icon=":material/analytics:"):
        result = run_analysis(api_base, "请诊断我的持仓组合")
        if result:
            st.session_state.portfolio_advice = result
    if st.session_state.get("portfolio_advice"):
        result = st.session_state.portfolio_advice
        render_advice(result)
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


def _restore_profile(api_base: str) -> None:
    """登录后恢复服务端保存的画像，避免每次重新登录都要重填问卷。"""

    result = api_request(api_base, "GET", "/profile", quiet=True)
    if not isinstance(result, dict) or "profile" not in result:
        # 旧版后端没有画像接口：保持未确认状态，用户主动填写即可。
        st.session_state.profile["confirmed"] = False
        return
    profile = result["profile"]
    if isinstance(profile, dict):
        st.session_state.profile = profile
    st.session_state.profile.pop("user_id", None)
    if profile.get("confirmed"):
        note = (result.get("evidence") or ["已恢复上次保存的投资偏好。"])[-1]
        st.session_state.auth_notice = plain_language(str(note))


def reset_user_session() -> None:
    """退出时清理仅属于当前账号的浏览器状态。"""
    session_id = current_browser_session_id()
    if session_id:
        browser_session_reclaimer().unregister(session_id)
    for key in (
        "auth_token", "session_beacon_token", "auth_user", "conversation", "advice", "facts", "portfolio",
        "profile", "profile_draft", "questionnaire", "portfolio_advice", "navigation", "pending_navigation",
        "research_results", "market_query_result", "data_selected", "auth_notice",
    ):
        st.session_state.pop(key, None)
    st.session_state.conversation_id = str(uuid.uuid4())
    # 账号标识只来自服务端令牌，浏览器状态里不保存用户 id。
    st.session_state.profile = {"risk_level": "R3", "risk_score": None,
                                "horizon_months": 24, "max_drawdown": 0.10, "liquidity_need": "中",
                                "constraints": [], "target": None, "single_security_limit": 0.20,
                                "industry_limit": 0.30, "version": 1, "confirmed": False}


def page_login(api_base: str) -> None:
    """登录门禁；注册成功后同样直接进入系统。"""
    render_terminal_header()
    story, panel = st.columns([1.15, 1], gap="large")
    with story:
        st.html("""<section class="login-story"><div class="eyebrow">你的投资研究伙伴</div>
            <h1>看懂投资，<br>从容做选择。</h1><p>从一个简单的问题开始，了解市场变化，梳理投资思路。</p>
            <div class="story-points">
            <div class="story-point"><span>01</span><div><strong>说出你的疑问</strong><p>股票、基金、市场，用日常语言直接提问。</p></div></div>
            <div class="story-point"><span>02</span><div><strong>看懂分析与风险</strong><p>重点清楚，依据可查，帮助你独立判断。</p></div></div>
            <div class="story-point"><span>03</span><div><strong>随时接着聊</strong><p>保存研究记录，让每次思考都有迹可循。</p></div></div>
            </div></section>""")
    with panel, st.container(key="login-panel"):
        st.subheader("欢迎来到问策智投")
        st.caption("登录，开始你的投资研究。")
        with st.container(horizontal=True, gap="xsmall"):
            st.badge("资料可追溯", icon=":material/rule:", color="primary")
            st.badge("不下单", icon=":material/block:", color="gray")
        notice = st.session_state.pop("auth_notice", None)
        if notice:
            st.warning(notice)
        login_tab, register_tab = st.tabs(["登录", "注册"])
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


def complete_login(result: dict[str, Any]) -> None:
    """保存登录令牌并重置本次浏览器会话的对话状态。

    账号归属由后端根据令牌判定，前端不保存、也不展示用户标识。
    """

    st.session_state.auth_token = result["access_token"]
    st.session_state.session_beacon_token = result["session_beacon_token"]
    st.session_state.auth_user = {"username": result["user"]["username"]}
    st.session_state.conversation = []
    st.session_state.advice = None
    st.session_state.conversation_id = str(uuid.uuid4())
    st.session_state.profile_restored = False


def page_conversations(api_base: str) -> None:
    """查看当前登录用户的历史会话，并可恢复任意一轮继续对话。"""

    histories = api_request(api_base, "GET", "/history")
    if histories is None:
        return
    if not histories:
        st.info("还没有已保存的对话。完成一次分析后会自动出现在这里。")
        return
    labels = {
        item["id"]: f"{item['title']} · {item['message_count']} 条消息 · {item['updated_at'][:19]}"
        for item in histories
    }
    selected_id = st.selectbox("选择历史会话", list(labels), format_func=lambda item_id: labels[item_id])
    detail = api_request(api_base, "GET", f"/history/{selected_id}")
    if not detail:
        return
    left, right = st.columns([3, 1])
    left.subheader(detail["title"])
    if right.button("恢复并继续对话", type="primary", width="stretch"):
        st.session_state.conversation_id = detail["id"]
        st.session_state.conversation = [
            {"role": message["role"], "content": message["content"], "created_at": message["created_at"],
             "payload": message.get("payload")}
            for message in detail["messages"]
        ]
        assistant_messages = [message for message in detail["messages"] if message["role"] == "assistant"]
        st.session_state.advice = assistant_messages[-1].get("payload") if assistant_messages else None
        st.session_state.pending_navigation = "投资问答"
        st.rerun()
    for message in detail["messages"]:
        with st.chat_message(message["role"]):
            if message["role"] == "assistant" and message.get("payload"):
                render_advice(message["payload"])
            else:
                st.write(plain_language(message["content"]) if message["role"] == "assistant" else message["content"])


def _render_service_unavailable(api_base: str) -> None:
    """后端暂时不可用时的兜底界面：保留侧栏，给出重试与退出入口。"""

    render_sidebar(api_base, full=False)
    render_terminal_header()
    st.warning("暂时连接不上分析服务，请稍后重试。")
    st.caption("你的登录状态仍保留在本机；服务恢复后重新打开页面即可继续。")


def main() -> None:
    """配置登录门禁、侧栏状态和产品页面。"""
    st.set_page_config(page_title="问策智投", page_icon=":material/query_stats:", layout="wide")
    init_session()
    render_app_styles()
    if not st.session_state.get("auth_token"):
        page_login(st.session_state.api_base)
        return
    api_base = st.session_state.api_base
    current_user = api_request(api_base, "GET", "/auth/me")
    if current_user is None:
        # 会话失效或后端不可用时保留侧栏，避免整页空白无从操作。
        _render_service_unavailable(api_base)
        return
    st.session_state.auth_user = current_user
    register_browser_session(api_base)
    enforce_session_timeout(api_base)
    if not st.session_state.get("profile_restored"):
        st.session_state.profile_restored = True
        _restore_profile(api_base)
    if st.session_state.get("pending_navigation"):
        st.session_state.navigation = st.session_state.pop("pending_navigation")
    page = render_sidebar(api_base, full=True)
    pages = {
        "投资问答": lambda: page_home(api_base),
        MATERIALS_PAGE: lambda: page_materials(api_base),
        "持仓分析": lambda: page_portfolio(api_base),
        "历史记录": lambda: page_insights(api_base),
        "投资偏好": lambda: page_profile(api_base),
    }
    pages.get(page, lambda: page_home(api_base))()


def new_conversation() -> None:
    st.session_state.conversation = []
    st.session_state.advice = None
    st.session_state.conversation_id = str(uuid.uuid4())
    st.session_state.navigation = "投资问答"


if __name__ == "__main__":
    main()
