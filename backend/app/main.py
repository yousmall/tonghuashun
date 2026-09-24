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
from contextvars import ContextVar
from collections.abc import Callable
from contextlib import asynccontextmanager
from contextlib import suppress
from datetime import date, datetime, timezone
from time import perf_counter

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi import Request, Response
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv

from backend.app.agents.coordinator import (
    CoordinatorAgent,
    basic_compliance_check,
    verify_facts,
)
from backend.app.agents.llm_agents import LLMConfig, OpenAICompatibleLLM, make_investment_agents
from backend.app.semantic import SemanticService
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
from backend.app.session_pool import (
    SessionCapacityExceeded,
    SessionLeaseExpired,
    SessionThreadPool,
)


# 从本地 .env 加载可选外部服务配置；生产环境中已有的环境变量优先。
load_dotenv()
database = Database.from_env()
session_thread_pool = SessionThreadPool.from_env()


async def reap_idle_sessions() -> None:
    """定期回收超过空闲时限且没有在途请求的登录会话。"""

    interval = min(30.0, max(1.0, session_thread_pool.idle_timeout_seconds / 2))
    while True:
        await asyncio.sleep(interval)
        session_thread_pool.reap_expired()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """准备数据表并启动空闲会话清理任务。"""

    database.initialize()
    reaper = asyncio.create_task(reap_idle_sessions())
    try:
        yield
    finally:
        reaper.cancel()
        with suppress(asyncio.CancelledError):
            await reaper
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
research_pipeline = AutomatedResearchPipeline(data_provider)
service_metrics = ServiceMetrics()
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
    try:
        payload = _decode_session_token(authorization.split(" ", 1)[1].strip())
        session_id = str(payload["sid"])
        leased_user_id = session_thread_pool.acquire(session_id)
        if leased_user_id != int(payload["sub"]):
            raise TokenError("登录状态无效")
        future = session_thread_pool.submit(session_id, database.get_user, int(payload["sub"]))
        user = await asyncio.wrap_future(future)
        if not user:
            raise TokenError("登录账号不存在")
        user["_session_id"] = session_id
        yield user
    except DatabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (TokenError, SessionLeaseExpired) as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    finally:
        if session_id is not None:
            session_thread_pool.finish(session_id)


def authenticated_user(
    user: dict[str, object] | None = Depends(optional_authenticated_user),
) -> dict[str, object]:
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
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


@app.middleware("http")
async def observe_requests(request: Request, call_next):
    """记录单进程请求量、失败率和延迟分位数，不保存请求正文或敏感信息。"""

    started = perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        service_metrics.record(status_code, (perf_counter() - started) * 1_000)


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
        "mysql_configured": database.configured,
        "mysql_ready": database.configured and database.initialization_error is None,
        "session_thread_pool": session_thread_pool.snapshot(),
    }


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
def login(credentials: Credentials) -> AuthResponse:
    """验证账号密码并签发有过期时间、不可篡改的登录令牌。"""

    try:
        user = database.get_user_by_username(credentials.username)
    except DatabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not user or not verify_password(credentials.password, str(user["password_hash"])):
        raise HTTPException(status_code=401, detail="账号或密码错误")
    user.pop("password_hash", None)
    return auth_response_for(user)


@app.get("/api/v1/auth/me", response_model=UserSummary, tags=["auth"])
def current_user(user: dict[str, object] = Depends(authenticated_user)) -> UserSummary:
    return UserSummary.model_validate(user)


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


@app.get("/api/v1/history", response_model=list[ConversationSummary], tags=["history"])
def list_history(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
    q: str = Query(default="", max_length=100),
    user: dict[str, object] = Depends(authenticated_user),
) -> list[ConversationSummary]:
    return [ConversationSummary.model_validate(row) for row in database.list_conversations(
        int(user["id"]), limit, offset, q,
    )]


@app.get("/api/v1/history/{conversation_id}", response_model=ConversationDetail, tags=["history"])
def history_detail(
    conversation_id: str,
    before_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=40, ge=1, le=100),
    user: dict[str, object] = Depends(authenticated_user),
) -> ConversationDetail:
    result = database.get_conversation(int(user["id"]), conversation_id, before_id, limit)
    if not result:
        raise HTTPException(status_code=404, detail="对话记录不存在")
    return ConversationDetail.model_validate(result)


@app.patch("/api/v1/history/{conversation_id}", tags=["history"])
def rename_history(
    conversation_id: str,
    request: ConversationRename,
    user: dict[str, object] = Depends(authenticated_user),
) -> dict[str, str]:
    if not database.rename_conversation(int(user["id"]), conversation_id, request.title):
        raise HTTPException(status_code=404, detail="对话记录不存在")
    return {"id": conversation_id, "title": request.title}


