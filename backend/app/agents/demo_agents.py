"""用于验证主协调智能体的确定性专业智能体示例，不生成市场数据。

该文件不是生产级证券研究逻辑。它刻意只回显调用方授权的 ``FactRecord``，用于
验证协调器的并发、核验、聚合和合规链路；接入真实专业智能体时必须保留相同的
``AgentResult`` 输出契约。
"""

from __future__ import annotations

from backend.app.models import AgentResult, OrchestrationRequest, TaskStatus


def make_fact_based_agent(agent_id: str):
    """构造一个仅依据输入事实产生稳定结果的异步专业智能体。

    工厂函数让 market、industry、security、fund 和 portfolio 可以复用同一演示
    实现，同时在最终结果中保留各自的 ``agent_id``，便于测试并发和聚合行为。
    """

    async def analyze(request: OrchestrationRequest) -> AgentResult:
        # 演示实现最多读取两条事实，以避免它被误认为是完整的分析/优化算法。
        facts = request.facts[:2]
        if not facts:
            # 没有事实时应明确降级，绝不让模型或固定文本杜撰市场数字。
            return AgentResult(
                agent_id=agent_id,
                status=TaskStatus.DEGRADED,
                opinion="缺少授权事实，无法形成判断。",
                confidence=0,
                confidence_reasons=["无 FactRecord"],
                risk_flags=["证据不足"],
            )
        # 所有引用均来自 request.facts，因此可以被 verify_facts() 直接追溯。
        return AgentResult(
            agent_id=agent_id,
            status=TaskStatus.COMPLETED,
            opinion="已基于输入事实完成规则化分析。",
            # 固定分数仅用于 UI 和通路测试，不代表真实证券评分。
            score=60,
            confidence=0.7,
            facts_used=[fact.fact_id for fact in facts],
            # 引用来源与事实一起返回，方便后续接入证据中心。
            citations=[fact.source_id for fact in facts],
            risk_flags=["演示结果，需结合最新快照复核"],
            invalidation_conditions=["输入事实过期、撤回或被更高质量来源推翻"],
        )

    return analyze
