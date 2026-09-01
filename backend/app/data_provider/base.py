"""DataProvider 抽象。

专业智能体只能消费 ``FactRecord``，而不是绑定外部 SkillHub 返回的原始字段。
这样真实接口、测试夹具和最近有效快照可无缝互换，且所有数字都保留来源与时点。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from backend.app.models import FactRecord


class DataProvider(Protocol):
    """开发手册定义的最小异步数据边界。"""

    async def get_quote(self, symbol: str) -> list[FactRecord]: ...
    async def get_financial_metrics(self, symbol: str) -> list[FactRecord]: ...
    async def get_news(self, query: str) -> list[FactRecord]: ...
    async def get_fund_candidates(self, filters: dict[str, object]) -> list[FactRecord]: ...
    async def get_industry_rank(self, window: str) -> list[FactRecord]: ...


class SnapshotProvider:
    """只读内存快照 Provider，供 MVP、单元测试和外部依赖故障降级使用。"""

    def __init__(self, facts: Sequence[FactRecord]) -> None:
        # 深拷贝隔离调用方后续修改，确保一次 Provider 实例始终代表同一审计快照。
        self._facts = [fact.model_copy(deep=True) for fact in facts]

    def _find(self, *, entity: str | None = None, fields: set[str] | None = None) -> list[FactRecord]:
        """集中处理筛选；返回深拷贝，调用方无法修改 Provider 的内部快照。"""
        normalized_fields = {field.casefold() for field in fields} if fields is not None else None
        return [
            fact.model_copy(deep=True)
            for fact in self._facts
            if (entity is None or fact.entity == entity)
            and (normalized_fields is None or fact.field.casefold() in normalized_fields)
        ]

    async def get_quote(self, symbol: str) -> list[FactRecord]:
        return self._find(entity=symbol, fields={"close_price", "change", "volume", "volatility"})

    async def get_financial_metrics(self, symbol: str) -> list[FactRecord]:
        return self._find(entity=symbol, fields={"pe_ttm", "pb", "revenue_growth", "roe", "fundamental_score"})

    async def get_news(self, query: str) -> list[FactRecord]:
        # MVP 不做语义检索；只保留实体文本匹配，避免暗中引入未标注来源的搜索结果。
        normalized_query = query.casefold()
        return [
            fact.model_copy(deep=True)
            for fact in self._facts
            if normalized_query in fact.entity.casefold() and fact.field.casefold() == "news"
        ]

    async def get_fund_candidates(self, filters: dict[str, object]) -> list[FactRecord]:
        del filters  # 筛选规则由 FundAgent 执行；Provider 只负责提供规范化快照。
        return self._find(fields={"fund_risk_level", "fee_rate", "tracking_error", "fund_score", "liquidity_score"})

    async def get_industry_rank(self, window: str) -> list[FactRecord]:
        del window  # 快照的时点由 FactRecord.snapshot_time 表达，不伪造滚动窗口。
        return self._find(fields={"prosperity_score", "valuation_score", "capital_flow_score", "policy_score", "crowding_score"})