@app.get("/api/v1/watchlist", response_model=list[WatchlistItem], tags=["watchlist"])
def list_watchlist(user: dict[str, object] = Depends(authenticated_user)) -> list[WatchlistItem]:
    """返回当前账号的自选标的。"""

    return [WatchlistItem.model_validate(row) for row in database.list_watchlist(int(user["id"]))]


@app.post("/api/v1/watchlist", response_model=WatchlistItem, tags=["watchlist"])
def add_watchlist_item(
    request: WatchlistItemCreate,
    user: dict[str, object] = Depends(authenticated_user),
) -> WatchlistItem:
    """新增自选标的；重复项和容量限制以可操作错误返回。"""

    try:
        row = database.add_watchlist_item(int(user["id"]), request.target, request.asset_type)
        return WatchlistItem.model_validate(row)
    except WatchlistItemExists as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WatchlistCapacityExceeded as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/v1/watchlist/{item_id}", tags=["watchlist"])
def remove_watchlist_item(
    item_id: int,
    user: dict[str, object] = Depends(authenticated_user),
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
    }


@app.post("/api/v1/data/fetch", response_model=DataFetchResponse, tags=["data"])
async def fetch_market_data(request: DataFetchRequest) -> DataFetchResponse:
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


@app.post("/api/v1/data/price-history", response_model=PriceHistoryResponse, tags=["data"])
async def price_history(
    request: PriceHistoryRequest,
    user: dict[str, object] = Depends(authenticated_user),
) -> PriceHistoryResponse:
    """为已登录用户按需读取真实日期序列；不把走势当作投资结论。"""

    del user
    metric_label = "单位净值" if request.asset_type == "基金" else "收盘价"
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
        facts = await data_provider.get_price_history(
            request.target, request.asset_type, limit=request.limit,
        )
    except RuntimeError as exc:
        return PriceHistoryResponse(**common, status="unavailable", message=str(exc))
    points = [
        PriceHistoryPoint(date=date.fromisoformat(str(fact.period)), value=float(fact.value))
        for fact in facts if fact.period
    ]
    urls = {fact.source_url for fact in facts if fact.source_url}
    return PriceHistoryResponse(
        **common,
        status="ok" if len(points) >= 2 else "empty",
        points=points if len(points) >= 2 else [],
        source_url=next(iter(urls)) if len(urls) == 1 else None,
        message=None if len(points) >= 2 else "问财暂未返回足够的带日期数值，无法绘制走势。",
    )


@app.post("/api/v1/profile/assess", response_model=ProfileAssessment, tags=["profile"])
async def assess_user_profile(request: ProfileAssessmentRequest) -> ProfileAssessment:
    """将问卷/文本转换为未确认画像草稿。

    返回值始终是 ``confirmed=False``；这不是可直接用于精确仓位建议的授权，
    前端应展示提取证据和缺失字段，请用户在下一步核对并确认。
    """

    return await assess_profile(request, coordinator.semantic)


@app.post("/api/v1/profile/confirm", response_model=UserProfile, tags=["profile"])
async def confirm_user_profile(
    request: ProfileConfirmRequest,
    user: dict[str, object] | None = Depends(optional_authenticated_user),
) -> UserProfile:
    """显式确认画像、递增版本号并按账号持久化。

    已登录用户的画像写入 MySQL，因此退出登录或换浏览器后再次登录可以直接恢复，
    不必重新填写问卷；版本号用于留下审计边界。
    """

    if user is not None:
        # 画像归属以鉴权上下文为准，不接受前端声明的 user_id。
        request = request.model_copy(
            update={"profile": request.profile.model_copy(update={"user_id": str(user["id"])})}
        )
    confirmed = confirm_profile(request.profile)
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
def read_user_profile(user: dict[str, object] = Depends(authenticated_user)) -> ProfileAssessment:
    """读取当前账号已确认的画像；没有保存过时返回未确认的默认画像。"""

    try:
        stored = database.get_profile(int(user["id"]))
    except DatabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not stored:
        return ProfileAssessment(profile=UserProfile(user_id=str(user["id"])), evidence=[])
    profile = UserProfile.model_validate({**stored["payload"], "user_id": str(user["id"])})
    return ProfileAssessment(
        profile=profile,
        evidence=["已从上次保存的投资偏好恢复，如情况有变化请重新评估。"],
    )


