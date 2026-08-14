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
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from backend.app.models import (
    AdvicePackage,
    AgentResult,
    ComplianceResult,
    ComplianceStatus,
    FactRecord,
    Intent,
    OrchestrationRequest,
    TaskNode,
    TaskPlan,
    TaskStatus,
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
    ) -> None:
        # 注册表由应用启动时装配。仅计划并执行已注册的 agent_id，避免由用户输入
        # 间接调用任意代码或不存在的“智能体”。
        self.agents = agents
        # 将核验与合规以依赖注入方式传入，便于替换为规则库、人工审核或服务调用。
        self.verifier = verifier
        self.compliance_checker = compliance_checker
        # 注入时间函数方便单元测试固定时钟；当前最小核验器仍使用 UTC 当前时间。
        self.now = now or (lambda: datetime.now(timezone.utc))

    def understand_intent(self, query: str) -> Intent:
        """第一版采用可测试的规则识别；后续可替换为受限 Schema 的 LLM。"""
        # 对英文 ETF 等关键词做统一小写化；中文文本不受 lower() 影响。
        normalized = query.lower()
        # 规则顺序即优先级：组合诊断通常也会包含“股票/基金”，故需最先识别。
        # 生产版可以替换为 JSON Schema 受约束的 LLM 分类器，但必须保留 UNKNOWN。
        rules = (
            (Intent.PORTFOLIO_REVIEW, ("组合", "持仓", "仓位", "调仓", "回撤")),
            (Intent.FUND_SCREENING, ("基金", "etf", "指数基金")),
            (Intent.SECURITY_RESEARCH, ("股票", "个股", "证券", "公司")),
            (Intent.MARKET_ANALYSIS, ("市场", "宏观", "行业", "行情")),
        )
        for intent, keywords in rules:
            # 任意一个关键词命中即可进入对应工作流；不猜测未命中的复杂意图。
            if any(keyword in normalized for keyword in keywords):
                return intent
        # 不确定意图时返回 UNKNOWN，由 plan() 生成澄清问题，而不是继续取数分析。
        return Intent.UNKNOWN

    def plan(self, request: OrchestrationRequest, trace_id: str) -> TaskPlan:
        """为已确认请求生成最小必要的 Task DAG。

        规划本身不调用数据服务或模型，因此速度快、易测试。它只描述“要做什么”，
        具体“怎么执行”由 :meth:`run` 根据节点关系实施。
        """
        intent = self.understand_intent(request.query)
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
                timeout_seconds=8,
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
                    timeout_seconds=5,
                    data_max_age_seconds=86_400,
                    priority=100,
                ),
            ]
        )
        return TaskPlan(trace_id=trace_id, intent=intent, nodes=nodes)

    async def run(self, request: OrchestrationRequest) -> AdvicePackage:
        """按安全顺序执行计划，并始终返回可审计的 AdvicePackage。

        函数没有抛出某个专业节点的异常给前端：节点异常会被转换为失败结果，其他
        节点仍可完成。只有合规 BLOCK 才会阻断最终建议；此时返回的是拦截说明，
        而不是把已生成的分析文字悄悄混入结论。
        """
        # UUID 只截取 12 位是为了展示友好；真实审计数据库可保存完整 UUID。
        trace_id = f"T-{uuid4().hex[:12].upper()}"
        plan = self.plan(request, trace_id)
        if plan.clarification_question:
            # 追问不是异常，是刻意的安全业务结果，使用 REVIEW 状态返回给界面。
            return self._review_package(plan, plan.clarification_question)

        # 这里仅选择注册的专业节点。事实核验和合规在后面严格按依赖顺序单独运行。
        specialist_nodes = [node for node in plan.nodes if node.agent_id in self.agents]
        results = await self._run_parallel(request, specialist_nodes)
        # 核验器可移除不存在/过期的 fact_id，并同步降低结果置信度。
        results = await self.verifier(results, request.facts)
        # 合规器使用核验后的结果作最终判定，保证无来源结论无法被放行。
        compliance = await self.compliance_checker(request, results)

        if compliance.status is ComplianceStatus.BLOCK:
            # BLOCK 是硬拦截：不执行综合摘要，也不把专业观点组合成建议。
            return AdvicePackage(
                trace_id=trace_id,
                intent=plan.intent,
                conclusion="请求未通过合规或适当性审核，未生成投资建议。",
                confidence=0,
                risks=[compliance.reason or "触发合规拦截"],
                compliance=compliance,
                task_plan=plan,
                agent_results=results,
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
        conclusion = self._summarize(usable, compliance.status)
        return AdvicePackage(
            trace_id=trace_id,
            intent=plan.intent,
            snapshot_time=snapshot_time,
            conclusion=conclusion,
            confidence=round(confidence, 2),
            evidence=evidence,
            risks=risks,
            compliance=compliance,
            task_plan=plan,
            agent_results=results,
        )

    async def _run_parallel(
        self, request: OrchestrationRequest, nodes: list[TaskNode]
    ) -> list[AgentResult]:
        """运行没有前置依赖的专业节点，并将单点故障局部化。"""

        async def execute(node: TaskNode) -> AgentResult:
            # plan() 已确保 agent_id 存在于注册表；在此处读取不会暴露用户可控索引。
            handler = self.agents[node.agent_id]
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
            Intent.SECURITY_RESEARCH: ("market", "industry", "security"),
            Intent.FUND_SCREENING: ("market", "fund"),
            Intent.PORTFOLIO_REVIEW: ("market", "industry", "security", "fund", "portfolio"),
        }
        return mapping[intent]

    def _review_package(self, plan: TaskPlan, reason: str) -> AdvicePackage:
        """把必要追问包装为统一响应，供前端按 REVIEW 状态展示。"""
        return AdvicePackage(
            trace_id=plan.trace_id,
            intent=plan.intent,
            conclusion=reason,
            confidence=0,
            compliance=ComplianceResult(
                status=ComplianceStatus.REVIEW,
                reason=reason,
                risk_notice=RISK_NOTICE,
            ),
            task_plan=plan,
        )

    @staticmethod
    def _summarize(results: list[AgentResult], status: ComplianceStatus) -> str:
        """在不篡改专业分歧的前提下，生成最小可读的聚合结论。

        此处故意不做投票或“取平均意见”：基本面偏正面、技术面过热等冲突应作为
        不同观点保留给用户与合规器，而非伪造一个单一确定结论。
        """
        if not results:
            # 无可用结果时必须明确证据不足，而不是输出空的“建议”。
            return "证据不足，暂不输出投资建议。"
        # 每个观点始终带来源 agent_id，方便前端把结论追溯到具体专业能力。
        opinions = "；".join(f"{result.agent_id}：{result.opinion}" for result in results)
        # REVIEW 的内容只能被界面标记为需人工复核/教育性说明。
        prefix = "需人工复核。" if status is ComplianceStatus.REVIEW else "分析结果："
        return f"{prefix}{opinions}"


