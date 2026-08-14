"""主协调智能体与下游智能体共用的数据契约。

本模块只定义“数据长什么样”，不放业务决策。这样 API、数据服务、专业
智能体、事实核验器和合规器都能使用同一套对象，避免不同模块对同一个字段
使用不同名称或不同单位。所有模型均继承 Pydantic ``BaseModel``：进入系统的
外部 JSON 会先被验证，再交给编排器执行。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Intent(StrEnum):
    """主协调智能体可识别的请求类型。

    ``UNKNOWN`` 是安全兜底状态。意图无法可靠判断时，系统只能追问，不得把
    请求猜测为投资建议场景并继续执行。
    """

    MARKET_ANALYSIS = "market_analysis"
    SECURITY_RESEARCH = "security_research"
    FUND_SCREENING = "fund_screening"
    PORTFOLIO_REVIEW = "portfolio_review"
    UNKNOWN = "unknown"


class TaskStatus(StrEnum):
    """DAG 节点及专业智能体的运行状态。

    ``DEGRADED`` 与 ``FAILED`` 的差异很重要：前者表示系统仍有部分可用信息，
    后者表示该节点完全没有可采信输出。前端可据此展示不同提示。
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    DEGRADED = "degraded"
    FAILED = "failed"
    SKIPPED = "skipped"


class ComplianceStatus(StrEnum):
    """合规闸门的三种结果，按严格程度递增。"""

    PASS = "PASS"
    REVIEW = "REVIEW"
    BLOCK = "BLOCK"


class UserProfile(BaseModel):
    """经用户确认后才能用于个性化建议的画像快照。

    画像需要版本化，因为用户的期限、流动性和风险偏好可能会改变。``confirmed``
    是硬闸门；它为 ``False`` 时，协调器只会要求确认信息，不会输出建议。
    """

    # 用户唯一标识。真实系统应来自鉴权上下文，而不是由前端任意指定。
    user_id: str
    # 画像版本号，用于审计“当时到底使用了哪个画像”。
    version: int = 1
    # R1-R5 等适当性等级；第一版保留字符串以便对接现有规则库。
    risk_level: str | None = None
    # 投资期限（月）。小于 1 的值无意义，因此由 Pydantic 在入口处拒绝。
    horizon_months: int | None = Field(default=None, ge=1)
    # 最大可接受回撤，以小数存储，例如 8% 填 0.08。
    max_drawdown: float | None = Field(default=None, ge=0, le=1)
    # 如“高/中/低”的资金流动性需求；后续可收敛为枚举。
    liquidity_need: str | None = None
    # 用户明确的禁忌或硬约束，如“不买高波动资产”。
    constraints: list[str] = Field(default_factory=list)
    # 防止模型把抽取到的“画像草稿”误当成最终事实。
    confirmed: bool = False


class FactRecord(BaseModel):
    """可引用、可追溯的事实记录。

    专业智能体只能引用本对象中的数值，不能用模型记忆补充实时价格、财务数字
    或新闻事实。``fact_id`` 贯穿结果、建议包和审计日志。
    """

    # 事实的稳定 ID；结果中的 facts_used 必须引用它。
    fact_id: str
    # 事实所属实体，例如证券代码、指数或行业名称。
    entity: str
    # 实体的字段名，例如 close_price、pe_ttm 或 sector_weight。
    field: str
    # 具体值可为数值、字符串或结构化对象；具体字段的类型约束由领域层定义。
    value: Any
    # 数据抓取或快照生成时间，核验器据此判断事实是否过期。
    snapshot_time: datetime
    # 数据来源标识；演示快照必须明确使用 DEMO_SNAPSHOT，不能伪装成实时源。
    source_id: str
    # 0 到 1 的证据质量分，供冲突处理、置信度计算和界面展示使用。
    quality: float = Field(ge=0, le=1)
    # 财务/经营数据的报告期，例如 2026Q1；行情数据可为空。
    period: str | None = None


class TaskNode(BaseModel):
    """Task DAG 中一个可调度的节点。

    每个节点显式保存依赖、超时、数据时效和优先级。这些字段是主协调智能体
    输出可解释、可取消和可回放的基础，而不是临时写在代码里的隐式规则。
    """

    # DAG 内唯一 ID，用于让下游节点声明依赖关系。
    task_id: str
    # 已注册的执行者 ID，例如 market、portfolio 或 fact_verifier。
    agent_id: str
    # 必须成功或降级完成后，本节点才允许运行的 task_id 列表。
    depends_on: list[str] = Field(default_factory=list)
    # 节点最长运行时间；限制为 60 秒以避免单一服务拖垮用户请求。
    timeout_seconds: float = Field(default=8, gt=0, le=60)
    # 当前节点允许使用的事实最大年龄（秒）。
    data_max_age_seconds: int = Field(default=86_400, gt=0)
    # 1-100，数值越大越优先；合规审核通常应是最高优先级。
    priority: int = Field(default=50, ge=1, le=100)
    # 初始为 pending，运行时可被调度器写为 running/completed 等状态。
    status: TaskStatus = TaskStatus.PENDING


