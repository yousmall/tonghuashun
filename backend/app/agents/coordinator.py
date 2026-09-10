"""主协调智能体：规划、调度、核验和合规闸门。

本文件承担“项目经理”而非“证券分析师”的角色：它识别问题类型，确认用户
画像是否可用，生成可审计的任务图并调度下游能力。它不拥有市场事实，也不应
直接生成具体行情数字；所有此类数字必须由调用方注入的 ``FactRecord`` 提供。

执行顺序固定为：意图识别 → 画像闸门 → 专业节点并行 → 事实核验 → 合规审核
→ 建议包。这样即便专业智能体返回了看似合理的文字，也无法绕过事实和合规
控制直接出现在最终页面。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from backend.app.semantic import RequestUnderstanding, SemanticService
from backend.app import fact_taxonomy as _TAXONOMY
from backend.app.services.model_input import slice_facts_for_model

from backend.app.models import (
    AdvicePackage,
    AgentResult,
    ComplianceResult,
    ComplianceStatus,
    CrossValidationIssue,
    CrossValidationResult,
    FactRecord,
    Intent,
    OrchestrationRequest,
    TaskNode,
    TaskPlan,
    TaskStatus,
    UserProfile,
)

# 专业智能体的统一调用签名。实际实现可以是规则函数、远程模型调用或工作流，
# 但无论实现细节如何，输出都必须收敛为 AgentResult。
AgentHandler = Callable[[OrchestrationRequest], Awaitable[AgentResult]]
# 核验器在专业分析完成后运行；它可以修改结果状态、置信度和可引用事实集合。
Verifier = Callable[[list[AgentResult], list[FactRecord]], Awaitable[list[AgentResult]]]
# 合规器拥有最终拦截权；它只审查请求和已核验结果，不负责生成新的投资观点。
ComplianceChecker = Callable[[OrchestrationRequest, list[AgentResult]], Awaitable[ComplianceResult]]

# 固定风险提示与具体分析内容分离，保证通过、复核和拦截场景都能复用同一口径。
RISK_NOTICE = (
    "以上内容基于所示数据时点和公开/授权信息生成，仅用于投资研究辅助，"
    "不构成收益承诺或交易指令；请结合自身风险承受能力和独立判断审慎决策。"
)


class CoordinatorAgent:
    """将用户请求转换为可审计的 DAG，并安全执行其节点。

    专业智能体只能从 ``facts`` 中取数字；本类不会创造市场事实，也不会
    在核验或合规闸门失败后返回投资建议。
    """

    def __init__(
        self,
        agents: dict[str, AgentHandler],
        verifier: Verifier,
        compliance_checker: ComplianceChecker,
        *,
        now: Callable[[], datetime] | None = None,
        semantic: SemanticService | None = None,
    ) -> None:
        # 注册表由应用启动时装配。仅计划并执行已注册的 agent_id，避免由用户输入
        # 间接调用任意代码或不存在的“智能体”。
        self.agents = agents
        self.semantic = semantic or SemanticService()
        # 最近一次编排送入模型的事实切片摘要；由 API 层并入 data_acquisition，
        # 让"取数成功但研判降级"对用户可解释。
        self.last_model_slice: dict[str, Any] = {}
        # 编排超时必须覆盖模型的排队、网络与重试总预算，否则会在模型响应前提前降级。
        llm_config = getattr(self.semantic.llm, "config", None)
        self._llm_node_timeout = min(60.0, max(8.0, getattr(llm_config, "timeout_seconds", 6.0) + 1.0))
        # 将核验与合规以依赖注入方式传入，便于替换为规则库、人工审核或服务调用。
        self.verifier = verifier
        self.compliance_checker = compliance_checker
        # 注入时间函数方便单元测试固定时钟；当前最小核验器仍使用 UTC 当前时间。
        self.now = now or (lambda: datetime.now(timezone.utc))

    async def understand_request(self, request: OrchestrationRequest) -> RequestUnderstanding:
        """同时识别上下文意图和请求风险；调用方应传递结果而非重复调用。"""
        return await self.semantic.understand(request)

    async def understand_intent(self, query: str) -> Intent:
        """便捷入口；多轮分析应调用 understand_request 并传入完整请求。"""
        request = OrchestrationRequest(query=query, profile=UserProfile(user_id="intent-only"))
        return (await self.understand_request(request)).intent

    def plan(self, request: OrchestrationRequest, trace_id: str, intent: Intent = Intent.UNKNOWN) -> TaskPlan:
        """为已确认请求生成最小必要的 Task DAG。

        规划本身不调用数据服务或模型，因此速度快、易测试。它只描述“要做什么”，
        具体“怎么执行”由 :meth:`run` 根据节点关系实施。
        """
        if intent is Intent.UNKNOWN:
            # 意图不明确会实质改变所需数据和智能体，属于必须追问的情况。
            return TaskPlan(
                trace_id=trace_id,
                intent=intent,
                clarification_question="请说明您希望进行市场解读、标的研究、基金筛选还是组合诊断。",
            )
        if not request.profile.confirmed:
            # 画像未确认时，不能以模型抽取的草稿替用户决定风险与仓位。
            return TaskPlan(
                trace_id=trace_id,
                intent=intent,
                clarification_question="请先确认风险等级、投资期限、最大可接受回撤和流动性需求，再生成个性化建议。",
            )

        # 根据意图只选择必要专业能力，避免无关调用增加时延、成本和信息噪声。
        specialist_ids = self._specialists_for(intent)
        nodes = [
            TaskNode(
                # task_id 是 DAG 的依赖键；agent_id 是注册表中真正的执行器名称。
                task_id=f"{agent_id}-analysis",
                agent_id=agent_id,
                # 所有专业节点相互独立，因此没有 depends_on，可被并行调度。
                timeout_seconds=self._llm_node_timeout,
                # 行情类事实变化快，设置为 15 分钟；财务等慢变量默认允许一天。
                data_max_age_seconds=900 if agent_id == "market" else 86_400,
                # 专业分析优先级低于核验和合规，后两者必须拥有最终控制权。
                priority=80,
            )
            for agent_id in specialist_ids
            # 配置缺失的专业能力不会被写入计划，防止运行时 KeyError。
            if agent_id in self.agents
        ]
        nodes.extend(
            [
                TaskNode(
                    task_id="fact-verification",
                    agent_id="fact_verifier",
                    # 核验必须等待全部已计划专业节点结束，再检查其引用是否可信。
                    depends_on=[node.task_id for node in nodes],
                    timeout_seconds=5,
                    data_max_age_seconds=86_400,
                    priority=95,
                ),
                TaskNode(
                    task_id="compliance-review",
                    agent_id="compliance",
                    # 合规只接收核验后的结果，避免未证实结论进入规则判断。
                    depends_on=["fact-verification"],
                    timeout_seconds=self._llm_node_timeout,
                    data_max_age_seconds=86_400,
                    priority=100,
                ),
            ]
        )
        return TaskPlan(trace_id=trace_id, intent=intent, nodes=nodes)

    async def run(
        self, request: OrchestrationRequest, *, understanding: RequestUnderstanding | None = None,
    ) -> AdvicePackage:
        """按安全顺序执行计划，并始终返回可审计的 AdvicePackage。

        函数没有抛出某个专业节点的异常给前端：节点异常会被转换为失败结果，其他
        节点仍可完成。只有合规 BLOCK 才会阻断最终建议；此时返回的是拦截说明，
        而不是把已生成的分析文字悄悄混入结论。
        """
        # UUID 只截取 12 位是为了展示友好；真实审计数据库可保存完整 UUID。
        trace_id = f"T-{uuid4().hex[:12].upper()}"
        understanding = understanding or await self.understand_request(request)
        plan = self.plan(request, trace_id, understanding.intent)
        if understanding.risk_rules:
            # 在执行专业节点前处理风险；API 同时据此跳过外部取数。
            compliance = semantic_compliance(understanding.risk_rules, understanding.reason)
            plan.nodes = []
            output = self._review_package(plan, compliance.reason or "请求需要复核")
            return output.model_copy(update={"compliance": compliance, "confidence": 0})
        if understanding.intent is Intent.UNKNOWN:
            plan.clarification_question = understanding.reason + " 请说明研究对象与希望解决的问题。"

        if plan.clarification_question:
            # 追问不是异常，是刻意的安全业务结果，使用 REVIEW 状态返回给界面。
            return self._review_package(plan, plan.clarification_question)

        # 这里仅选择注册的专业节点。事实核验和合规在后面严格按依赖顺序单独运行。
        specialist_nodes = [node for node in plan.nodes if node.agent_id in self.agents]
        # 只把与本次意图最相关的事实子集送给模型：全量证据包可能上千条，会直接
        # 超出模型输入预算并让所有节点静默回退规则引擎。核验仍使用完整事实集。
        model_facts, slice_metrics = slice_facts_for_model(request.facts, plan.intent)
        self.last_model_slice = slice_metrics
        model_view = _RequestView(request, model_facts)
        results = await self._run_parallel(model_view, specialist_nodes)
        # 专业节点的最终状态必须回写到 DAG，前端才能区分完成、失败与降级。
        for node, result in zip(specialist_nodes, results, strict=True):
            node.status = result.status
        # 核验器可移除不存在/过期的 fact_id，并同步降低结果置信度。
        verification_node = next(node for node in plan.nodes if node.agent_id == "fact_verifier")
        verification_node.status = TaskStatus.RUNNING
        results = await self.verifier(results, request.facts)
        verification_node.status = TaskStatus.COMPLETED
        # 合规器使用核验后的结果作最终判定，保证无来源结论无法被放行。
        compliance_node = next(node for node in plan.nodes if node.agent_id == "compliance")
        compliance_node.status = TaskStatus.RUNNING
        cross_validation = cross_validate_results(results, request.facts)
        compliance = await self.compliance_checker(request, results)
        # 所有专业意见合并为一次语义审核，补充数值一致性检查无法发现的实质矛盾。
        if compliance.status is not ComplianceStatus.BLOCK:
            review = await self.semantic.review(request, results)
            if review is None:
                semantic_result = ComplianceResult(
                    status=ComplianceStatus.REVIEW, matched_rules=["SEMANTIC_REVIEW_UNAVAILABLE"],
                    reason="语义复核不可用或不确定，需要人工复核。",
                    risk_notice=RISK_NOTICE, required_disclosures=[RISK_NOTICE],
                )
            else:
                semantic_result = semantic_compliance(review.risk_rules, review.reason)
                if review.conflicting_agents:
                    cross_validation.status = ComplianceStatus.REVIEW
                    cross_validation.issues.append(CrossValidationIssue(
                        code="SEMANTIC_AGENT_CONFLICT", severity="warning", message=review.reason,
                        agent_ids=review.conflicting_agents,
                    ))
                    cross_validation.dissenting_agents = sorted(
                        set(cross_validation.dissenting_agents) | set(review.conflicting_agents)
                    )
            compliance = merge_compliance(compliance, semantic_result)
        if cross_validation.status is ComplianceStatus.REVIEW and compliance.status is ComplianceStatus.PASS:
            compliance = compliance.model_copy(
                update={
                    "status": ComplianceStatus.REVIEW,
                    "matched_rules": [*compliance.matched_rules, "CROSS_AGENT_INCONSISTENCY"],
                    "reason": "跨智能体或跨来源一致性检查发现分歧，需要人工复核。",
                }
            )
        compliance_node.status = TaskStatus.COMPLETED

        if compliance.status is ComplianceStatus.BLOCK:
            # BLOCK 是硬拦截：不执行综合摘要，也不把专业观点组合成建议。
            return AdvicePackage(
                trace_id=trace_id,
                intent=plan.intent,
                conclusion="请求未通过合规或适当性审核，未生成投资建议。",
                confidence=0,
                risks=[compliance.reason or "触发合规拦截"],
                user_fit="不适配：请求未通过合规或适当性审核。",
                next_steps=["调整问题表述或补充已确认画像后重试"],
                compliance=compliance,
                task_plan=plan,
                agent_results=results,
                cross_validation=cross_validation,
            )

        # 失败节点不参与置信度平均；降级节点会保留，但其低置信度会拉低总分。
        usable = [result for result in results if result.status is not TaskStatus.FAILED]
        # 用集合去重后排序，令同样输入获得稳定的证据/风险展示顺序。
        evidence = sorted({fact_id for result in usable for fact_id in result.facts_used})
        risks = sorted({flag for result in usable for flag in result.risk_flags})
        # 这是第一版的等权平均；生产版可按数据质量、时效、领域权重重新加权。
        confidence = sum(result.confidence for result in usable) / len(usable) if usable else 0
        # 永远显式返回数据时点，前端不得把它改写为含糊的“当前”。
        snapshot_time = max((fact.snapshot_time for fact in request.facts), default=None)
        conclusion = self._summarize(usable, compliance.status, cross_validation)
        # 只有画像已确认时才会走到此处；仍只输出目标区间/诊断而非自动交易指令。
        user_fit = self._user_fit_summary(request)
        allocation = self._allocation_summary(usable, request)
        next_steps = self._next_steps(results, compliance.status)
        return AdvicePackage(
            trace_id=trace_id,
            intent=plan.intent,
            snapshot_time=snapshot_time,
            conclusion=conclusion,
            confidence=round(confidence, 2),
            evidence=evidence,
            risks=risks,
            user_fit=user_fit,
            allocation=allocation,
            next_steps=next_steps,
            compliance=compliance,
            task_plan=plan,
            agent_results=results,
            cross_validation=cross_validation,
        )

    async def _run_parallel(
        self, request: OrchestrationRequest, nodes: list[TaskNode]
    ) -> list[AgentResult]:
        """运行没有前置依赖的专业节点，并将单点故障局部化。"""

        async def execute(node: TaskNode) -> AgentResult:
            # plan() 已确保 agent_id 存在于注册表；在此处读取不会暴露用户可控索引。
            handler = self.agents[node.agent_id]
            # RUNNING 只在协程真正开始时设置，避免把等待中的节点误展示为执行中。
            node.status = TaskStatus.RUNNING
            try:
                # wait_for 强制执行节点自己的超时预算，不允许慢模型拖住整条链路。
                return await asyncio.wait_for(handler(request), timeout=node.timeout_seconds)
            except TimeoutError:
                # 超时仍是一个可审计的业务状态，其他 asyncio.gather 任务不会被取消。
                return AgentResult(
                    agent_id=node.agent_id,
                    status=TaskStatus.DEGRADED,
                    opinion="分析超时，已局部降级。",
                    confidence=0,
                    confidence_reasons=["节点超时"],
                    risk_flags=["信息不完整"],
                )
            except Exception as exc:
                # 只记录异常类型，不把堆栈、密钥或底层服务细节泄露给最终用户。
                # 返回 FAILED 使前端可展示缺口，聚合器也会自动排除该结果。
                return AgentResult(
                    agent_id=node.agent_id,
                    status=TaskStatus.FAILED,
                    opinion="分析不可用。",
                    confidence=0,
                    confidence_reasons=[f"执行失败：{type(exc).__name__}"],
                    risk_flags=["信息不完整"],
                )

        # gather 保持输入节点顺序，同时真正并发等待所有互不依赖的专业分析。
        return list(await asyncio.gather(*(execute(node) for node in nodes)))

    @staticmethod
    def _specialists_for(intent: Intent) -> tuple[str, ...]:
        """定义各意图所需的最小专业能力集合。

        返回元组而非集合以固定计划显示顺序。新增意图时应同时补充该映射、测试和
        对应的合规规则，不能仅新增一个自由输出的专业智能体。
        """
        mapping = {
            Intent.MARKET_ANALYSIS: ("market", "industry"),
            Intent.INDUSTRY_ANALYSIS: ("market", "industry"),
            Intent.SECURITY_RESEARCH: ("market", "industry", "security"),
            Intent.FUND_SCREENING: ("market", "fund"),
            Intent.CONVERTIBLE_BOND_ANALYSIS: ("market", "industry", "security"),
            Intent.PORTFOLIO_REVIEW: ("market", "industry", "security", "fund", "portfolio"),
            # 教育类问题没有充分画像/事实时不应伪装成专业研究，直接进入 REVIEW。
            Intent.EDUCATION: (),
        }
        return mapping[intent]

    def _review_package(self, plan: TaskPlan, reason: str) -> AdvicePackage:
        """把必要追问包装为统一响应，供前端按 REVIEW 状态展示。"""
        return AdvicePackage(
            trace_id=plan.trace_id,
            intent=plan.intent,
            conclusion=reason,
            confidence=0,
            user_fit="待确认：尚未具备生成个性化建议的必要条件。",
            next_steps=["确认画像或补充希望分析的对象与授权事实"],
            compliance=ComplianceResult(
                status=ComplianceStatus.REVIEW,
                reason=reason,
                risk_notice=RISK_NOTICE,
            ),
            task_plan=plan,
        )

    @staticmethod
    def _summarize(
        results: list[AgentResult],
        status: ComplianceStatus,
        cross_validation: CrossValidationResult,
    ) -> str:
        """在不篡改专业分歧的前提下，生成最小可读的聚合结论。

        此处故意不做投票或“取平均意见”：基本面偏正面、技术面过热等冲突应作为
        不同观点保留给用户与合规器，而非伪造一个单一确定结论。
        """
        if not results:
            # 无可用结果时必须明确证据不足，而不是输出空的“建议”。
            return "证据不足，暂不输出投资建议。"
        # 节点标识和分数保留在结构化结果中，用户摘要按主题分段。
        labels = {"market": "市场", "industry": "行业", "security": "个股", "fund": "基金", "portfolio": "持仓"}
        opinions = "\n\n".join(
            f"{labels.get(result.agent_id, '分析')}：{result.opinion}" for result in results
        )
        prefix = "仍需进一步确认，以下内容仅供参考。\n\n" if status is ComplianceStatus.REVIEW else ""
        return f"{prefix}{opinions}"

    @staticmethod
    def _user_fit_summary(request: OrchestrationRequest) -> str:
        """用已确认字段生成适配说明，不把风险等级解释成收益预期。"""
        profile = request.profile
        parts = [f"已确认画像 {profile.risk_level or '未量化'}"]
        if profile.horizon_months is not None:
            parts.append(f"期限 {profile.horizon_months} 个月")
        if profile.max_drawdown is not None:
            parts.append(f"最大回撤 {profile.max_drawdown:.0%}")
        if profile.liquidity_need:
            parts.append(f"流动性需求 {profile.liquidity_need}")
        if profile.investment_experience_years is not None:
            parts.append(f"投资经验 {profile.investment_experience_years:g} 年")
        if profile.expected_annual_return is not None:
            parts.append(f"期望年化收益 {profile.expected_annual_return:.1%}")
        return "；".join(parts)

    @staticmethod
    def _allocation_summary(
        results: list[AgentResult], request: OrchestrationRequest
    ) -> list[dict[str, object]]:
        """按已确认风险等级输出资产类别区间，不生成具体交易指令。"""

        ranges = {
            "R1": ((0, 20), (60, 90), (10, 30)),
            "R2": ((20, 40), (40, 70), (10, 25)),
            "R3": ((40, 60), (25, 50), (5, 20)),
            "R4": ((60, 80), (10, 30), (5, 15)),
            "R5": ((75, 95), (0, 20), (0, 10)),
        }
        for result in results:
            if result.agent_id == "portfolio" and result.details:
                profile_ranges = ranges.get(request.profile.risk_level or "", ranges["R3"])
                return [
                    {
                        "asset_class": name,
                        "min_weight": lower / 100,
                        "max_weight": upper / 100,
                        "basis": f"已确认画像 {request.profile.risk_level or '未量化'}；仅作目标区间",
                    }
                    for name, (lower, upper) in zip(
                        ("权益类", "固收类", "现金及低波动类"),
                        profile_ranges,
                        strict=True,
                    )
                ]
        return []

    @staticmethod
    def _next_steps(results: list[AgentResult], status: ComplianceStatus) -> list[str]:
        """把降级原因转化为可操作下一步，避免用户只看到模糊的低置信度。"""
        steps: list[str] = []
        if status is ComplianceStatus.REVIEW:
            steps.append("补充有来源、含时间戳的事实后重新核验")
        if any(result.status in {TaskStatus.DEGRADED, TaskStatus.UNKNOWN} for result in results):
            steps.append("补齐各专业智能体列出的缺失字段")
        if any(result.risk_flags for result in results):
            steps.append("在执行任何调整前复核风险标记和证伪条件")
        return steps or ["关注证据时点与证伪条件，定期复核"]


FAST_MARKET_FIELDS = _TAXONOMY.FAST_MARKET_FIELDS
NEWS_FIELDS = _TAXONOMY.NEWS_FIELDS
SCORE_FIELDS = _TAXONOMY.SCORE_FIELDS
SLOW_FINANCIAL_FIELDS = _TAXONOMY.SLOW_FINANCIAL_FIELDS
PORTFOLIO_FIELDS = _TAXONOMY.PORTFOLIO_FIELDS
FIELD_LABELS = _TAXONOMY.FIELD_LABELS


class _RequestView:
    """把编排请求替换为"送给模型的事实子集"的只读视图。

    只替换 ``facts``，其余字段（画像、问题、持仓、上下文）沿用原请求。这样专业
    节点看到的是裁剪后的输入，而事实核验仍使用完整证据包。
    """

    __slots__ = ("_request", "facts")

    def __init__(self, request: Any, facts: list[FactRecord]) -> None:
        object.__setattr__(self, "_request", request)
        object.__setattr__(self, "facts", facts)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_request"), name)


def entity_label(entity: Any) -> str:
    """把实体内部标识转成面向用户的称呼。"""
    text = str(entity or "").strip()
    if not text:
        return "该对象"
    if "宏观" in text:
        return "宏观环境"
    if "行业排名" in text or text == "行业":
        return "行业整体"
    if "基金ETF" in text:
        return "基金与 ETF"
    return text


def field_label(field: Any) -> str:
    """把字段编码转成面向用户的中文标签；未知字段不暴露内部编码。"""
    code = str(field or "").strip()
    if not code:
        return "这项数据"
    if re.search(r"[\u4e00-\u9fff]", code):
        return code
    return FIELD_LABELS.get(code.casefold(), "该项指标")


def fact_max_age_seconds(fact: FactRecord) -> int:
    """按数据类型返回可接受最大年龄，避免用统一 7 天阈值处理行情和财报。"""

    field = fact.field.casefold()
    if field in FAST_MARKET_FIELDS:
        return 60
    if field in NEWS_FIELDS:
        return 300
    if field in SCORE_FIELDS:
        return 900
    if field in PORTFOLIO_FIELDS:
        return 86_400
    if field in SLOW_FINANCIAL_FIELDS:
        return 90 * 86_400
    return 7 * 86_400


def cross_validate_results(results: list[AgentResult], facts: list[FactRecord]) -> CrossValidationResult:
    """检查跨来源事实冲突和跨 Agent 评分分歧，并生成置信加权共识。"""

    issues: list[CrossValidationIssue] = []
    grouped: dict[tuple[str, str, str | None], list[FactRecord]] = {}
    for fact in facts:
        # 分组键中的 period 同时承载两种含义：财务数据的报告期，以及综合搜索
        # （公告/新闻/研报）中单条记录的身份标识。同一实体因此可以合法持有多条
        # 取值不同的记录，而跨来源比对同一份记录时仍会归入同一组并检出冲突。
        grouped.setdefault((fact.entity, fact.field.casefold(), fact.period), []).append(fact)
    for records in grouped.values():
        if len({json.dumps(record.value, ensure_ascii=False, sort_keys=True, default=str) for record in records}) > 1:
            issues.append(
                CrossValidationIssue(
                    code="SOURCE_VALUE_CONFLICT",
                    severity="warning",
                    message=f"{entity_label(records[0].entity)}的{field_label(records[0].field)}存在不同来源数值不一致，已按较新资料处理。",
                    fact_ids=[record.fact_id for record in records],
                )
            )

    scored = [result for result in results if result.score is not None and result.confidence > 0]
    total_weight = sum(result.confidence for result in scored)
    consensus_score = (
        sum(float(result.score) * result.confidence for result in scored) / total_weight
        if total_weight
        else None
    )
    dissenting: list[str] = []
    supporting: list[str] = []
    if consensus_score is not None:
        for result in scored:
            if abs(float(result.score) - consensus_score) >= 25:
                dissenting.append(result.agent_id)
            else:
                supporting.append(result.agent_id)
    if len(scored) >= 3 and max(float(result.score) for result in scored) - min(float(result.score) for result in scored) >= 35:
        issues.append(
            CrossValidationIssue(
                code="AGENT_SCORE_DISPERSION",
                severity="warning",
                message="专业智能体评分分散度较高，协调器保留分歧并要求人工复核。",
                agent_ids=[result.agent_id for result in scored],
            )
        )
    for result in results:
        if result.status is TaskStatus.COMPLETED and not result.facts_used:
            issues.append(
                CrossValidationIssue(
                    code="COMPLETED_WITHOUT_EVIDENCE",
                    severity="critical",
                    message=f"{result.agent_id} 声称完成但没有通过核验的事实引用。",
                    agent_ids=[result.agent_id],
                )
            )
    status = ComplianceStatus.REVIEW if any(issue.severity in {"warning", "critical"} for issue in issues) else ComplianceStatus.PASS
    confidence = sum(result.confidence for result in scored) / len(scored) if scored else 0
    return CrossValidationResult(
        status=status,
        consensus_score=round(consensus_score, 2) if consensus_score is not None else None,
        confidence=round(confidence, 2),
        issues=issues,
        supporting_agents=supporting,
        dissenting_agents=dissenting,
    )


async def verify_facts(
    results: list[AgentResult],
    facts: list[FactRecord],
    *,
    now: datetime | None = None,
) -> list[AgentResult]:
    """最小事实核验器：剔除无来源或过期事实，并降低结果置信度。

    这是可运行的基础实现，并不替代生产级实体消歧、报表口径核验、指标重算和
    双源交叉验证。它先确保每一个 ``facts_used`` 都能在本次授权事实中找到，
    再按行情、新闻、评分、持仓与财务字段应用不同的时效阈值。
    """
    # 先建立存在性集合，以 O(1) 复杂度发现智能体引用了不存在的事实 ID。
    known_fact_ids = {fact.fact_id for fact in facts}
    # 全部使用带时区的 UTC，避免本地时区与数据源时区混用造成错误过期判断。
    current_time = now or datetime.now(timezone.utc)
    # 只有来源明确且未过期的事实才可继续支撑最终结论。
    valid_fact_ids = {
        fact.fact_id
        for fact in facts
        # 未来时间超过 5 分钟也视为异常，防止错误时钟让陈旧数据永久有效。
        if current_time - timedelta(seconds=fact_max_age_seconds(fact)) <= fact.snapshot_time <= current_time + timedelta(minutes=5)
        and fact.quality >= 0.4
        and fact.source_id.strip()
    }
    verified: list[AgentResult] = []
    for result in results:
        # invalid 表示根本无来源，stale 表示来源存在但已超过本版本允许的年龄。
        invalid = set(result.facts_used) - known_fact_ids
        stale = set(result.facts_used) - valid_fact_ids
        if invalid or stale:
            # 深拷贝避免原始专业结果被就地修改，便于审计时保留原始响应。
            result = result.model_copy(deep=True)
            # 此结果仍可被展示，但必须标为降级并设置置信度上限。
            result.status = TaskStatus.DEGRADED
            result.confidence = min(result.confidence, 0.4)
            result.confidence_reasons.append("存在无来源、低质量或过期事实")
            result.risk_flags.append("证据质量不足")
            # 删除不合格引用，确保 AdvicePackage.evidence 中不会含无效事实 ID。
            result.facts_used = [fact_id for fact_id in result.facts_used if fact_id in valid_fact_ids]
        verified.append(result)
    return verified


async def basic_compliance_check(
    request: OrchestrationRequest, results: list[AgentResult]
) -> ComplianceResult:
    """确定性的数值、适当性与证据闸门；文本语义由 SemanticService 审核。

    这里只判断已结构化的目标、回撤、持仓上限和证据完整性。协调器合并语义审核结果，
    并确保 BLOCK 优先于 REVIEW、REVIEW 优先于 PASS。
    """
    disclosures = [RISK_NOTICE]
    # 目标收益与最大回撤明显不匹配时先教育和复核，不据此反推更高风险仓位。
    if (
        request.profile.expected_annual_return is not None
        and request.profile.max_drawdown is not None
        and request.profile.expected_annual_return >= 0.15
        and request.profile.max_drawdown <= 0.08
    ):
        return ComplianceResult(
            status=ComplianceStatus.REVIEW,
            matched_rules=["RETURN_DRAWDOWN_MISMATCH"],
            reason="期望收益与最大可接受回撤存在明显张力，请先重新确认目标和风险边界。",
            risk_notice=RISK_NOTICE,
            required_disclosures=disclosures,
        )
    # 规则 5：组合单标的权重不得超过已确认的硬上限。MVP 返回 REVIEW，
    # 让结果压缩到区间并解释风险；绝不把它升级成自动调仓。
    portfolio_weights = [
        holding.get("weight")
        for holding in request.portfolio
        if isinstance(holding, dict) and isinstance(holding.get("weight"), (int, float))
    ]
    if portfolio_weights and max(portfolio_weights) > request.profile.single_security_limit:
        return ComplianceResult(
            status=ComplianceStatus.REVIEW,
            matched_rules=["SINGLE_SECURITY_CONCENTRATION"],
            reason="持仓存在超过已确认单标的上限的集中度风险。",
            risk_notice=RISK_NOTICE,
            required_disclosures=disclosures,
        )
    # 规则 6：具体价格/估值等判断没有通过事实层时不可放行。该检查也覆盖
    # 事实过期、质量过低或 source_id 为空导致 verifier 清空证据的情形。
    if not any(result.facts_used for result in results):
        # 证据不足不是“通过但置信度低”，而是 REVIEW：只能提供教育性内容或追问。
        return ComplianceResult(
            status=ComplianceStatus.REVIEW,
            matched_rules=["EVIDENCE_INSUFFICIENT"],
            reason="证据不足，仅可展示教育性说明。",
            risk_notice=RISK_NOTICE,
            required_disclosures=disclosures,
        )
    # 所有已实现的硬规则均未命中时才允许通过；风险提示仍必须随结果一同返回。
    return ComplianceResult(
        status=ComplianceStatus.PASS,
        risk_notice=RISK_NOTICE,
        required_disclosures=disclosures,
    )


def semantic_compliance(rules: list[str], reason: str) -> ComplianceResult:
    """规则 ID 来自受限枚举，优先级由代码决定，模型不能自行放行。"""
    hard_blocks = {"PRIVACY_AND_PERMISSION", "NO_RETURN_PROMISE", "SUITABILITY_R1_HIGH_RISK"}
    status = (ComplianceStatus.BLOCK if hard_blocks.intersection(rules)
              else ComplianceStatus.REVIEW if rules else ComplianceStatus.PASS)
    return ComplianceResult(
        status=status, matched_rules=sorted(set(rules)), reason=reason,
        risk_notice=RISK_NOTICE, required_disclosures=[RISK_NOTICE], rule_version="semantic-1.0",
    )


def merge_compliance(first: ComplianceResult, second: ComplianceResult) -> ComplianceResult:
    """语义判定只能增加约束，不能覆盖数值或证据检查。"""
    severity = {ComplianceStatus.PASS: 0, ComplianceStatus.REVIEW: 1, ComplianceStatus.BLOCK: 2}
    strictest = max((first, second), key=lambda item: severity[item.status])
    return strictest.model_copy(update={
        "matched_rules": sorted(set(first.matched_rules + second.matched_rules)),
        "reason": "；".join(item.reason for item in (first, second)
                          if item.reason and item.status is not ComplianceStatus.PASS) or strictest.reason,
        "required_disclosures": list(dict.fromkeys(first.required_disclosures + second.required_disclosures)),
    })