async def verify_facts(
    results: list[AgentResult], facts: list[FactRecord]
) -> list[AgentResult]:
    """最小事实核验器：剔除无来源或过期事实，并降低结果置信度。

    这是可运行的基础实现，并不替代生产级实体消歧、报表口径核验、指标重算和
    双源交叉验证。它先确保每一个 ``facts_used`` 都能在本次授权事实中找到，
    再以 7 天作为演示期的通用时效阈值。
    """
    # 先建立存在性集合，以 O(1) 复杂度发现智能体引用了不存在的事实 ID。
    known_fact_ids = {fact.fact_id for fact in facts}
    # 全部使用带时区的 UTC，避免本地时区与数据源时区混用造成错误过期判断。
    now = datetime.now(timezone.utc)
    # 只有来源明确且未过期的事实才可继续支撑最终结论。
    valid_fact_ids = {
        fact.fact_id for fact in facts if fact.snapshot_time >= now - timedelta(days=7)
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
            result.confidence_reasons.append("存在无来源或过期事实")
            result.risk_flags.append("证据质量不足")
            # 删除不合格引用，确保 AdvicePackage.evidence 中不会含无效事实 ID。
            result.facts_used = [fact_id for fact_id in result.facts_used if fact_id in valid_fact_ids]
        verified.append(result)
    return verified


async def basic_compliance_check(
    request: OrchestrationRequest, results: list[AgentResult]
) -> ComplianceResult:
    """规则优先的第一版合规闸门；后续规则库可替换本函数。

    规则按“硬拦截优先于可展示内容”的顺序执行。它示范了收益承诺、适当性和
    证据完整性三类最小控制；生产环境应替换为版本化、可配置、可回放的规则库。
    """
    text = request.query.lower()
    if re.search(r"稳赚|必涨|保本高收益", text):
        # 不将此类请求改写成具体标的建议，直接阻断确定性收益承诺。
        return ComplianceResult(
            status=ComplianceStatus.BLOCK,
            matched_rules=["NO_RETURN_PROMISE"],
            reason="不得提供收益承诺或保证性表述。",
            risk_notice=RISK_NOTICE,
        )
    if request.profile.risk_level == "R1" and re.search(r"满仓|全仓", text):
        # 此规则仅演示低风险画像与高暴露表达的冲突；真实版本还要判断标的波动性。
        return ComplianceResult(
            status=ComplianceStatus.BLOCK,
            matched_rules=["SUITABILITY_R1_HIGH_RISK"],
            reason="低风险用户不能获得满仓高风险标的建议。",
            risk_notice=RISK_NOTICE,
        )
    if not any(result.facts_used for result in results):
        # 证据不足不是“通过但置信度低”，而是 REVIEW：只能提供教育性内容或追问。
        return ComplianceResult(
            status=ComplianceStatus.REVIEW,
            matched_rules=["EVIDENCE_INSUFFICIENT"],
            reason="证据不足，仅可展示教育性说明。",
            risk_notice=RISK_NOTICE,
        )
    # 所有已实现的硬规则均未命中时才允许通过；风险提示仍必须随结果一同返回。
    return ComplianceResult(status=ComplianceStatus.PASS, risk_notice=RISK_NOTICE)