class TaskPlan(BaseModel):
    """一次请求对应的完整执行计划，包含追问或 DAG 二者之一。"""

    # 全链路追踪 ID，必须被写入后续事实、任务、建议和审计记录。
    trace_id: str
    # 本次请求被识别出的意图。
    intent: Intent
    # 缺少关键决策信息时的最小必要追问；存在该值时不执行 nodes。
    clarification_question: str | None = None
    # 计划中的所有节点，按依赖关系形成有向无环图。
    nodes: list[TaskNode] = Field(default_factory=list)

    @field_validator("nodes")
    @classmethod
    def task_ids_are_unique(cls, nodes: list[TaskNode]) -> list[TaskNode]:
        """拒绝重复节点 ID，避免依赖解析时指向不确定的执行者。"""
        ids = [node.task_id for node in nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("task_id 必须唯一")
        return nodes


class AgentResult(BaseModel):
    """专业智能体的统一输出。

    统一 Schema 能使不同领域的结果被安全聚合；任何没有 ``facts_used`` 的市场
    结论都会在后续合规阶段降级为仅教育性内容。
    """

    # 产出本结果的专业智能体 ID，必须与 TaskNode.agent_id 对应。
    agent_id: str
    # 成功、降级或失败状态，供聚合器决定是否计入最终置信度。
    status: TaskStatus
    # 在已授权事实范围内形成的专业判断，不应包含虚构的实时数字。
    opinion: str
    # 可选的 0-100 标准化评分；无可比评分时应保持 None。
    score: float | None = Field(default=None, ge=0, le=100)
    # 0-1 的结果可信度，必须同时给出降低原因以便解释。
    confidence: float = Field(ge=0, le=1)
    # 影响置信度的因素，例如“数据过期”“样本不足”。
    confidence_reasons: list[str] = Field(default_factory=list)
    # 本结论实际使用的 FactRecord ID；用于防幻觉检查。
    facts_used: list[str] = Field(default_factory=list)
    # 展示层使用的引用标识，通常与 source_id 或引用中心记录对应。
    citations: list[str] = Field(default_factory=list)
    # 需要在建议卡片中展示的风险点。
    risk_flags: list[str] = Field(default_factory=list)
    # 会让当前判断失效的可观察条件，支持后续复盘和监控。
    invalidation_conditions: list[str] = Field(default_factory=list)


class ComplianceResult(BaseModel):
    """合规审核结果；它是 AdvicePackage 进入界面前的最终闸门。"""

    # PASS 可展示、REVIEW 仅教育性/人工复核、BLOCK 必须拦截。
    status: ComplianceStatus
    # 命中的规则 ID，必须存储以便 trace_id 回放和规则版本审计。
    matched_rules: list[str] = Field(default_factory=list)
    # 面向用户或人工审核人的可解释原因。
    reason: str | None = None
    # 无论通过与否均可展示的标准风险提示。
    risk_notice: str | None = None


class OrchestrationRequest(BaseModel):
    """进入主协调智能体的单次请求输入。"""

    # 原始用户问题，禁止为空字符串，避免无意义调用专业智能体。
    query: str = Field(min_length=1)
    # 当前已加载的用户画像版本。
    profile: UserProfile
    # 数据层在本次请求中已授权的事实集合。
    facts: list[FactRecord] = Field(default_factory=list)
    # 用户授权导入的持仓快照；真实系统还应增加权限与敏感字段脱敏。
    portfolio: list[dict[str, Any]] = Field(default_factory=list)


class AdvicePackage(BaseModel):
    """最终返回给 API 和前端的、可审计的建议包。"""

    # 关联同一次请求的所有过程记录。
    trace_id: str
    # 用于决定展示模板和后续追问策略的意图。
    intent: Intent
    # 本次建议可引用事实中最新的时间；不可省略为“当前”。
    snapshot_time: datetime | None = None
    # 面向用户的综合结论；BLOCK 时只能说明拦截原因，不能给投资建议。
    conclusion: str
    # 已聚合的可信度；失败/无证据结果不会抬高它。
    confidence: float = Field(ge=0, le=1)
    # 去重后的 fact_id 列表，供证据中心展开。
    evidence: list[str] = Field(default_factory=list)
    # 去重后的风险提示列表。
    risks: list[str] = Field(default_factory=list)
    # 合规结果必须始终存在，防止未审核内容直接渲染。
    compliance: ComplianceResult
    # 实际执行/未执行的 TaskPlan，便于前端显示协作过程。
    task_plan: TaskPlan
    # 各专业智能体的原始标准化结果，便于保留分歧而非强行投票。
    agent_results: list[AgentResult] = Field(default_factory=list)