@app.post(
    "/api/v1/portfolio/analyze",
    response_model=AdvicePackage,
    tags=["advice"],
)
async def analyze_portfolio(
    request: OrchestrationRequest,
    user: dict[str, object] | None = Depends(optional_authenticated_user),
) -> AdvicePackage:
    """运行组合诊断闭环并返回可审计建议包。

    FastAPI 会在进入本函数前验证 ``query``、画像和事实记录的类型及边界。协调器
    会在内部完成画像确认、并行专业分析、事实核验和合规审核；若被 BLOCK，响应
    仍为 200，但 ``compliance.status`` 为 ``BLOCK`` 且不含投资建议。
    """

    try:
        _report_progress("核对投资偏好")
        if user:
            stored = await asyncio.wrap_future(session_thread_pool.submit(
                str(user["_session_id"]), database.get_profile, int(user["id"]),
            ))
            if not stored or not stored["payload"].get("confirmed"):
                raise HTTPException(status_code=409, detail="请先确认投资偏好，再开始分析。")
            if request.profile.version != stored["version"]:
                raise HTTPException(status_code=409, detail="投资偏好已更新，请重新打开投资偏好并确认后再分析。")
            profile = UserProfile.model_validate({
                **stored["payload"], "version": stored["version"], "user_id": str(user["id"]),
            })
            request = request.model_copy(update={"profile": profile})
        _report_progress("理解问题")
        understanding = await coordinator.understand_request(request)
        # 请求风险和意图在一次模型调用中完成，风险请求不访问外部数据服务。
        fetch_intent = Intent.UNKNOWN if understanding.risk_rules else understanding.intent
        _report_progress("查找资料")
        prepared_request, acquisition = await research_pipeline.prepare(
            request,
            fetch_intent,
            target=understanding.target,
            data_requirements=understanding.data_requirements,
        )
        model_slice: dict[str, object] = {}
        advice = await coordinator.run(
            prepared_request, understanding=understanding, metrics_sink=model_slice,
            progress_sink=_report_progress,
        )
        # 指标属于本次请求；提前拦截时保持为零，不读取共享协调器状态。
        acquisition = acquisition.model_copy(
            update={
                "model_fact_count": int(model_slice.get("selected") or 0),
                "model_fact_available": int(model_slice.get("available") or 0),
                "facts_truncated": bool(model_slice.get("truncated")),
            }
        )
        if llm_enabled and advice.agent_results and not any(
            (result.details or {}).get("engine") == "third_party_llm" for result in advice.agent_results
        ):
            acquisition = acquisition.model_copy(update={"reason_code": "MODEL_UNAVAILABLE"})
        completed_advice = advice.model_copy(
            update={
                "facts": prepared_request.facts,
                "profile_version": request.profile.version,
                "data_acquisition": acquisition,
            }
        )
        if user:
            _report_progress("保存对话")
            # 历史记录只保留展示所需的轻量摘要：完整证据包可达数 MB，会撑大
            # messages 行宽并让列表/详情查询触发数据库排序内存告警。
            completed_payload = completed_advice.model_dump(mode="json")
            future = session_thread_pool.submit(
                str(user["_session_id"]),
                database.save_exchange,
                int(user["id"]),
                request.conversation_id,
                request.query,
                request.model_dump(mode="json"),
                completed_advice.conclusion,
                summarise_advice(completed_payload, used_fact_ids_of(completed_payload)),
            )
            await asyncio.wrap_future(future)
        _report_progress("已完成")
        return completed_advice
    except DatabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        # 不暴露异常细节（可能包含数据源地址或内部实现），同时保留服务端日志入口。
        # 当前本地版未配置日志器；正式环境应记录 trace_id、异常类型与脱敏上下文。
        raise HTTPException(status_code=500, detail="组合诊断服务暂时不可用，请稍后重试。") from exc


@app.post("/api/v1/portfolio/analyze/stream", tags=["advice"])
async def stream_portfolio_analysis(
    request: OrchestrationRequest,
    user: dict[str, object] | None = Depends(optional_authenticated_user),
) -> StreamingResponse:
    """逐阶段发送进度；只在全部核验完成后发送最终建议包。"""

    async def events():
        queue: asyncio.Queue[str] = asyncio.Queue()
        token = analysis_progress.set(queue.put_nowait)
        task = asyncio.create_task(analyze_portfolio(request, user))
        analysis_progress.reset(token)
        try:
            while not task.done() or not queue.empty():
                try:
                    stage = await asyncio.wait_for(queue.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
                yield json.dumps({"type": "progress", "stage": stage}, ensure_ascii=False) + "\n"
            try:
                advice = await task
            except HTTPException as exc:
                yield json.dumps({"type": "error", "message": exc.detail}, ensure_ascii=False) + "\n"
            except Exception:
                yield json.dumps({"type": "error", "message": "分析暂时无法完成，请稍后重试。"}, ensure_ascii=False) + "\n"
            else:
                yield json.dumps({"type": "result", "advice": advice.model_dump(mode="json")}, ensure_ascii=False) + "\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        events(), media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
