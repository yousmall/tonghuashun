"""问策智投后端的 FastAPI 应用入口。

本文件只负责三件事：创建 Web 应用、装配主协调智能体的依赖，以及把 HTTP
请求交给协调器。意图识别、Task DAG、事实核验和合规判断均保留在各自模块，
避免 API 入口变成难以测试和维护的业务代码集合。

本版本以确定性规则作为可重算基线，并可通过环境变量接入第三方大模型和同花顺
问财数据。默认分析入口会按意图自动获取最小必要的只读数据并转换为
``FactRecord``；外部能力异常或未配置时，系统会明确回退到规则/快照模式。
"""

from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter

from fastapi import FastAPI, HTTPException
from fastapi import Request
from dotenv import load_dotenv

from backend.app.agents.coordinator import (
    CoordinatorAgent,
    basic_compliance_check,
    verify_facts,
)
from backend.app.agents.llm_agents import make_investment_agents
from backend.app.data_provider import IwencaiSkillHubProvider
from backend.app.models import (
    AdvicePackage,
    DataFetchRequest,
    DataFetchResponse,
    OrchestrationRequest,
    ProfileAssessment,
    ProfileAssessmentRequest,
    ProfileConfirmRequest,
    UserProfile,
)
from backend.app.services import AutomatedResearchPipeline, ServiceMetrics, assess_profile, confirm_profile


# 从本地 .env 加载可选外部服务配置；生产环境中已有的环境变量优先。
load_dotenv()


def build_coordinator() -> tuple[CoordinatorAgent, bool]:
    """装配主协调智能体及当前运行模式所需的专业能力。

    注册表的键必须与 ``CoordinatorAgent._specialists_for()`` 返回的 agent_id
    对应。之后替换为真实专业智能体时，只更换这里注入的 handler；API 路由与
    编排协议均不需要改动。
    """

    # ``make_rule_agents`` 返回与 Task DAG 完全一致的五个注册键。它们不是行情源，
    # 只对请求已经授权的 FactRecord 做确定性计算，便于单测和审计回放。
    agents, llm_enabled = make_investment_agents()
    coordinator = CoordinatorAgent(
        agents=agents,
        # 事实核验先于合规审核执行，二者都可在后续替换为正式服务实现。
        verifier=verify_facts,
        compliance_checker=basic_compliance_check,
    )
    return coordinator, llm_enabled


# 供 Uvicorn 加载的 ASGI 应用对象。--reload 时也会重新创建并装配协调器。
app = FastAPI(
    title="问策智投 API",
    version="1.0.0",
    description="投资研究辅助演示接口；不自动交易，不构成证券投资建议。",
)

# 应用级单例。后续接数据库/Redis 时可改为 lifespan 管理。
coordinator, llm_enabled = build_coordinator()
data_provider = IwencaiSkillHubProvider.from_env()
research_pipeline = AutomatedResearchPipeline(data_provider)
service_metrics = ServiceMetrics()


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
    """报告可选外部能力是否就绪；缺失时基础规则模式仍保持可用。"""

    return {
        "status": "ready",
        "mode": "hybrid_llm" if llm_enabled else "rule_only",
        "third_party_llm_configured": llm_enabled,
        "iwencai_skillhub_configured": data_provider is not None,
        "automatic_data_pipeline_enabled": True,
    }


@app.get("/api/v1/metrics", tags=["system"])
async def metrics() -> dict[str, object]:
    """返回容量与可用性观测值；其范围不冒充生产 SLA。"""

    return service_metrics.snapshot()


@app.post("/api/v1/data/fetch", response_model=DataFetchResponse, tags=["data"])
async def fetch_market_data(request: DataFetchRequest) -> DataFetchResponse:
    """从问财只读接口调用项目允许的 SkillHub 能力并标准化为事实。"""

    if data_provider is None:
        raise HTTPException(status_code=503, detail="尚未配置 IWENCAI_API_KEY，当前只能使用本地快照。")
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
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return DataFetchResponse(
        provider=data_provider.source_id,
        fetched_at=datetime.now(timezone.utc),
        facts=facts,
    )


@app.post("/api/v1/profile/assess", response_model=ProfileAssessment, tags=["profile"])
async def assess_user_profile(request: ProfileAssessmentRequest) -> ProfileAssessment:
    """将问卷/文本转换为未确认画像草稿。

    返回值始终是 ``confirmed=False``；这不是可直接用于精确仓位建议的授权，
    前端应展示提取证据和缺失字段，请用户在下一步核对并确认。
    """

    return assess_profile(request)


@app.post("/api/v1/profile/confirm", response_model=UserProfile, tags=["profile"])
async def confirm_user_profile(request: ProfileConfirmRequest) -> UserProfile:
    """显式确认画像并递增版本号。

    当前演示不持久化数据；生产环境必须在鉴权后的事务内比对上一版本，防止
    并发覆盖。即使调用方传入 ``confirmed=True``，版本仍会递增以留下审计边界。
    """

    return confirm_profile(request.profile)


@app.post(
    "/api/v1/portfolio/analyze",
    response_model=AdvicePackage,
    tags=["advice"],
)
async def analyze_portfolio(request: OrchestrationRequest) -> AdvicePackage:
    """运行组合诊断闭环并返回可审计建议包。

    FastAPI 会在进入本函数前验证 ``query``、画像和事实记录的类型及边界。协调器
    会在内部完成画像确认、并行专业分析、事实核验和合规审核；若被 BLOCK，响应
    仍为 200，但 ``compliance.status`` 为 ``BLOCK`` 且不含投资建议。
    """

    try:
        intent = coordinator.understand_intent(request.query)
        prepared_request, acquisition = await research_pipeline.prepare(request, intent)
        advice = await coordinator.run(prepared_request)
        return advice.model_copy(
            update={
                "facts": prepared_request.facts,
                "data_acquisition": acquisition,
            }
        )
    except Exception as exc:
        # 不暴露异常细节（可能包含数据源地址或内部实现），同时保留服务端日志入口。
        # 当前本地版未配置日志器；正式环境应记录 trace_id、异常类型与脱敏上下文。
        raise HTTPException(status_code=500, detail="组合诊断服务暂时不可用，请稍后重试。") from exc
