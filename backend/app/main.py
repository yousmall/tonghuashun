"""问策智投后端的 FastAPI 应用入口。

本文件只负责三件事：创建 Web 应用、装配主协调智能体的依赖，以及把 HTTP
请求交给协调器。意图识别、Task DAG、事实核验和合规判断均保留在各自模块，
避免 API 入口变成难以测试和维护的业务代码集合。

本版本接入的是 ``demo_agents``，只会使用请求显式传入的 ``FactRecord``。因此
它适合在快照模式下跑通 MVP 闭环；接入真实行情或大模型前，须替换为经过鉴权、
时效检查和审计记录的正式实现。
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from backend.app.agents.coordinator import (
    CoordinatorAgent,
    basic_compliance_check,
    verify_facts,
)
from backend.app.agents.demo_agents import make_fact_based_agent
from backend.app.models import AdvicePackage, OrchestrationRequest


def build_coordinator() -> CoordinatorAgent:
    """装配主协调智能体及当前 MVP 所需的专业能力。

    注册表的键必须与 ``CoordinatorAgent._specialists_for()`` 返回的 agent_id
    对应。之后替换为真实专业智能体时，只更换这里注入的 handler；API 路由与
    编排协议均不需要改动。
    """

    agent_ids = ("market", "industry", "security", "fund", "portfolio")
    agents = {
        # 每个 handler 都是异步函数，并且只根据 OrchestrationRequest 产生 AgentResult。
        agent_id: make_fact_based_agent(agent_id)
        for agent_id in agent_ids
    }
    return CoordinatorAgent(
        agents=agents,
        # 事实核验先于合规审核执行，二者都可在后续替换为正式服务实现。
        verifier=verify_facts,
        compliance_checker=basic_compliance_check,
    )


# 供 Uvicorn 加载的 ASGI 应用对象。--reload 时也会重新创建并装配协调器。
app = FastAPI(
    title="问策智投 MVP API",
    version="0.1.0",
    description="投资研究辅助演示接口；不自动交易，不构成证券投资建议。",
)

# 应用级单例：当前无外部连接池，适合 MVP。后续接数据库/Redis 时可改为 lifespan 管理。
coordinator = build_coordinator()


@app.get("/api/v1/health", tags=["system"])
async def health() -> dict[str, str]:
    """健康检查接口，供浏览器、部署平台和 README 的启动验证使用。"""

    return {"status": "ok"}


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
        return await coordinator.run(request)
    except Exception as exc:
        # 不暴露异常细节（可能包含数据源地址或内部实现），同时保留服务端日志入口。
        # 目前 MVP 未配置日志器；正式环境应记录 trace_id、异常类型与脱敏上下文。
        raise HTTPException(status_code=500, detail="组合诊断服务暂时不可用，请稍后重试。") from exc
