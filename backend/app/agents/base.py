"""专业智能体共用的安全基类与事实选择工具。

MVP 不接大模型，因此每个智能体都是可测试的 Python 规则函数。即使以后把
``run`` 内部替换成模型调用，也必须保留本文件的“只引用授权事实”边界。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from numbers import Real

from backend.app.models import AgentResult, FactRecord, OrchestrationRequest


class BaseAgent(ABC):
    """所有专业智能体的最小契约。

    ``agent_id`` 同时是 DAG 中的注册键和审计字段。抽象基类让新增智能体不能
    忘记返回 ``AgentResult``，并集中实现证据 ID 的授权校验。
    """

    agent_id: str

    @abstractmethod
    async def run(self, request: OrchestrationRequest) -> AgentResult:
        """基于请求中显式授权的事实生成结构化结论。"""

    @staticmethod
    def ensure_fact_only(fact_ids: Iterable[str], facts: Iterable[FactRecord]) -> None:
        """拒绝未出现在本次请求事实包中的引用，防止旁路数据源或模型幻觉。"""
        allowed = {fact.fact_id for fact in facts}
        unknown = set(fact_ids) - allowed
        if unknown:
            raise ValueError(f"{BaseAgent.__name__} 收到未授权事实: {sorted(unknown)}")


def numeric_fact_values(facts: Iterable[FactRecord]) -> list[tuple[FactRecord, float]]:
    """返回可计算的数值事实，排除 ``bool`` 以免 True 被错误视为 1 分。"""
    values: list[tuple[FactRecord, float]] = []
    for fact in facts:
        if isinstance(fact.value, Real) and not isinstance(fact.value, bool):
            values.append((fact, float(fact.value)))
    return values


def clamp_score(value: float) -> float:
    """将标准化分数安全限制到 UI/Schema 共用的 0-100 区间。"""
    return round(min(100.0, max(0.0, value)), 2)


def evidence_metadata(facts: Iterable[FactRecord]) -> tuple[list[str], list[str]]:
    """从事实统一导出引用 ID 和来源，避免各 Agent 手写不一致的引用字段。"""
    selected = list(facts)
    return [fact.fact_id for fact in selected], sorted({fact.source_id for fact in selected})
