"""主协调智能体与下游智能体共用的数据契约。

本模块只定义“数据长什么样”，不放业务决策。这样 API、数据服务、专业
智能体、事实核验器和合规器都能使用同一套对象，避免不同模块对同一个字段
使用不同名称或不同单位。所有模型均继承 Pydantic ``BaseModel``：进入系统的
外部 JSON 会先被验证，再交给编排器执行。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class Intent(StrEnum):
    """主协调智能体可识别的请求类型。

    ``UNKNOWN`` 是安全兜底状态。意图无法可靠判断时，系统只能追问，不得把
    请求猜测为投资建议场景并继续执行。
    """

    MARKET_ANALYSIS = "market_analysis"
    INDUSTRY_ANALYSIS = "industry_analysis"
    SECURITY_RESEARCH = "security_research"
    FUND_SCREENING = "fund_screening"
    CONVERTIBLE_BOND_ANALYSIS = "convertible_bond_analysis"
    PORTFOLIO_REVIEW = "portfolio_review"
    EDUCATION = "education"
    UNKNOWN = "unknown"


class TaskStatus(StrEnum):
    """DAG 节点及专业智能体的运行状态。

    ``DEGRADED`` 与 ``FAILED`` 的差异很重要：前者表示系统仍有部分可用信息，
    后者表示该节点完全没有可采信输出。前端可据此展示不同提示。
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    # 没有足够事实时，UNKNOWN 比 FAILED 更准确：它表示系统没有声称知道答案。
    UNKNOWN = "unknown"
    DEGRADED = "degraded"
    FAILED = "failed"
    SKIPPED = "skipped"


class ComplianceStatus(StrEnum):
    """合规闸门的三种结果，按严格程度递增。"""

    PASS = "PASS"
    REVIEW = "REVIEW"
    BLOCK = "BLOCK"


class VerificationStatus(StrEnum):
    """事实核验器的结论。

    专业智能体的 ``TaskStatus`` 描述“任务是否完成”；本枚举描述“输出是否可以
    被事实层接受”。两者分开可以避免把“执行成功但证据需要修改”误显示为成功。
    """

    PASS = "PASS"
    REVISE = "REVISE"
    REJECT = "REJECT"


class UserProfile(BaseModel):
    """经用户确认后才能用于个性化建议的画像快照。

    画像需要版本化，因为用户的期限、流动性和风险偏好可能会改变。``confirmed``
    是硬闸门；它为 ``False`` 时，协调器只会要求确认信息，不会输出建议。
    """

    # 用户唯一标识。已登录请求由鉴权上下文写入，前端不需要也看不到该字段。
    user_id: str = Field(default="", max_length=128)
    # 画像版本号，用于审计“当时到底使用了哪个画像”。
    version: int = 1
    # R1-R5 等适当性等级；第一版保留字符串以便对接现有规则库。
    risk_level: str | None = None
    # 0-100 的问卷分数。risk_level 是可读分层，risk_score 保留可解释的原始分。
    risk_score: float | None = Field(default=None, ge=0, le=100)
    # 投资期限（月）。小于 1 的值无意义，因此由 Pydantic 在入口处拒绝。
    horizon_months: int | None = Field(default=None, ge=1)
    # 最大可接受回撤，以小数存储，例如 8% 填 0.08。
    max_drawdown: float | None = Field(default=None, ge=0, le=1)
    # 如“高/中/低”的资金流动性需求；后续可收敛为枚举。
    liquidity_need: str | None = None
    # 用户明确的禁忌或硬约束，如“不买高波动资产”。
    constraints: list[str] = Field(default_factory=list)
    # 投资目标，例如“2 年后购房”；它会影响组合建议的流动性约束。
    target: str | None = None
    # 画像不仅记录风险问卷，也保留与适当性直接相关的真实经历和收益目标。
    # 这些字段均由用户提供或确认，系统不得从风险等级反向猜测。
    investment_experience_years: float | None = Field(default=None, ge=0, le=100)
    investment_history: list[str] = Field(default_factory=list)
    holding_history: list[dict[str, Any]] = Field(default_factory=list)
    expected_annual_return: float | None = Field(default=None, ge=-1, le=5)
    behavioral_notes: list[str] = Field(default_factory=list)
    # 两类上限是组合/合规可执行的硬约束，不由语言模型自行决定。
    single_security_limit: float = Field(default=0.20, gt=0, le=1)
    industry_limit: float = Field(default=0.30, gt=0, le=1)
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
    # 规则派生事实记录输入 fact_id；供应商原始事实保持为空。
    derived_from: list[str] = Field(default_factory=list)

    @field_validator("snapshot_time")
    @classmethod
    def snapshot_time_must_include_timezone(cls, value: datetime) -> datetime:
        """拒绝无时区时间，防止时效核验把本地时间错误当作 UTC。"""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("snapshot_time 必须包含时区，例如 2026-08-29T08:00:00Z")
        return value.astimezone(timezone.utc)


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

    @model_validator(mode="after")
    def dependencies_form_valid_dag(self) -> "TaskPlan":
        """拒绝悬空、自依赖和循环依赖，确保计划可被拓扑调度。"""
        dependencies = {node.task_id: set(node.depends_on) for node in self.nodes}
        known_ids = set(dependencies)
        for task_id, required_ids in dependencies.items():
            unknown = required_ids - known_ids
            if unknown:
                raise ValueError(f"任务 {task_id!r} 引用了不存在的依赖: {sorted(unknown)}")
            if task_id in required_ids:
                raise ValueError(f"任务 {task_id!r} 不能依赖自身")

        # 逐轮移除已无前置依赖的节点；若最终仍有剩余节点，说明依赖图中存在环。
        remaining = {task_id: set(required_ids) for task_id, required_ids in dependencies.items()}
        while remaining:
            ready = {task_id for task_id, required_ids in remaining.items() if not required_ids}
            if not ready:
                raise ValueError(f"TaskPlan 必须是无环图，循环涉及: {sorted(remaining)}")
            remaining = {
                task_id: required_ids - ready
                for task_id, required_ids in remaining.items()
                if task_id not in ready
            }
        return self


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
    # 供前端展示的确定性明细（如评分维度、集中度）。不得放未引用的市场数字。
    details: dict[str, Any] = Field(default_factory=dict)


