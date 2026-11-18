"""问策智投后端的 FastAPI 应用入口。

本文件只负责三件事：创建 Web 应用、装配主协调智能体的依赖，以及把 HTTP
请求交给协调器。意图识别、Task DAG、事实核验和合规判断均保留在各自模块，
避免 API 入口变成难以测试和维护的业务代码集合。

本版本以确定性规则作为可重算基线，并可通过环境变量接入第三方大模型和同花顺
问财数据。默认分析入口会按意图自动获取最小必要的只读数据并转换为
``FactRecord``；外部能力异常或未配置时，系统会明确回退到规则/快照模式。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from contextvars import ContextVar
from collections.abc import Callable
from contextlib import asynccontextmanager
from contextlib import suppress
from datetime import date, datetime, timedelta, timezone
from time import perf_counter, monotonic
from sqlalchemy.exc import SQLAlchemyError
from backend.app.services.context_compression import prepare_request_context, HISTORY_MESSAGES
from backend.app.services.rag import configured_store, DocumentUpload, SearchRequest, DocumentState
from backend.app.services.history import cited_facts_of
from backend.app.services.model_telemetry import analysis_telemetry, summarize_costs
from backend.app.services.local_consultation import LocalConsultationService, select_scope
from backend.app.answer_text import clean_advice
from backend.app.services.instant_research import InstantResearchService
from backend.app.services.trend_analytics import analyze_trend
from backend.app.services.technical_indicators import history_bars
from backend.app.services.research_recovery import recover_research
from backend.app.services.stock_recommendation import STOCK_RECOMMENDATION_TIMEOUT_SECONDS, StockRecommendationService
from backend.app.services.automatic_consultation import run_automatic, source_item
from backend.app.services.automatic_planning import AutomaticPlanningRequest, build_automatic_planning
from backend.app.services.personalized_investment import PersonalizedInvestmentRequest, build_personalized_investment
from backend.app.models.auth_schemas import UserAppearanceUpdate
from backend.app.services.prepared_research import (PreparedResearchCache, PreparedResearchRequest, board_signature, report_is_ready, task_query)
from backend.app.services.research_admission import ResearchAdmission
from backend.app.services.dependency_health import DatabaseHealthProbe
from backend.app.services.research_cache import ResearchFactCache
from threading import Event

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi import Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from dotenv import load_dotenv

from backend.app.agents.coordinator import (
    CoordinatorAgent,
    basic_compliance_check,
    verify_facts,
)
from backend.app.agents.llm_agents import LLMConfig, OpenAICompatibleLLM, make_investment_agents
from backend.app.semantic import RequestUnderstanding, SemanticService
from backend.app.research_routing import DIRECTION_FOR_INTENT, PROFILE_STOCK_RECOMMENDATION_QUERY
from backend.app.data_provider import IwencaiSkillHubProvider
from backend.app.auth import TokenError, create_access_token, decode_access_token, hash_password, verify_password
from backend.app.database import (
    Database,
    DatabaseUnavailable,
    ProfileVersionConflict,
    UsernameExists,
    WatchlistCapacityExceeded,
    WatchlistItemExists,
)
from backend.app.models import (
    AdvicePackage,
    AuthResponse,
    ConversationDetail,
    ConversationRename,
    ConversationSummary,
    Credentials,
    DataFetchRequest,
    DataFetchResponse,
    PriceHistoryRequest,
    PriceHistoryPoint,
    PriceHistoryResponse,
    ReportExportRequest,
    Intent,
    OrchestrationRequest,
    ProfileAssessment,
    ProfileAssessmentRequest,
    ProfileConfirmRequest,
    UserProfile,
    UserSummary,
    WatchlistItem,
    WatchlistItemCreate,
)
from backend.app.services import (
    AutomatedResearchPipeline,
    ServiceMetrics,
    assess_profile,
    confirm_profile,
    summarise_advice,
    used_fact_ids_of,
)
from backend.app.models.schemas import (
    DataAcquisitionResult,
    DataOverviewRequest,
    DataOverviewResponse,
    ComparisonRequest,
    ComparisonResponse,
    SnapshotOptions,
    WatchlistQuotesRequest,
    GroundingCheckRequest,
    PortfolioPlanRequest,
)
from backend.app.services.comparison import fetch_snapshots
from backend.app.models.index_board import IndexBoardResponse
from backend.app.services.security_names import resolve_security_identity
from backend.app.services.fund_analytics import FundAnalyticsRequest, fetch_fund_analytics
from backend.app.services.research_report import research_report_markdown
from backend.app.risk_questionnaire import assessment_is_current
from backend.app.models.trade_history import TradeHistoryUpload
from backend.app.services.trade_history import inspect_trade_workbook, analyze_trade_history
from backend.app.services.overview_cache import OverviewCache
overview_cache = OverviewCache()
prepared_research_cache = PreparedResearchCache()
from backend.app.services.index_board import IndexBoardCache
index_board_cache = IndexBoardCache()
from backend.app.services.admin import consultation_metadata
from backend.app.services.call_tracking import calling_user_id
from backend.app.services.login_guard import login_throttle
from backend.app.services.monitoring import RequestMetricsMiddleware
from backend.app.services.numeric_grounding import check_text_grounding, summarize_grounding
from backend.app.services.portfolio_optimizer import (
    OptimizationConstraints,
    build_portfolio_plan,
    price_series_from_points,
)
from backend.app.services.prometheus_metrics import render_prometheus
from backend.app.session_pool import (
    SessionCapacityExceeded,
    SessionLeaseExpired,
    SessionThreadPool,
)
from backend.app.wechat_login import WechatLogin


# 从本地 .env 加载可选外部服务配置；生产环境中已有的环境变量优先。
load_dotenv()
rag_service = configured_store()
database = Database.from_env()
session_thread_pool = SessionThreadPool.from_env(str(Path(__file__).resolve().parents[2] / ".tmp_flow/login_sessions.sqlite3"))
wechat_login = WechatLogin.from_env()


async def reap_idle_sessions() -> None:
    """定期回收超过空闲时限且没有在途请求的登录会话。"""

    interval = min(30.0, max(1.0, session_thread_pool.idle_timeout_seconds / 2))
    while True:
        await asyncio.sleep(interval)
        await asyncio.to_thread(session_thread_pool.reap_expired)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """准备数据表并启动空闲会话清理任务。"""

    await asyncio.to_thread(database.initialize, backfill=False)
    stop_backfill = Event()

    async def backfill_history():
        try:
            await asyncio.to_thread(database.backfill_consultations, stop_backfill)
        except Exception:
            logging.getLogger(__name__).warning("历史咨询统计补齐失败，可在下次启动时重试")

    history_backfill = asyncio.create_task(backfill_history())
    reaper = asyncio.create_task(reap_idle_sessions())
    try:
        yield
    finally:
        stop_backfill.set()
        await history_backfill
        reaper.cancel()
        with suppress(asyncio.CancelledError):
            await reaper
        await prepared_research_cache.aclose()
        await overview_cache.aclose()
        await index_board_cache.aclose()
        await research_pipeline.cache.aclose()
        from backend.app.services.disclosure_reader import close_document_caches
        await close_document_caches()
        closers = []
        llm = getattr(coordinator.semantic, "llm", None)
        if llm is not None and hasattr(llm, "aclose"):
            closers.append(llm.aclose())
        if data_provider is not None and hasattr(data_provider, "aclose"):
            closers.append(data_provider.aclose())
        if closers:
            await asyncio.gather(*closers)


def build_coordinator() -> tuple[CoordinatorAgent, bool]:
    """装配主协调智能体及当前运行模式所需的专业能力。

    注册表的键必须与 ``CoordinatorAgent._specialists_for()`` 返回的 agent_id
    对应。之后替换为真实专业智能体时，只更换这里注入的 handler；API 路由与
    编排协议均不需要改动。
    """

    # ``make_rule_agents`` 返回与 Task DAG 完全一致的五个注册键。它们不是行情源，
    # 只对请求已经授权的 FactRecord 做确定性计算，便于单测和审计回放。
    config = LLMConfig.from_env()
    client = OpenAICompatibleLLM(config) if config else None
    agents, llm_enabled = make_investment_agents(client)
    coordinator = CoordinatorAgent(
        agents=agents,
        # 事实核验先于合规审核执行，二者都可在后续替换为正式服务实现。
        verifier=verify_facts,
        compliance_checker=basic_compliance_check,
        semantic=SemanticService(client),
    )
    return coordinator, llm_enabled


# 供 Uvicorn 加载的 ASGI 应用对象。--reload 时也会重新创建并装配协调器。
app = FastAPI(
    title="问策智投 API",
    version="1.0.0",
    description="投资研究辅助演示接口；不自动交易，不构成证券投资建议。",
    lifespan=lifespan,
)

# 应用级单例。后续接数据库/Redis 时可改为 lifespan 管理。
coordinator, llm_enabled = build_coordinator()
data_provider = IwencaiSkillHubProvider.from_env()
research_pipeline = AutomatedResearchPipeline(data_provider, cache=ResearchFactCache(
    shared_path=os.getenv("WENCE_RESEARCH_CACHE_PATH", str(Path(__file__).resolve().parents[2] / ".tmp_flow/public_research_cache.sqlite3")) or None,
    lease_seconds=float(os.getenv("WENCE_DATA_CALL_TIMEOUT_SECONDS", "30")) + 5,
))
service_metrics = ServiceMetrics()
database_health_probe = DatabaseHealthProbe()
local_consultation = LocalConsultationService()
instant_research = InstantResearchService()
local_research_admission = ResearchAdmission(
    limit=int(os.getenv("WENCE_LOCAL_RESEARCH_MAX_CONCURRENCY", "16")),
    queue_seconds=float(os.getenv("WENCE_LOCAL_RESEARCH_QUEUE_SECONDS", "0.75")),
)
research_admission = ResearchAdmission()
deep_research_admission = ResearchAdmission(
    limit=int(os.getenv("WENCE_DEEP_RESEARCH_MAX_CONCURRENCY", "2")),
    queue_seconds=float(os.getenv("WENCE_DEEP_RESEARCH_QUEUE_SECONDS", "1800")),
    max_pending=int(os.getenv("WENCE_DEEP_RESEARCH_MAX_PENDING", "100")),
)
analysis_timeout_seconds = float(os.getenv("WENCE_ANALYSIS_TIMEOUT_SECONDS", "90"))
if not 0 < analysis_timeout_seconds <= 300:
    raise ValueError("整次研究的时间预算须在 0 至 300 秒之间")
preparation_timeout_seconds = float(os.getenv("WENCE_PREPARE_TIMEOUT_SECONDS", "180"))
preparation_recovery_seconds = float(os.getenv("WENCE_PREPARE_RECOVERY_SECONDS", "120"))
if not 0 < preparation_timeout_seconds <= 300 or not 0 < preparation_recovery_seconds <= 120:
    raise ValueError("预生成预算须不超过300秒，预生成补取预算须不超过120秒")
# Leave 0.4 seconds of the three-second response target for transport and the
# page's 0.2-second local completion check. Legacy online research is separate.
fast_response_timeout_seconds = float(os.getenv("WENCE_FAST_RESPONSE_TIMEOUT_SECONDS", "2.6"))
if not 0 < fast_response_timeout_seconds <= 2.6:
    raise ValueError("3秒限时研究的后端预算须大于0且不超过2.6秒")
DEEP_STREAM_HEARTBEAT_SECONDS = 15.0
iwencai_tracking_errors = 0


async def record_iwencai_attempt(event: dict[str, object]) -> None:
    """统计写入异常只影响统计完整性，不改变现有投资研究与取数逻辑。"""
    global iwencai_tracking_errors
    try:
        await asyncio.to_thread(database.record_iwencai_call, event)
    except Exception:
        iwencai_tracking_errors += 1
        logging.getLogger(__name__).warning("问财调用统计写入失败")


if data_provider is not None:
    data_provider.call_observer = record_iwencai_attempt

analysis_progress: ContextVar[Callable[[str], None] | None] = ContextVar("analysis_progress", default=None)


def _report_progress(stage: str) -> None:
    callback = analysis_progress.get()
    if callback is not None:
        callback(stage)


def _decode_session_token(token: str) -> dict[str, object]:
    """校验登录令牌，并返回其中的用户与会话标识。"""

    database.require_ready()
    assert database.auth_secret is not None
    return decode_access_token(token, database.auth_secret)


async def optional_authenticated_user(authorization: str | None = Header(default=None)):
    """解析可选令牌并在请求期间固定对应租约。"""

    if not authorization:
        yield None
        return
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="请使用 Bearer 登录令牌")
    session_id: str | None = None
    context_token = None
    try:
        payload = _decode_session_token(authorization.split(" ", 1)[1].strip())
        session_id = str(payload["sid"])
        leased_user_id = await asyncio.to_thread(session_thread_pool.acquire, session_id)
        if leased_user_id != int(payload["sub"]):
            raise TokenError("登录状态无效")
        future = session_thread_pool.submit(session_id, database.get_user, int(payload["sub"]))
        user = await asyncio.wrap_future(future)
        if not user:
            raise TokenError("登录账号不存在")
        user["_session_id"] = session_id
        context_token = calling_user_id.set(int(user["id"]))
        yield user
    except DatabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (TokenError, SessionLeaseExpired) as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    finally:
        if context_token is not None:
            calling_user_id.reset(context_token)
        if session_id is not None:
            await asyncio.to_thread(session_thread_pool.finish, session_id)


def authenticated_user(
    user: dict[str, object] | None = Depends(optional_authenticated_user),
) -> dict[str, object]:
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def admin_user(user: dict[str, object] = Depends(authenticated_user)) -> dict[str, object]:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可以访问管理后台")
    return user


def customer_user(user: dict[str, object] = Depends(authenticated_user)) -> dict[str, object]:
    if user.get("role") == "admin":
        raise HTTPException(status_code=403, detail="管理员账号仅用于管理后台")
    return user


def optional_customer_user(user: dict[str, object] | None = Depends(optional_authenticated_user)):
    if user and user.get("role") == "admin":
        raise HTTPException(status_code=403, detail="管理员账号仅用于管理后台")
    return user


def auth_response_for(user: dict[str, object]) -> AuthResponse:
    """分配会话槽并签发与该槽绑定的登录令牌。"""

    database.require_ready()
    assert database.auth_secret is not None
    try:
        session_id, beacon_token = session_thread_pool.allocate(int(user["id"]))
    except SessionCapacityExceeded as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        token, expires_at = create_access_token(
            int(user["id"]),
            str(user["username"]),
            database.auth_secret,
            session_id=session_id,
        )
        return AuthResponse(
            access_token=token,
            session_beacon_token=beacon_token,
            expires_at=expires_at,
            user=UserSummary.model_validate(user),
        )
    except Exception:
        session_thread_pool.release(session_id, user_id=int(user["id"]))
        raise


app.add_middleware(RequestMetricsMiddleware, metrics=service_metrics)


@app.get("/api/v1/health", tags=["system"])
async def health() -> dict[str, str]:
    """健康检查接口，供浏览器、部署平台和 README 的启动验证使用。"""

    return {"status": "ok"}


@app.get("/api/v1/readiness", tags=["system"])
async def readiness() -> dict[str, object]:
    """报告外部能力配置；未配置 LLM 时自然语言处理降级为澄清。"""

    return {
        "status": "ready",
        "mode": "hybrid_llm" if llm_enabled else "rule_only",
        "third_party_llm_configured": llm_enabled,
        "language_processing": "llm" if llm_enabled else "unavailable",
        "iwencai_skillhub_configured": data_provider is not None,
        "automatic_data_pipeline_enabled": True,
        "research_cache": {"shared_same_host": research_pipeline.cache.shared is not None,
                           "error": research_pipeline.cache.shared_error},
        "research_concurrency_per_process": research_admission.limit,
        "deep_research": {"active_limit": deep_research_admission.limit,
            "waiting_limit": deep_research_admission.max_pending,
            "waiting": deep_research_admission.pending,
            "queue_budget_seconds": deep_research_admission.queue_seconds,
            "three_second_target": False},
        "analysis_timeout_seconds": analysis_timeout_seconds,
        "deep_overall_timeout_seconds": None,
        "fast_response_timeout_seconds": fast_response_timeout_seconds,
        "default_consultation_mode": "lite",
        "default_page_consultation_mode": "instant",
        "instant_research": {"budget_seconds": fast_response_timeout_seconds, "online_calls": False,
                             "scope": "verified_loaded_evidence_only"},
        "prepared_reports": {"preparation_timeout_seconds": preparation_timeout_seconds,
            "recovery_timeout_seconds": preparation_recovery_seconds, "delivery_budget_seconds": 2.6},
        "research_models": {
            "deep": getattr(getattr(coordinator.semantic.llm, "config", None), "model", None),
            "fast": (getattr(getattr(coordinator.semantic.llm, "config", None), "fast_model", None)
                     or getattr(getattr(coordinator.semantic.llm, "config", None), "model", None)),
        },
        "model_pricing_configured": bool(getattr(getattr(coordinator.semantic, "llm", None), "config", None)
            and coordinator.semantic.llm.config.input_price_per_million is not None
            and coordinator.semantic.llm.config.output_price_per_million is not None),
        "mysql_configured": database.configured,
        "mysql_ready": database.configured and database.initialization_error is None,
        "session_thread_pool": session_thread_pool.snapshot(),
    }


@app.get("/api/v1/readyz", tags=["system"])
async def dependency_readiness(response: Response) -> dict[str, object]:
    """Load balancer readiness; configuration alone is not a database heartbeat."""
    db_ready = await database_health_probe.check(database)
    ready = db_ready and llm_enabled and data_provider is not None
    response.status_code = 200 if ready else 503
    return {"status": "ready" if ready else "unavailable", "database_reachable": db_ready,
            "model_configured": llm_enabled, "data_provider_configured": data_provider is not None,
            "external_model_and_data_liveness_verified": False}


@app.post("/api/v1/auth/register", response_model=AuthResponse, tags=["auth"])
def register(credentials: Credentials) -> AuthResponse:
    """创建账号并直接返回登录令牌；密码只以 scrypt 哈希形式进入 MySQL。"""

    try:
        user = database.create_user(credentials.username, hash_password(credentials.password))
        return auth_response_for(user)
    except UsernameExists as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DatabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/v1/auth/login", response_model=AuthResponse, tags=["auth"])
def login(credentials: Credentials, request: Request) -> AuthResponse:
    """验证账号密码并签发有过期时间、不可篡改的登录令牌。

    同一账号来自同一来源的连续失败会被短暂锁定，锁定期返回 429 + Retry-After，
    避免被在线猜口令；成功登录立即清零，不影响正常用户。
    """

    client = request.client.host if request.client else "unknown"
    throttle_key = login_throttle.key(credentials.username, client)
    waiting = login_throttle.retry_after(throttle_key)
    if waiting:
        raise HTTPException(
            status_code=429,
            detail=f"登录失败次数过多，请在 {waiting} 秒后重试。",
            headers={"Retry-After": str(waiting)},
        )
    try:
        user = database.get_user_by_username(credentials.username)
    except DatabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not user or not verify_password(credentials.password, str(user["password_hash"])):
        locked_for = login_throttle.failure(throttle_key)
        if locked_for:
            raise HTTPException(
                status_code=429,
                detail=f"登录失败次数过多，请在 {locked_for} 秒后重试。",
                headers={"Retry-After": str(locked_for)},
            )
        raise HTTPException(status_code=401, detail="账号或密码错误")
    login_throttle.success(throttle_key)
    user.pop("password_hash", None)
    return auth_response_for(user)


@app.post("/api/v1/auth/wechat/start", tags=["auth"])
def start_wechat_login() -> dict[str, str]:
    """为当前浏览器签发只在服务端保存的扫码状态和独立轮询凭据。"""
    try:
        database.require_ready()
        return wechat_login.start()
    except (ValueError, DatabaseUnavailable) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


@app.get("/api/v1/auth/wechat/callback", response_class=HTMLResponse, tags=["auth"])
def wechat_login_callback(state: str = Query(max_length=128), code: str = Query(default="", max_length=512)) -> HTMLResponse:
    """微信回调只完成身份绑定；登录令牌由原浏览器凭轮询密钥领取。"""
    if not wechat_login.begin_callback(state):
        raise HTTPException(status_code=400, detail="扫码请求已过期或无效，请返回登录页重试")
    if not code:
        wechat_login.finish_callback(state, error="微信授权已取消，请重新扫码")
        message = "微信授权已取消，请返回登录页重试。"
    else:
        try:
            openid = wechat_login.exchange_code(code)
            user = database.get_or_create_wechat_user(openid)
            wechat_login.finish_callback(state, user_id=int(user["id"]))
            message = "微信验证成功，请返回登录页。"
        except Exception:
            # httpx 异常可能包含带 AppSecret 的请求 URL，日志只记录固定文案。
            logging.getLogger(__name__).warning("微信登录回调失败")
            wechat_login.finish_callback(state, error="微信登录未完成，请重新扫码")
            message = "微信登录未完成，请返回登录页重试。"
    return HTMLResponse(f"<html><meta charset='utf-8'><body><p>{message}</p></body></html>",
                        headers={"Cache-Control": "no-store"})


@app.post("/api/v1/auth/wechat/poll", tags=["auth"])
def poll_wechat_login(poll_token: str = Body(embed=True, min_length=32, max_length=128)) -> dict[str, object]:
    """一次性领取普通账号会话；二维码中的 state 不能用于领取令牌。"""
    status, user_id, error = wechat_login.poll(poll_token)
    if status == "expired":
        raise HTTPException(status_code=410, detail="二维码已过期，请重新扫码")
    if status == "failed":
        return {"status": "failed", "detail": error}
    if status == "ready" and user_id is not None:
        try:
            user = database.get_user(user_id)
            if user is None or user.get("role") != "user":
                raise HTTPException(status_code=403, detail="该账号不能使用微信登录")
            return {"status": "ready", "auth": auth_response_for(user).model_dump(mode="json")}
        except DatabaseUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": "pending"}


@app.get("/api/v1/auth/me", response_model=UserSummary, tags=["auth"])
def current_user(user: dict[str, object] = Depends(authenticated_user)) -> UserSummary:
    return UserSummary.model_validate(user)


@app.patch("/api/v1/auth/me", response_model=UserSummary, tags=["auth"])
def update_current_user(request: UserAppearanceUpdate, user: dict[str, object] = Depends(authenticated_user)) -> UserSummary:
    updated = database.update_user_appearance(int(user["id"]), request.model_dump(exclude_unset=True))
    return UserSummary.model_validate(updated)


@app.get("/api/v1/auth/session/status", tags=["auth"])
def session_status(authorization: str | None = Header(default=None)) -> dict[str, bool]:
    """检查会话是否仍有效，但不把轮询本身算作用户活动。"""

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="请先登录")
    try:
        payload = _decode_session_token(authorization.split(" ", 1)[1].strip())
    except DatabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except TokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if not session_thread_pool.is_active(str(payload["sid"]), user_id=int(payload["sub"])):
        raise HTTPException(status_code=401, detail="登录已失效或闲置超过 10 分钟，请重新登录")
    return {"active": True}


@app.post("/api/v1/auth/logout", tags=["auth"])
def logout(user: dict[str, object] = Depends(authenticated_user)) -> dict[str, str]:
    """显式退出并立即释放当前登录占用的会话槽。"""

    prepared_research_cache.cancel_session(str(user["_session_id"]))
    session_thread_pool.release(str(user["_session_id"]), user_id=int(user["id"]))
    return {"status": "logged_out"}


async def _beacon_token(request: Request) -> str | None:
    """读取浏览器发送的最小权限会话回收凭据。"""

    body = await request.body()
    if not body or len(body) > 4096:
        return None
    try:
        return body.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None


@app.post("/api/v1/auth/logout/beacon", status_code=204, tags=["auth"])
async def session_logout_beacon(request: Request) -> Response:
    """在浏览器页面关闭时以幂等方式释放会话槽。"""

    beacon_token = await _beacon_token(request)
    if beacon_token is not None:
        session_thread_pool.release_beacon(beacon_token)
    return Response(status_code=204)


def _statistics_since(days: int) -> datetime | None:
    return datetime.now(timezone.utc) - timedelta(days=days) if days else None


@app.get("/api/v1/admin/users", tags=["admin"])
def admin_users(
    days: int = Query(default=0, ge=0, le=3650),
    q: str = Query(default="", max_length=50),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    user: dict[str, object] = Depends(admin_user),
) -> dict[str, object]:
    return database.admin_users(session_thread_pool.online_users(), since=_statistics_since(days),
                                search=q, offset=offset, limit=limit)


@app.get("/api/v1/admin/statistics", tags=["admin"])
def admin_statistics(
    days: int = Query(default=0, ge=0, le=3650),
    user: dict[str, object] = Depends(admin_user),
) -> dict[str, object]:
    return {**database.admin_statistics(since=_statistics_since(days)),
            "online_users": len(session_thread_pool.online_users()),
            "online_idle_seconds": session_thread_pool.idle_timeout_seconds}


@app.get("/api/v1/admin/users/{username}/consultations", tags=["admin"])
def admin_consultations(
    username: str,
    days: int = Query(default=0, ge=0, le=3650),
    user: dict[str, object] = Depends(admin_user),
) -> dict[str, object]:
    target_user = database.get_user_by_username(username.lower())
    if not target_user:
        raise HTTPException(status_code=404, detail="账号不存在")
    since = _statistics_since(days)
    return {**database.admin_statistics(since=since, user_id=int(target_user["id"])),
            "recent": database.admin_recent_consultations(int(target_user["id"]), since)}


@app.get("/api/v1/admin/iwencai", tags=["admin"])
def admin_iwencai(
    days: int = Query(default=0, ge=0, le=3650),
    user: dict[str, object] = Depends(admin_user),
) -> dict[str, object]:
    return {**database.admin_iwencai_statistics(_statistics_since(days)),
            "tracking_errors_since_start": iwencai_tracking_errors}


@app.get("/api/v1/history", response_model=list[ConversationSummary], tags=["history"])
def list_history(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
    q: str = Query(default="", max_length=100),
    user: dict[str, object] = Depends(customer_user),
) -> list[ConversationSummary]:
    return [ConversationSummary.model_validate(row) for row in database.list_conversations(
        int(user["id"]), limit, offset, q,
    )]


@app.get("/api/v1/history/{conversation_id}", response_model=ConversationDetail, tags=["history"])
def history_detail(
    conversation_id: str,
    before_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=40, ge=1, le=100),
    user: dict[str, object] = Depends(customer_user),
) -> ConversationDetail:
    result = database.get_conversation(int(user["id"]), conversation_id, before_id, limit)
    if not result:
        raise HTTPException(status_code=404, detail="对话记录不存在")
    return ConversationDetail.model_validate(result)


@app.patch("/api/v1/history/{conversation_id}", tags=["history"])
def rename_history(
    conversation_id: str,
    request: ConversationRename,
    user: dict[str, object] = Depends(customer_user),
) -> dict[str, str]:
    if not database.rename_conversation(int(user["id"]), conversation_id, request.title):
        raise HTTPException(status_code=404, detail="对话记录不存在")
    return {"id": conversation_id, "title": request.title}


@app.get("/api/v1/watchlist", response_model=list[WatchlistItem], tags=["watchlist"])
def list_watchlist(user: dict[str, object] = Depends(customer_user)) -> list[WatchlistItem]:
    """返回当前账号的自选标的。"""

    return [WatchlistItem.model_validate(row) for row in database.list_watchlist(int(user["id"]))]


@app.post("/api/v1/watchlist", response_model=WatchlistItem, tags=["watchlist"])
async def add_watchlist_item(
    request: WatchlistItemCreate,
    user: dict[str, object] = Depends(customer_user),
) -> WatchlistItem:
    """新增自选标的，并尽力把代码补全为数据源核实的证券简称。"""

    existing = await asyncio.to_thread(database.list_watchlist, int(user["id"]))
    if any(item["asset_type"] == request.asset_type and
           str(item["target"]).upper() == request.target.upper() for item in existing):
        raise HTTPException(status_code=409, detail="该标的已在自选中")
    identity = await resolve_security_identity(data_provider, request.target, request.asset_type)
    target = request.target
    name = identity[0] if identity else None
    if identity and not re.fullmatch(r"\d{5,6}(?:\.[A-Za-z]{2})?", target):
        target = identity[1]
    # 同代码的名称输入与代码输入共用一个自选项。
    if any(item["asset_type"] == request.asset_type and
           str(item["target"]).upper().split(".")[0] == target.upper().split(".")[0] for item in existing):
        raise HTTPException(status_code=409, detail="该标的已在自选中")
    try:
        row = await asyncio.to_thread(
            database.add_watchlist_item, int(user["id"]), target, request.asset_type, name)
    except WatchlistItemExists as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WatchlistCapacityExceeded as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return WatchlistItem.model_validate(row)


@app.delete("/api/v1/watchlist/{item_id}", tags=["watchlist"])
def remove_watchlist_item(
    item_id: int,
    user: dict[str, object] = Depends(customer_user),
) -> dict[str, str]:
    """删除当前账号自己的自选项。"""

    if not database.remove_watchlist_item(int(user["id"]), item_id):
        raise HTTPException(status_code=404, detail="自选标的不存在或已被移除")
    return {"status": "removed"}


@app.get("/api/v1/metrics", tags=["system"])
async def metrics() -> dict[str, object]:
    """返回容量与可用性观测值；其范围不冒充生产 SLA。"""

    return {
        **service_metrics.snapshot(),
        "session_thread_pool": session_thread_pool.snapshot(),
        "login_throttle": login_throttle.snapshot(),
    }


@app.get("/api/v1/metrics/prometheus", tags=["system"])
async def prometheus_metrics() -> Response:
    """同一份观测数据的 Prometheus 文本格式，供抓取与告警面板使用。"""

    snapshot = service_metrics.snapshot()
    return Response(
        content=render_prometheus(snapshot, session_pool=session_thread_pool.snapshot()),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.post("/api/v1/data/fetch", response_model=DataFetchResponse, tags=["data"])
async def fetch_market_data(request: DataFetchRequest, user: dict[str, object] | None = Depends(optional_customer_user)) -> DataFetchResponse:
    """从问财只读接口调用项目允许的 SkillHub 能力并标准化为事实。"""

    if data_provider is None:
        return DataFetchResponse(
            provider="IWENCAI_SKILLHUB",
            fetched_at=datetime.now(timezone.utc),
            status="unavailable",
            message="尚未配置 IWENCAI_API_KEY，请在 .env 中配置只读密钥并重启后端。",
        )
    try:
        handlers = {
            "quote": lambda: data_provider.get_quote(request.target),
            "financial": lambda: data_provider.get_financial_metrics(request.target),
            "news": lambda: data_provider.get_news(request.target),
            "fund": lambda: data_provider.get_fund_candidates(request.filters),
            "industry": lambda: data_provider.get_industry_rank(request.target),
            "convertible": lambda: data_provider.get_convertible_bond(request.target),
            "basic_info": lambda: data_provider.get_basic_info(request.target),
            "company_operations": lambda: data_provider.get_company_operations(request.target),
            "shareholder_equity": lambda: data_provider.get_shareholder_equity(request.target),
            "event": lambda: data_provider.get_event_data(request.target),
            "macro": lambda: data_provider.get_macro_data(request.target),
            "institutional_research": lambda: data_provider.get_institutional_research(request.target),
            "research_report": lambda: data_provider.get_research_reports(request.target),
            "announcement": lambda: data_provider.get_announcements(request.target),
            "stock_screen": lambda: data_provider.screen_stocks(request.target),
            "sector_screen": lambda: data_provider.screen_sectors(request.target),
        }
        facts = await handlers[request.kind]()
    except RuntimeError as exc:
        # 单个外部数据源不可用不是整套研究服务故障。返回显式状态和空事实，
        # 由界面提示用户如何恢复；不得把失败伪装成“没有搜索结果”。
        return DataFetchResponse(
            provider=data_provider.source_id,
            fetched_at=datetime.now(timezone.utc),
            status="unavailable",
            message=str(exc),
        )
    return DataFetchResponse(
        provider=data_provider.source_id,
        fetched_at=datetime.now(timezone.utc),
        facts=facts,
    )


@app.post("/api/v1/data/overview", response_model=DataOverviewResponse, tags=["data"])
async def research_data_overview(
    request: DataOverviewRequest, user: dict[str, object] = Depends(customer_user),
) -> DataOverviewResponse:
    """返回研究页面的公开事实概览；不修改用户画像或咨询上下文。"""
    result = await overview_cache.get(data_provider, request)
    # Warm deterministic calculations as data arrives, before a question is sent.
    # Raw facts alone are returned; every analysis still verifies them afresh.
    facts = [fact for section in result.sections if section.status == "ok" for fact in section.facts]
    if facts:
        token = analysis_telemetry.set({"cache_scope": str(user["id"])})
        try:
            research_pipeline._derive(facts)
            local_consultation.pipeline._derive(facts)
            instant_research.pipeline._derive(facts)
        finally:
            analysis_telemetry.reset(token)
    return result


@app.post("/api/v1/data/fund-analytics", tags=["data"])
async def fund_analytics(request: FundAnalyticsRequest, user: dict[str, object] = Depends(customer_user)):
    """统一窗口与参数的长期复利观察，不生成个性化投资建议。"""
    return await fetch_fund_analytics(overview_cache, data_provider, request)


@app.post("/api/v1/data/compare", response_model=ComparisonResponse, tags=["data"])
async def compare_data(request: ComparisonRequest, user: dict[str, object] = Depends(customer_user)):
    return await fetch_snapshots(overview_cache, data_provider,
        [(target, request.asset_type, None) for target in request.targets],
        refresh=request.refresh, decide=True)


@app.post("/api/v1/data/watchlist-quotes", response_model=ComparisonResponse, tags=["data"])
async def watchlist_quotes(request: WatchlistQuotesRequest, user: dict[str, object] = Depends(customer_user)):
    items = await asyncio.to_thread(database.list_watchlist, int(user["id"]))
    if request.ids is None:
        items = items[:20]  # 兼容旧客户端；分页客户端显式提交当前页ID。
    else:
        owned = {int(item["id"]): item for item in items}
        if not set(request.ids) <= owned.keys():
            raise HTTPException(status_code=404, detail="部分自选标的已移除，请刷新自选列表。")
        items = [owned[item_id] for item_id in request.ids]
    result = await fetch_snapshots(overview_cache, data_provider,
        [(item["target"], item["asset_type"], item["id"]) for item in items], wait=request.wait, refresh=request.refresh)
    # 界面优先显示数据源核实名称；取不到时用添加自选时保存的名称。
    saved = {int(item["id"]): item.get("name") for item in items}
    for entry in result.items:
        entry.display_name = entry.entity or saved.get(entry.watchlist_id) or None
    return result


@app.post("/api/v1/data/price-history", response_model=PriceHistoryResponse, tags=["data"])
async def price_history(
    request: PriceHistoryRequest,
    user: dict[str, object] = Depends(customer_user),
) -> PriceHistoryResponse:
    """为已登录用户按需读取真实日期序列；不把走势当作投资结论。"""

    del user
    metric_label = {"基金": "单位净值", "指数": "收盘点位", "行业": "行业指数"}.get(
        request.asset_type, "收盘价")
    common = {
        "target": request.target, "asset_type": request.asset_type,
        "metric_label": metric_label, "fetched_at": datetime.now(timezone.utc),
    }
    if data_provider is None:
        return PriceHistoryResponse(
            **common, status="unavailable",
            message="尚未配置问财访问密钥，暂时无法加载历史走势。",
        )
    try:
        load_history = getattr(data_provider, "get_technical_history", data_provider.get_price_history)
        facts = await load_history(
            request.target, request.asset_type, limit=request.limit,
        )
    except RuntimeError as exc:
        return PriceHistoryResponse(**common, status="unavailable", message=str(exc))
    points = [PriceHistoryPoint(**bar) for bar in history_bars(facts)]
    urls = {fact.source_url for fact in facts if fact.source_url}
    analysis = analyze_trend(points) if len(points) >= 2 else None
    return PriceHistoryResponse(
        **common,
        status="ok" if len(points) >= 2 else "empty",
        points=points if len(points) >= 2 else [],
        ma5=analysis.ma5_series if analysis else [],
        analysis=analysis,
        source_url=next(iter(urls)) if len(urls) == 1 else None,
        message=None if len(points) >= 2 else "问财暂未返回足够的带日期数值，无法绘制走势。",
    )


@app.post("/api/v1/market/index-board", response_model=IndexBoardResponse, tags=["data"])
async def market_index_board(
    request: SnapshotOptions, user: dict[str, object] = Depends(customer_user),
) -> IndexBoardResponse:
    """主要指数的行情、五日均线、三日升幅与中和判断；固定模板取数。"""

    del user
    return await index_board_cache.get(data_provider, refresh=request.refresh)


@app.post("/api/v1/portfolio/plan", tags=["advice"])
async def plan_portfolio(
    request: PortfolioPlanRequest,
    user: dict[str, object] = Depends(customer_user),
) -> dict[str, object]:
    """组合诊断与再平衡：约束投影 + 三种候选权重 + 相关性/风险贡献。

    全部为确定性算术（风险平价、逆波动、相关性、行业暴露），不调用大模型，
    因此结果可复现、可解释，也不会因为模型波动给出漂移的仓位建议。
    缺少历史序列的标的按给定波动率或等权处理，并在 ``warnings`` 里写明。
    """

    started = perf_counter()
    weights: dict[str, float] = {}
    holding_rows: dict[str, dict[str, object]] = {}
    unknown_weight = False
    for item in request.holdings:
        if item.weight is not None:
            weight = float(item.weight)
        elif item.shares and item.price:
            weight = float(item.shares) * float(item.price)
        else:  # 表模型已拦截，这里只是防御性兜底
            unknown_weight = True
            continue
        weights[item.symbol] = weights.get(item.symbol, 0.0) + weight
        # 同一标的重复填报时合并权重，而不是在组合里出现两次。
        row = holding_rows.setdefault(item.symbol, {"symbol": item.symbol, "name": item.name,
                                                    "industry": item.industry, "volatility": item.volatility})
        row["name"] = row["name"] or item.name
        row["industry"] = row["industry"] or item.industry
        row["volatility"] = row["volatility"] if row["volatility"] is not None else item.volatility
    if unknown_weight:
        raise HTTPException(status_code=422, detail="每条持仓需要 weight，或同时提供 shares 与 price。")
    if not weights:
        raise HTTPException(status_code=422, detail="请至少提供一条带权重的持仓。")

    series: dict[str, list[float]] = {}
    if request.include_price_history and data_provider is not None:
        async def load(symbol: str) -> None:
            try:
                facts = await data_provider.get_price_history(
                    symbol, request.asset_type, limit=request.price_history_days)
            except Exception:  # 单个标的取不到序列只降级为波动率近似，不中断整个组合
                return
            points = [{"date": str(f.period), "value": f.value} for f in facts if f.period]
            values = price_series_from_points(points)
            if len(values) >= 20:
                series[symbol] = values

        await asyncio.gather(*(load(symbol) for symbol in weights))

    constraints = OptimizationConstraints(
        single_limit=request.single_limit if request.single_limit is not None else 0.20,
        industry_limit=request.industry_limit if request.industry_limit is not None else 0.30,
        rebalance_threshold=request.rebalance_threshold,
    )
    holdings = [{**row, "weight": weights[symbol]} for symbol, row in holding_rows.items()]
    try:
        plan = build_portfolio_plan(
            holdings, price_series=series, constraints=constraints,
            risk_level=request.risk_level, total_value=request.total_value,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    plan["elapsed_ms"] = round((perf_counter() - started) * 1000, 2)
    plan["price_history_coverage"] = f"{len(series)}/{len(weights)}"
    return plan


@app.post("/api/v1/research/grounding-check", tags=["advice"])
async def grounding_check(request: GroundingCheckRequest) -> dict[str, object]:
    """把一段投顾文字与一组事实做数值对账（幻觉检测的可复用入口）。"""

    return check_text_grounding(request.text, list(request.facts))


@app.post("/api/v1/research/report/markdown", tags=["advice"])
async def export_research_markdown(
    request: ReportExportRequest, user: dict[str, object] = Depends(customer_user),
) -> dict[str, object]:
    """把已生成的报告渲染为 Markdown，供用户下载留档。"""

    del user
    markdown = research_report_markdown(request.report, question=request.question)
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M")
    return {"filename": f"问策智投_投资分析报告_{stamp}.md", "markdown": markdown,
            "targets": request.report.targets, "status": request.report.status}


@app.post("/api/v1/profile/assess", response_model=ProfileAssessment, tags=["profile"])
async def assess_user_profile(request: ProfileAssessmentRequest, user: dict[str, object] | None = Depends(optional_customer_user)) -> ProfileAssessment:
    """将问卷/文本转换为未确认画像草稿。

    返回值始终是 ``confirmed=False``；这不是可直接用于精确仓位建议的授权，
    前端应展示提取证据和缺失字段，请用户在下一步核对并确认。
    """

    if user is not None:
        request = request.model_copy(update={"user_id": str(user["id"])})
    try:
        return await assess_profile(request, coordinator.semantic)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _trade_file_task(user, function, *args):
    """文件解析在账号工作线程执行，避免占用 API 事件循环。"""
    try:
        future = session_thread_pool.submit(str(user["_session_id"]), function, *args)
        return await asyncio.wrap_future(future)
    except ProfileVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DatabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _rag_store():
    if rag_service is None:
        raise HTTPException(status_code=503, detail="本地RAG已关闭")
    return rag_service


@app.get("/api/v1/rag/status", tags=["rag"])
async def rag_status(user: dict[str, object] = Depends(customer_user)):
    if rag_service is None:
        return {"enabled": False, "ready": False}
    return await _trade_file_task(user, rag_service.status, int(user["id"]))


@app.get("/api/v1/rag/documents", tags=["rag"])
async def rag_documents(user: dict[str, object] = Depends(customer_user)):
    return await _trade_file_task(user, _rag_store().documents, int(user["id"]))


@app.post("/api/v1/rag/documents", tags=["rag"])
async def rag_import(request: DocumentUpload, user: dict[str, object] = Depends(customer_user)):
    try:
        return await _trade_file_task(user, _rag_store().add, int(user["id"]), request)
    except (RuntimeError, OSError) as exc:
        raise HTTPException(status_code=503, detail="本地向量模型不可用，请运行RAG部署脚本") from exc


@app.post("/api/v1/rag/search", tags=["rag"])
async def rag_search(request: SearchRequest, user: dict[str, object] = Depends(customer_user)):
    try:
        return await _trade_file_task(user, _rag_store().search, int(user["id"]), request)
    except (RuntimeError, OSError) as exc:
        raise HTTPException(status_code=503, detail="本地向量模型不可用，请运行RAG部署脚本") from exc


@app.patch("/api/v1/rag/documents/{document_id}", tags=["rag"])
async def rag_document_state(document_id: str, request: DocumentState,
                             user: dict[str, object] = Depends(customer_user)):
    try:
        return await _trade_file_task(user, _rag_store().set_enabled, int(user["id"]), document_id, request.enabled)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="资料不存在") from exc


@app.post("/api/v1/profile/auto-plan", tags=["profile"])
async def automatically_plan_profile(request: AutomaticPlanningRequest, user: dict[str, object] = Depends(customer_user)):
    """只读规划预览；画像归属由账号决定，不生成未答题的答案或修改确认状态。"""
    from backend.app.risk_questionnaire import QUESTIONNAIRE_VERSION, QUESTIONS, answer_issues, evaluate_answers
    stored = await _trade_file_task(user, database.get_profile, int(user["id"]))
    profile = dict(stored["payload"]) if stored else {}
    if request.risk_answers is not None:
        if request.questionnaire_version != QUESTIONNAIRE_VERSION:
            raise HTTPException(status_code=422, detail="问卷版本已更新，请刷新后重新识别。")
        issues = answer_issues(request.risk_answers)
        if issues:
            raise HTTPException(status_code=422, detail="；".join(issues))
        # 编辑中的问卷不继承旧结果的风险等级或确认状态。
        profile = {}
        if len(request.risk_answers) == len(QUESTIONS):
            profile = evaluate_answers(request.risk_answers)
    elif request.questionnaire_version is not None:
        raise HTTPException(status_code=422, detail="请同时提交问卷答案。")
    return build_automatic_planning(profile, narrative=request.narrative, risk_answers=request.risk_answers)


@app.post("/api/v1/profile/investment-plan", tags=["profile"])
async def personalized_investment_plan(request: PersonalizedInvestmentRequest, user: dict[str, object] = Depends(customer_user)):
    """只读生成当前账号方案；不接受客户端画像、评级或账号归属。"""
    stored = await _trade_file_task(user, database.get_profile, int(user["id"]))
    profile = UserProfile.model_validate({**stored["payload"], "version": stored["version"]}) if stored else None
    return build_personalized_investment(profile, investment_amount=request.investment_amount)


@app.post("/api/v1/profile/trades/inspect", tags=["profile"])
async def inspect_user_trades(request: TradeHistoryUpload, user: dict[str, object] = Depends(customer_user)):
    return await _trade_file_task(user, inspect_trade_workbook, request)


@app.post("/api/v1/profile/trades/analyze", tags=["profile"])
async def analyze_user_trades(request: TradeHistoryUpload, user: dict[str, object] = Depends(customer_user)):
    """只预览交易行为，不改动账号画像或确认状态。"""
    return await _trade_file_task(user, analyze_trade_history, request)


def _save_trade_analysis(request: TradeHistoryUpload, user_id: int) -> UserProfile:
    # 保存时重新从文件计算，客户端不能提交或篡改画像结论。
    analysis = analyze_trade_history(request)
    stored = database.get_profile(user_id)
    profile = UserProfile.model_validate({**(stored["payload"] if stored else {}), "user_id": str(user_id)})
    if profile.version != request.expected_version:
        raise ProfileVersionConflict("用户画像已在其他页面更新，请刷新后重新核对并保存。")
    analysis.pop("preview", None)
    analysis["confirmed"] = True
    saved = profile.model_copy(update={"trading_analysis": analysis, "version": profile.version + 1})
    database.save_profile(user_id, saved.model_dump(mode="json"), saved.version, request.expected_version)
    return saved


@app.post("/api/v1/profile/trades/confirm", response_model=UserProfile, tags=["profile"])
async def confirm_user_trades(request: TradeHistoryUpload, user: dict[str, object] = Depends(customer_user)):
    """确认交易行为补充画像，保留原风险测评及其有效期。"""
    return await _trade_file_task(user, _save_trade_analysis, request, int(user["id"]))


@app.post("/api/v1/profile/confirm", response_model=UserProfile, tags=["profile"])
async def confirm_user_profile(
    request: ProfileConfirmRequest,
    user: dict[str, object] | None = Depends(optional_customer_user),
) -> UserProfile:
    """显式确认画像、递增版本号并按账号持久化。

    已登录用户的画像写入 MySQL，因此退出登录或换浏览器后再次登录可以直接恢复，
    不必重新填写问卷；版本号用于留下审计边界。
    """

    if not request.profile.questionnaire_version:
        raise HTTPException(status_code=422, detail="请完成当前版本的全部题目后再确认风险测评。")
    if user is not None:
        # 画像归属以鉴权上下文为准，不接受前端声明的 user_id。
        request = request.model_copy(
            update={"profile": request.profile.model_copy(update={"user_id": str(user["id"])})}
        )
    try:
        confirmed = confirm_profile(request.profile)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # 交易分析由专用上传接口生成；问卷确认不信任客户端提供的分析，且保留
    # 当前账号已确认的历史交易摘要，防止重新测评清掉交易画像。
    trading_analysis = {}
    investment_workspace = {}
    if user is not None:
        stored = await _trade_file_task(user, database.get_profile, int(user["id"]))
        if stored:
            trading_analysis = stored["payload"].get("trading_analysis") or {}
            investment_workspace = stored["payload"].get("investment_workspace") or {}
    confirmed = confirmed.model_copy(update={"trading_analysis": trading_analysis, "investment_workspace": investment_workspace})
    if user is not None:
        future = session_thread_pool.submit(
            str(user["_session_id"]),
            database.save_profile,
            int(user["id"]),
            confirmed.model_dump(mode="json"),
            confirmed.version,
            request.profile.version,
        )
        try:
            await asyncio.wrap_future(future)
        except ProfileVersionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return confirmed


@app.get("/api/v1/profile", response_model=ProfileAssessment, tags=["profile"])
def read_user_profile(user: dict[str, object] = Depends(customer_user)) -> ProfileAssessment:
    """读取当前账号已确认的画像；没有保存过时返回未确认的默认画像。"""

    try:
        stored = database.get_profile(int(user["id"]))
    except DatabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not stored:
        return ProfileAssessment(profile=UserProfile(user_id=str(user["id"])), evidence=[])
    profile = UserProfile.model_validate({**stored["payload"], "user_id": str(user["id"])})
    profile = profile.model_copy(update={"automatic_planning": build_automatic_planning(profile.model_dump(mode="json"))})
    return ProfileAssessment(
        profile=profile,
        evidence=["已从上次保存的投资偏好恢复，如情况有变化请重新评估。"],
    )


def _is_profile_stock_recommendation(request: OrchestrationRequest) -> bool:
    return (request.research_mode == "deep"
            and request.research_direction == DIRECTION_FOR_INTENT[Intent.SECURITY_RESEARCH]
            and request.query == PROFILE_STOCK_RECOMMENDATION_QUERY
            and request.research_target is None and not request.context_messages)


# 方向 → 意图的反查表：结构化按钮已经给出研究方向时，取数不必等模型识别。
INTENT_FOR_DIRECTION: dict[str, Intent] = {direction: intent for intent, direction in DIRECTION_FOR_INTENT.items()}
SPECULATIVE_PREFETCH = (os.getenv("WENCE_SPECULATIVE_PREFETCH", "on").strip().lower()
                        not in {"0", "off", "false", "no"})


def _speculation_key(request: OrchestrationRequest) -> tuple[Intent, str] | None:
    """判断能否在看到意图之前就先取数。

    只在免费可得的信号足够时才预取：结构化方向 + 明确的标的。问句里没有标的
    时模型才会给 target，猜错就会白抓一次数据，因此宁可不猜。
    """

    if not SPECULATIVE_PREFETCH or request.research_mode != "deep" or not request.auto_fetch:
        return None
    direction = (request.research_direction or "").strip()
    target = (request.research_target or "").strip()
    if not direction or not target:
        return None
    intent = INTENT_FOR_DIRECTION.get(direction)
    if intent is None:
        return None
    return intent, target


def _start_prefetch(pipeline: AutomatedResearchPipeline, request: OrchestrationRequest,
                    key: tuple[Intent, str]) -> asyncio.Task:
    intent, target = key

    async def run() -> tuple[OrchestrationRequest, DataAcquisitionResult]:
        return await pipeline.prepare(request, intent, target=target)

    task = asyncio.ensure_future(run())
    # 预取失败绝不能影响主流程：异常在这里就地消化，由调用方决定是否重取。
    task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
    return task


async def _take_prefetch(
    task: asyncio.Task | None,
    key: tuple[Intent, str] | None,
    fetch_intent: Intent,
    target: str | None,
    report,
) -> tuple[OrchestrationRequest, DataAcquisitionResult] | None:
    """拿回预取结果；与最终意图/标的不一致时直接丢弃并取消。"""

    if task is None:
        return None
    matched = (key is not None and fetch_intent == key[0]
               and (target or "").strip() == key[1])
    if not matched:
        task.cancel()
        return None
    try:
        prepared_request, acquisition = await task
    except asyncio.CancelledError:
        raise
    except Exception:  # 预取失败只降级为常规取数，不影响本次分析
        return None
    report("复用预取资料")
    return prepared_request, acquisition


@app.post(
    "/api/v1/portfolio/analyze",
    response_model=AdvicePackage,
    tags=["advice"],
)
async def analyze_portfolio(
    request: OrchestrationRequest,
    user: dict[str, object] | None = Depends(optional_customer_user),
) -> AdvicePackage:
    """Keep the three-second route bounded; deep research has no overall deadline."""
    if request.research_mode == "deep":
        return await _analyze_portfolio(request, user)
    budget = (min(analysis_timeout_seconds, fast_response_timeout_seconds)
              if request.research_mode in {"lite", "instant", "fast"} else analysis_timeout_seconds)
    try:
        return await asyncio.wait_for(_analyze_portfolio(request, user), timeout=budget)
    except TimeoutError as exc:
        detail = ("已达到3秒响应预算，完整分析或审核尚未完成，未生成投资建议。"
                  "需要完整分析时，请选择深度思考。" if request.research_mode in {"lite", "instant", "fast"}
                  else "本次研究超过时间预算，尚未完成核验，请稍后重试。")
        raise HTTPException(status_code=504, detail=detail) from exc


async def _analyze_portfolio(
    request: OrchestrationRequest,
    user: dict[str, object] | None,
    *, persist_history: bool = True, pipeline: AutomatedResearchPipeline | None = None,
) -> AdvicePackage:
    """运行组合诊断闭环并返回可审计建议包。

    FastAPI 会在进入本函数前验证 ``query``、画像和事实记录的类型及边界。协调器
    会在内部完成画像确认、并行专业分析、事实核验和合规审核；若被 BLOCK，响应
    仍为 200，但 ``compliance.status`` 为 ``BLOCK`` 且不含投资建议。
    """

    pipeline = pipeline or research_pipeline
    started = perf_counter()
    stage_started = started
    stage = None
    timings: dict[str, float] = {}
    outcome = "ERROR"
    completed_scope = None
    admission_slot = None
    prefetch_task: asyncio.Task | None = None
    telemetry = {"calls": [], "mode": request.research_mode}
    if user:
        telemetry["cache_scope"] = str(user["id"])
    telemetry_token = analysis_telemetry.set(telemetry)

    def report(next_stage: str) -> None:
        nonlocal stage, stage_started
        now = perf_counter()
        if stage is not None:
            timings[stage] = round(timings.get(stage, 0) + (now - stage_started) * 1_000, 2)
        stage, stage_started = next_stage, now
        _report_progress(next_stage)

    try:
        try:
            admission = (deep_research_admission if request.research_mode == "deep" else
                local_research_admission if request.research_mode in {"lite", "instant"} else research_admission)
            if request.research_mode == "deep":
                report("等待深入研究资源")
            admission_slot = await admission.acquire()
        except TimeoutError as exc:
            outcome = "OVERLOADED"
            raise HTTPException(status_code=429, detail="当前研究请求较多，请稍后重试。", headers={"Retry-After": "2"}) from exc
        report("核对投资偏好")
        if user:
            stored = await asyncio.wrap_future(session_thread_pool.submit(
                str(user["_session_id"]), database.get_profile, int(user["id"]),
            ))
            if not stored or not stored["payload"].get("confirmed"):
                raise HTTPException(status_code=409, detail="请先确认投资偏好，再开始分析。")
            if request.profile.version != stored["version"]:
                raise HTTPException(status_code=409, detail="风险评估已更新，请重新打开风险评估并确认后再分析。")
            profile = UserProfile.model_validate({
                **stored["payload"], "version": stored["version"], "user_id": str(user["id"]),
            })
            request = request.model_copy(update={"profile": profile})
        if request.profile.questionnaire_version and not assessment_is_current(request.profile):
            raise HTTPException(status_code=409, detail="风险测评未确认或已过期，请重新完成风险测评后再分析。")
        history_context, context_history_status = None, "not_requested"
        if (request.context_compression and request.use_conversation_history and user and request.conversation_id
                and request.research_mode in {"deep", "fast"} and not request.industry_collaboration
                and not _is_profile_stock_recommendation(request)):
            report("整理对话上下文")
            try:
                history_context = await asyncio.wait_for(asyncio.wrap_future(session_thread_pool.submit(
                    str(user["_session_id"]), database.get_conversation_context,
                    int(user["id"]), request.conversation_id, request.research_direction, HISTORY_MESSAGES,
                )), timeout=2)
                context_history_status = ("not_found" if history_context is None else
                                          "scope_mismatch" if history_context.get("scope_mismatch") else "loaded")
            except (DatabaseUnavailable, SQLAlchemyError, TimeoutError):
                context_history_status = "unavailable"
        request = prepare_request_context(request, history=history_context, history_status=context_history_status)
        scope_stage = "选择精简范围" if request.research_mode in {"lite", "instant"} else "理解问题"
        report("准备行业协同" if request.industry_collaboration else scope_stage)
        prefetch_key: tuple[Intent, str] | None = None
        if request.industry_collaboration:
            understanding = await coordinator.understand_request(request)
        elif request.research_mode in {"lite", "instant", "auto"}:
            understanding = select_scope(request)
        elif _is_profile_stock_recommendation(request):
            # 按钮发出的固定任务无需自由问句识别，候选及最终结论仍走完整核验。
            if not assessment_is_current(request.profile):
                raise HTTPException(status_code=409, detail="请先确认投资偏好，再推荐股票。")
            understanding = RequestUnderstanding(
                intent=Intent.SECURITY_RESEARCH, action="recommend_stocks", confidence=1,
                risk_rules=[], reason="按已确认的投资偏好筛选股票。",
            )
        else:
            # 方向与标的都已明确时，取数与意图识别并行跑；最后按识别结果决定复用还是丢弃。
            prefetch_key = _speculation_key(request)
            if prefetch_key is not None:
                prefetch_task = _start_prefetch(pipeline, request, prefetch_key)
            try:
                understanding = await coordinator.understand_request(request)
            except BaseException:
                if prefetch_task is not None:
                    prefetch_task.cancel()
                raise
        # 风险请求不访问外部数据服务。
        wrong_direction = bool(request.research_direction and
                               DIRECTION_FOR_INTENT.get(understanding.intent) != request.research_direction)
        fetch_intent = Intent.UNKNOWN if understanding.risk_rules or wrong_direction else understanding.intent
        report("查找资料")
        model_slice: dict[str, object] = {}
        rag_audit = {"status": "skipped", "reason": "当前模式不使用向量检索"}
        if request.research_mode == "auto":
            history = None
            if (user and request.conversation_id and source_item(request.query) and not understanding.risk_rules
                    and understanding.reason != "请求不在只读研究范围内" and assessment_is_current(request.profile)):
                history = await asyncio.wrap_future(session_thread_pool.submit(
                    str(user["_session_id"]), database.get_conversation, int(user["id"]), request.conversation_id, None, 12,
                ))
            prepared_request, acquisition, advice, understanding = await run_automatic(
                request, pipeline, instant_research, understanding, history=history, progress_sink=report,
            )
        elif request.research_mode in {"lite", "instant"}:
            service = instant_research if request.research_mode == "instant" else local_consultation
            prepared_request, acquisition, advice = await service.run(
                request, understanding, progress_sink=report,
            )
        elif StockRecommendationService.should_run(request, understanding):
            prepared_request, acquisition, advice = await StockRecommendationService(
                pipeline, coordinator,
            ).run(request, understanding, progress_sink=report, metrics_sink=model_slice)
        else:
            prefetched = None
            if not understanding.data_requirements:
                prefetched = await _take_prefetch(
                    prefetch_task, prefetch_key, fetch_intent, understanding.target, report,
                )
            elif prefetch_task is not None and prefetch_key != (fetch_intent, understanding.target):
                prefetch_task.cancel()
            if prefetched is not None:
                prepared_request, acquisition = prefetched
            else:
                prepared_request, acquisition = await pipeline.prepare(
                    request, fetch_intent, target=understanding.target,
                    data_requirements=understanding.data_requirements,
                )
            # Repair observable gaps before the first specialist/model pass.
            prepared_request, acquisition = await recover_research(
                pipeline, prepared_request, fetch_intent, None, acquisition,
                target=understanding.target, data_requirements=understanding.data_requirements,
                progress_sink=report, semantic=coordinator.semantic,
            )
            # Current provider data is collected first. RAG can supplement passages,
            # but cannot replace a fresh quote or cross account/asset boundaries.
            if (rag_service is not None and request.use_rag and user and not understanding.risk_rules
                    and not wrong_direction and not request.industry_collaboration):
                report("检索本地知识库")
                try:
                    from backend.app.services.security_identity import STOCK_NAMES
                    asset = {Intent.SECURITY_RESEARCH: "stock", Intent.FUND_SCREENING: "fund",
                             Intent.CONVERTIBLE_BOND_ANALYSIS: "bond", Intent.INDUSTRY_ANALYSIS: "industry",
                             Intent.MARKET_ANALYSIS: "market"}.get(fetch_intent)
                    target = (understanding.target or request.research_target or "").strip()
                    code = STOCK_NAMES.get(target, "") if asset == "stock" else ""
                    if re.fullmatch(r"[0-9]{6}(?:\.[A-Z]{2})?", target.upper()):
                        code = target.upper()
                    lookup = SearchRequest(query=request.query[:1000],entity=target,entity_code=code,asset_type=asset)
                    owner = int(user["id"])
                    rag_audit = await asyncio.to_thread(rag_service.search, owner, lookup,
                                                       general_only=not bool(target and asset),
                                                       available_facts=prepared_request.facts)
                    ids = {fact.fact_id for fact in prepared_request.facts}
                    from backend.app.models import FactRecord
                    retrieved = [FactRecord.model_validate(item["fact"]) for item in rag_audit["matches"]
                                 if item["fact"]["fact_id"] not in ids]
                    prepared_request = prepared_request.model_copy(update={"facts": [*prepared_request.facts, *retrieved][:4000]})
                    original_ids = {fact.fact_id for fact in request.facts}
                    acquired = [fact for fact in prepared_request.facts if fact.fact_id not in original_ids
                                and fact.fact_id not in {item.fact_id for item in retrieved}]
                    rag_audit["stored_facts"] = (await asyncio.to_thread(rag_service.ingest_facts, owner, acquired, asset)
                                                  if asset else 0)
                except Exception:
                    logging.getLogger(__name__).warning("Local RAG unavailable", exc_info=True)
                    rag_audit = {"status": "unavailable", "notice": "本地知识库暂不可用，继续执行原有取数与核验。"}
            async def recover_before_review(provisional):
                nonlocal prepared_request, acquisition
                recovered, acquisition = await recover_research(
                    pipeline, prepared_request, fetch_intent, provisional, acquisition,
                    target=understanding.target, data_requirements=understanding.data_requirements,
                    progress_sink=report, semantic=coordinator.semantic,
                )
                if recovered is not prepared_request:
                    acquisition = acquisition.model_copy(update={"recovery_reanalyzed": True})
                    prepared_request = recovered
                return prepared_request

            advice = await coordinator.run(
                prepared_request, understanding=understanding, metrics_sink=model_slice,
                progress_sink=report, recovery_hook=recover_before_review,
            )
        if acquisition.recovery_rounds and (
            acquisition.missing_fields_by_agent or advice.cross_validation.issues
        ):
            advice = advice.model_copy(update={"next_steps": list(dict.fromkeys([
                "系统已自动补取相关资料；仍未解决的缺项或冲突，请取得可核验资料后复核",
                *[step for step in advice.next_steps if step not in {
                    "补充有来源、含时间戳的事实后重新核验", "补齐各专业智能体列出的缺失字段",
                }],
            ]))})
        # 指标属于本次请求；提前拦截时保持为零，不读取共享协调器状态。
        acquisition = acquisition.model_copy(
            update={
                "model_fact_count": len(telemetry.get("model_fact_ids", set())),
                "model_fact_available": int(model_slice.get("available") or 0),
                "facts_truncated": bool(telemetry.get("model_fact_ids")) and
                    len(telemetry["model_fact_ids"]) < len(prepared_request.facts),
            }
        )
        exhausted = [cap for cap, error in acquisition.capability_errors.items()
                     if error.get("code") == "PROVIDER_QUOTA_EXHAUSTED"]
        if exhausted:
            label = "行业资料" if exhausted == ["industry"] else "部分研究资料"
            acquisition = acquisition.model_copy(update={
                "reason_code": "PROVIDER_QUOTA_EXHAUSTED",
                "message": (acquisition.message or "") + f" {label}查询额度已用完，缺失资料仍需在额度恢复后复核。",
            })
        if (not acquisition.reason_code and request.research_mode not in {"lite", "instant", "auto"}
                and (llm_enabled or request.industry_collaboration)
                and advice.agent_results and not any(
                    (result.details or {}).get("engine") == "third_party_llm" for result in advice.agent_results
                )):
            acquisition = acquisition.model_copy(update={"reason_code": "MODEL_UNAVAILABLE"})
        completed_advice = advice.model_copy(
            update={
                "facts": prepared_request.facts,
                "profile_version": request.profile.version,
                "data_acquisition": acquisition,
                "model_calls": telemetry["calls"],
                "model_cost": summarize_costs(telemetry["calls"]),
                "research_mode": request.research_mode,
                # 数字级对账：回答里每个数值是否能在所引事实中找到来源。
                "numeric_grounding": summarize_grounding(advice.agent_results, prepared_request.facts),
                "context_compression": {**request._context_compaction, "model_calls": len(telemetry["calls"])},
                "rag": rag_audit,
            }
        )
        from backend.app.services.research_report import attach_research_report
        completed_advice = attach_research_report(clean_advice(completed_advice))
        completed_scope = completed_advice.response_scope
        if user and persist_history and not (request.preview_only and request.research_mode in {"lite", "instant"}):
            report("排入历史记录" if request.research_mode in {"lite", "instant"} else "保存对话")
            completed_advice.history_status = "pending" if request.research_mode in {"lite", "instant"} else "stored"
            completed_advice.timings_ms = dict(timings)
            # 历史记录只保留展示所需的轻量摘要：完整证据包可达数 MB，会撑大
            # messages 行宽并让列表/详情查询触发数据库排序内存告警。
            completed_payload = completed_advice.model_dump(mode="json")
            # Once readable from history the write has succeeded; the immediate
            # response still reports pending while the background write runs.
            completed_payload["history_status"] = "stored"
            future = session_thread_pool.submit(
                str(user["_session_id"]),
                database.save_exchange,
                int(user["id"]),
                request.conversation_id,
                request.query,
                {**request.model_dump(mode="json"), "_analytics": consultation_metadata(
                    understanding.intent, understanding.target, request.query,
                )},
                completed_advice.conclusion,
                summarise_advice(completed_payload, used_fact_ids_of(completed_payload)),
                cited_facts_of(completed_payload),
            )
            if request.research_mode in {"lite", "instant"}:
                trace_id = completed_advice.trace_id
                def record_history_failure(done):
                    try:
                        done.result()
                    except Exception as exc:
                        logging.getLogger(__name__).warning("精简咨询历史保存失败: trace=%s type=%s",
                                                            trace_id, type(exc).__name__)
                future.add_done_callback(record_history_failure)
            else:
                await asyncio.wrap_future(future)
        report("已完成")
        timings["total"] = round((perf_counter() - started) * 1_000, 2)
        outcome = completed_advice.compliance.status.value
        return completed_advice.model_copy(update={"timings_ms": timings})
    except DatabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        # 不暴露异常细节（可能包含数据源地址或内部实现），同时保留服务端日志入口。
        # 当前本地版未配置日志器；正式环境应记录 trace_id、异常类型与脱敏上下文。
        raise HTTPException(status_code=500, detail="组合诊断服务暂时不可用，请稍后重试。") from exc
    finally:
        if prefetch_task is not None and not prefetch_task.done():
            prefetch_task.cancel()
        if admission_slot is not None:
            admission_slot.release()
        analysis_telemetry.reset(telemetry_token)
        service_metrics.record_analysis((perf_counter() - started) * 1_000, outcome,
                                        scope=(("instant" if completed_scope == "evidence_review" else "instant_other")
                                               if request.research_mode == "instant" else
                                               "lite" if request.research_mode == "lite" else "auto" if request.research_mode == "auto" else "full"))


async def _prepared_inputs(request, user):
    stored = await asyncio.wrap_future(session_thread_pool.submit(
        str(user["_session_id"]), database.get_profile, int(user["id"])))
    if not stored or not stored["payload"].get("confirmed") or stored["version"] != request.profile_version:
        raise HTTPException(status_code=409, detail="投资偏好尚未确认或已更新，请重新确认。")
    profile = UserProfile.model_validate({**stored["payload"], "version": stored["version"], "user_id": str(user["id"])})
    if profile.questionnaire_version and not assessment_is_current(profile):
        raise HTTPException(status_code=409, detail="风险测评已过期，请重新评估。")
    board = await overview_cache.get(data_provider, DataOverviewRequest(direction="stock", target=request.target, wait=False))
    llm = getattr(coordinator.semantic, "llm", None)
    config = getattr(llm, "config", None)
    runtime = [id(coordinator), id(data_provider), getattr(config, "model", None), getattr(config, "base_url", None)]
    key = prepared_research_cache.key(user["id"], profile, request.target, request.task, runtime, request.portfolio)
    return profile, board, key


@app.post("/api/v1/research/prepared", tags=["advice"])
async def prepare_stock_report(request: PreparedResearchRequest, user: dict[str, object] = Depends(customer_user)):
    """Start one bounded full run per account; status polling never waits for models."""
    profile, board, key = await _prepared_inputs(request, user)
    entry = prepared_research_cache.peek(key)
    existing_state = prepared_research_cache.state(entry) if entry else None
    source_ready = board.status == "ok"
    signature = board_signature(board) if source_ready else None
    # Data refresh must not hide a running or failed research job, and must
    # never cause repeated paid retries of an incomplete report.
    keep_existing = entry is not None and (
        (existing_state in {"preparing", "needs_review", "failed"} and not request.retry)
        or (existing_state == "ready" and source_ready and entry.source_signature == signature))
    if not keep_existing and not source_ready:
        return {"status": "loading_data" if board.status == "loading" else "data_unavailable", "target": request.target}
    if not keep_existing and entry is not None and existing_state == "ready":
        prepared_research_cache.entries.pop(key, None)
    facts = {f.fact_id: f for section in board.sections if section.status == "ok" for f in section.facts}
    research_request = OrchestrationRequest(query=task_query(request.target, request.task), research_direction="个股研究",
        research_mode="deep", profile=profile, facts=list(facts.values()), portfolio=request.portfolio, auto_fetch=True)
    async def load():
        # Background preparation can afford a longer evidence budget. The
        # independent pipeline shares only the verified public fact cache.
        pipeline = AutomatedResearchPipeline(data_provider, cache=research_pipeline.cache,
            recovery_timeout_seconds=preparation_recovery_seconds)
        return await asyncio.wait_for(_analyze_portfolio(research_request, dict(user),
            persist_history=False, pipeline=pipeline), preparation_timeout_seconds)
    if not keep_existing:
        entry = prepared_research_cache.prepare(key, user["id"], user["_session_id"], research_request, load, retry=request.retry, source_signature=signature)
    if entry is None:
        return {"status": "busy", "target": request.target}
    return {"status": prepared_research_cache.state(entry), "target": request.target, "task": request.task,
            "query": research_request.query, "generated_at": entry.generated_at,
            "preparation_seconds": round((entry.finished or monotonic())-entry.started, 3),
            "trace_id": entry.advice.trace_id if entry.advice else None,
            "error_type": entry.error_type,
            "generation_timings_ms": entry.advice.timings_ms if entry.advice else {},
            "review": ({"compliance": entry.advice.compliance.status.value,
                "cross_validation": entry.advice.cross_validation.status.value,
                "missing_fields": entry.advice.data_acquisition.missing_fields_by_agent,
                "model_calls": entry.advice.model_calls} if entry.advice else None)}


@app.post("/api/v1/research/prepared/open", response_model=AdvicePackage, tags=["advice"])
async def open_prepared_stock_report(request: PreparedResearchRequest, user: dict[str, object] = Depends(customer_user)):
    """Deliver only a still-matching, fully reviewed report within a 2.6s budget."""
    from uuid import uuid4
    from backend.app.agents.coordinator import cross_validate_results
    started = perf_counter()
    try:
        async with asyncio.timeout(2.6):
            profile, board, key = await _prepared_inputs(request, user)
            entry = prepared_research_cache.peek(key) if key else None
            if (board.status != "ok" or entry is None or prepared_research_cache.state(entry) != "ready"
                    or entry.source_signature != board_signature(board)):
                raise HTTPException(status_code=409, detail="完整报告尚未就绪、资料已变化或仍需复核；本次不计为3秒深入研究完成。")
            original = entry.advice.model_copy(deep=True)
            if not report_is_ready(original):
                raise HTTPException(status_code=409, detail="报告证据已失效，请重新准备。")
            checked = await verify_facts(original.agent_results, original.facts)
            safe_request = entry.request.model_copy(update={"profile": profile, "facts": original.facts})
            rules = await basic_compliance_check(safe_request, checked)
            cross = cross_validate_results(checked, original.facts)
            if (rules.status.value != "PASS" or cross.status.value != "PASS"
                    or any(a.facts_used != b.facts_used for a,b in zip(original.agent_results, checked, strict=True))):
                raise HTTPException(status_code=409, detail="当前事实或风险复核未通过，请重新准备。")
            trace_id = "P-" + uuid4().hex[:12].upper()
            delivered = original.model_copy(update={"trace_id": trace_id,
                "task_plan": original.task_plan.model_copy(update={"trace_id": trace_id}),
                "timings_ms": {}, "model_calls": [], "model_cost": summarize_costs([]), "history_status": "stored",
                "prepared_report": {"source_trace_id": original.trace_id, "generated_at": entry.generated_at,
                    "generation_timings_ms": original.timings_ms, "generation_model_calls": original.model_calls,
                    "generation_model_cost": original.model_cost, "reused": True}})
            from backend.app.services.research_report import attach_research_report
            delivered = attach_research_report(clean_advice(delivered))
            delivered.timings_ms = {"核对已审核报告": round((perf_counter()-started)*1000, 2)}
            payload = delivered.model_dump(mode="json")
            await asyncio.wrap_future(session_thread_pool.submit(str(user["_session_id"]), database.save_exchange,
                int(user["id"]), request.conversation_id, entry.request.query,
                {**entry.request.model_dump(mode="json"), "_analytics": consultation_metadata(Intent.SECURITY_RESEARCH, request.target, entry.request.query)},
                delivered.conclusion, summarise_advice(payload, used_fact_ids_of(payload)), cited_facts_of(payload)))
            delivered.timings_ms["total"] = round((perf_counter()-started)*1000, 2)
            return delivered
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="本次报告交付超过3秒预算，未宣称已完成；请检查历史后再重试。") from exc


@app.post("/api/v1/portfolio/analyze/stream", tags=["advice"])
async def stream_portfolio_analysis(
    request: OrchestrationRequest,
    user: dict[str, object] | None = Depends(optional_customer_user),
) -> StreamingResponse:
    """逐阶段发送进度；只在全部核验完成后发送最终建议包。"""

    async def events():
        queue: asyncio.Queue[str] = asyncio.Queue()
        token = analysis_progress.set(queue.put_nowait)
        task = asyncio.create_task(analyze_portfolio(request, user))
        analysis_progress.reset(token)
        stage_task = None
        try:
            while not task.done() or not queue.empty():
                if not queue.empty():
                    stage = queue.get_nowait()
                else:
                    stage_task = asyncio.create_task(queue.get())
                    done, _ = await asyncio.wait(
                        {task, stage_task}, timeout=DEEP_STREAM_HEARTBEAT_SECONDS if request.research_mode == "deep" else None,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if not done:
                        stage_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await stage_task
                        stage_task = None
                        yield '{"type":"heartbeat"}\n'
                        continue
                    if not stage_task.done():
                        stage_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await stage_task
                        stage_task = None
                        break
                    stage = stage_task.result()
                    stage_task = None
                yield json.dumps({"type": "progress", "stage": stage}, ensure_ascii=False) + "\n"
            try:
                advice = await task
            except HTTPException as exc:
                yield json.dumps({"type": "error", "message": exc.detail, "status_code": exc.status_code,
                                  "retry_after": (exc.headers or {}).get("Retry-After")}, ensure_ascii=False) + "\n"
            except Exception:
                yield json.dumps({"type": "error", "message": "分析暂时无法完成，请稍后重试。"}, ensure_ascii=False) + "\n"
            else:
                yield json.dumps({"type": "result", "advice": advice.model_dump(mode="json")}, ensure_ascii=False) + "\n"
        finally:
            if stage_task is not None:
                stage_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stage_task
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    return StreamingResponse(
        events(), media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