class VerificationResult(BaseModel):
    """事实层对一次专业输出的结构化审查记录。

    当前 MVP 将可展示的修订结果继续传给合规器，同时保留本模型，供后续写入
    ``verification_record`` / ``audit_log`` 表并支持 trace_id 回放。
    """

    status: VerificationStatus
    issues: list[str] = Field(default_factory=list)
    recomputed_values: dict[str, float] = Field(default_factory=dict)
    missing_evidence_ids: list[str] = Field(default_factory=list)
    confidence_penalty: float = Field(default=0, ge=0, le=1)


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
    # 规则版本让每次命中结果能够按 trace_id 回放，而不是依赖当时的代码记忆。
    rule_version: str = "mvp-1.0"
    # 前端必须展示的补充披露；BLOCK/REVIEW 时同样需要保留。
    required_disclosures: list[str] = Field(default_factory=list)


class OrchestrationRequest(BaseModel):
    """进入主协调智能体的单次请求输入。"""

    # 原始用户问题，禁止为空字符串，避免无意义调用专业智能体。
    query: str = Field(min_length=1)
    # 当前已加载的用户画像版本。
    profile: UserProfile
    # 数据层在本次请求中已授权的事实集合。
    facts: list[FactRecord] = Field(default_factory=list)
    # 默认由后端按意图自动补充真实数据；显式关闭时只使用调用方提供的事实。
    auto_fetch: bool = True
    # 用户授权导入的持仓快照；真实系统还应增加权限与敏感字段脱敏。
    portfolio: list[dict[str, Any]] = Field(default_factory=list)
    # 最近对话由客户端显式传入，既支持多轮理解，也避免服务端跨用户串话。
    conversation_id: str | None = Field(default=None, max_length=128)
    context_messages: list["ConversationTurn"] = Field(default_factory=list, max_length=20)

    @field_validator("query")
    @classmethod
    def query_must_contain_visible_text(cls, value: str) -> str:
        """去除首尾空白并拒绝纯空白问题，避免创建无意义的执行计划。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("query 不能为空或仅包含空白字符")
        return normalized

    @field_validator("facts")
    @classmethod
    def fact_ids_are_unique(cls, facts: list[FactRecord]) -> list[FactRecord]:
        """拒绝重复事实 ID，保证证据引用能唯一回溯到一条快照记录。"""
        ids = [fact.fact_id for fact in facts]
        if len(ids) != len(set(ids)):
            raise ValueError("fact_id 必须唯一")
        return facts


class ProfileAssessmentRequest(BaseModel):
    """画像评估入口：支持问卷分项和自然语言，两者均只生成草稿。"""

    # 已登录请求由鉴权上下文覆盖，客户端无需也无需知道自己的账号标识。
    user_id: str = Field(default="", max_length=128)
    # 问卷维度使用 0-100；未知维度省略，服务会列入 missing_fields。
    questionnaire: dict[str, float] = Field(default_factory=dict)
    narrative: str | None = Field(default=None, max_length=2_000)
    investment_experience_years: float | None = Field(default=None, ge=0, le=100)
    investment_history: list[str] = Field(default_factory=list, max_length=50)
    holding_history: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    expected_annual_return: float | None = Field(default=None, ge=-1, le=5)

    @field_validator("questionnaire")
    @classmethod
    def questionnaire_scores_are_valid(cls, values: dict[str, float]) -> dict[str, float]:
        """拒绝越界分数，避免错误输入被悄悄映射为风险等级。"""
        for name, value in values.items():
            if not 0 <= value <= 100:
                raise ValueError(f"问卷维度 {name!r} 必须在 0-100 之间")
        return values


class ProfileAssessment(BaseModel):
    """画像草稿及其证据，客户端必须显式确认后才可用于个性化建议。"""

    profile: UserProfile
    missing_fields: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class ProfileConfirmRequest(BaseModel):
    """确认端点的显式包装，避免误把 assess 返回值自动当作已确认画像。"""

    profile: UserProfile


class DataFetchRequest(BaseModel):
    """从已配置金融数据源拉取一类只读数据。"""

    kind: Literal[
        "quote",
        "financial",
        "news",
        "fund",
        "industry",
        "convertible",
        "basic_info",
        "company_operations",
        "shareholder_equity",
        "event",
        "macro",
        "institutional_research",
        "research_report",
        "announcement",
        "stock_screen",
        "sector_screen",
    ]
    target: str = Field(min_length=1, max_length=500)
    filters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("target")
    @classmethod
    def target_must_contain_visible_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("target 不能为空")
        return normalized


class DataFetchResponse(BaseModel):
    """数据源查询结果及其真实来源状态。"""

    provider: str
    fetched_at: datetime
    facts: list[FactRecord] = Field(default_factory=list)


class ConversationTurn(BaseModel):
    """一次可控的多轮上下文输入；只允许用户与助手文本。"""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4_000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("content")
    @classmethod
    def content_must_contain_visible_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("对话内容不能为空")
        return normalized


class CrossValidationIssue(BaseModel):
    """跨智能体或跨来源一致性核验发现的一项问题。"""

    code: str
    severity: Literal["info", "warning", "critical"]
    message: str
    agent_ids: list[str] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)


class CrossValidationResult(BaseModel):
    """跨智能体一致性、来源冲突和协调器共识摘要。"""

    status: ComplianceStatus = ComplianceStatus.PASS
    consensus_score: float | None = Field(default=None, ge=0, le=100)
    confidence: float = Field(default=0, ge=0, le=1)
    issues: list[CrossValidationIssue] = Field(default_factory=list)
    supporting_agents: list[str] = Field(default_factory=list)
    dissenting_agents: list[str] = Field(default_factory=list)


class DataAcquisitionResult(BaseModel):
    """自动取数阶段的审计摘要，不包含密钥、原始请求头或内部异常文本。"""

    mode: Literal["provided", "live", "mixed", "unavailable", "not_required"] = "not_required"
    provider: str | None = None
    requested_capabilities: list[str] = Field(default_factory=list)
    successful_capabilities: list[str] = Field(default_factory=list)
    empty_capabilities: list[str] = Field(default_factory=list)
    failed_capabilities: list[str] = Field(default_factory=list)
    supplied_fact_count: int = Field(default=0, ge=0)
    fetched_fact_count: int = Field(default=0, ge=0)
    derived_fact_count: int = Field(default=0, ge=0)
    # 送入模型研判的事实条数与是否被裁剪：让"取数成功但研判降级"可解释。
    model_fact_count: int = Field(default=0, ge=0)
    model_fact_available: int = Field(default=0, ge=0)
    facts_truncated: bool = False
    # 结构化降级原因码（不含内部实现细节），供界面翻译成用户可理解的话术。
    reason_code: str | None = None
    message: str | None = None


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
    # 包含调用方事实、自动取数事实和规则派生事实，供证据中心完整回放。
    facts: list[FactRecord] = Field(default_factory=list)
    # 单独记录自动取数是否成功，防止界面把演示/手工事实误标为实时数据。
    data_acquisition: DataAcquisitionResult = Field(default_factory=DataAcquisitionResult)
    # 去重后的风险提示列表。
    risks: list[str] = Field(default_factory=list)
    # 画像与本次结论的适配摘要；不足时应说明不适配/待确认，而非给精确仓位。
    user_fit: str | None = None
    # MVP 只允许目标区间或诊断摘要，严禁被当作自动交易指令。
    allocation: list[dict[str, Any]] = Field(default_factory=list)
    # 没有足够证据时，明确告诉用户补什么数据或采取何种教育性下一步。
    next_steps: list[str] = Field(default_factory=list)
    # 合规结果必须始终存在，防止未审核内容直接渲染。
    compliance: ComplianceResult
    # 实际执行/未执行的 TaskPlan，便于前端显示协作过程。
    task_plan: TaskPlan
    # 各专业智能体的原始标准化结果，便于保留分歧而非强行投票。
    agent_results: list[AgentResult] = Field(default_factory=list)
    # 单独保留一致性审查，避免把专业分歧藏进一个平均分。
    cross_validation: CrossValidationResult = Field(default_factory=CrossValidationResult)
