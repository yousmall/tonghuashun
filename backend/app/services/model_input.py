"""给大模型的事实切片。

真实数据源一次请求可能返回上千条事实（含供应商元数据与大量同名字段），直接
全量塞进提示词会超过输入预算，导致所有专业节点静默回退规则引擎。本模块在
**不改变审计用事实包** 的前提下，为模型挑选"与本次意图最相关、每条字段只留
最有代表性取值"的子集。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from backend.app.fact_taxonomy import (
    FAST_MARKET_FIELDS,
    NEWS_FIELDS,
    PORTFOLIO_FIELDS,
    SCORE_FIELDS,
    SLOW_FINANCIAL_FIELDS,
)
from backend.app.models import FactRecord, Intent

# 单次模型调用的事实条数上限。经验值：约 100 条结构化事实的 JSON 长度在
# 2-4 万字符之间，可稳定落在 6 万字符预算内。
MAX_MODEL_FACTS = 110

# 同一字段允许多条并存（新闻、公告、研报天然一天多条）；其余字段只留最新一条。
MULTI_RECORD_FIELDS = frozenset(
    {"news", "announcement", "research_report", "news_summary", "announcement_summary", "research_report_summary", "event"}
)
MULTI_RECORD_LIMIT = 6

# 各意图下优先送给模型的字段：越靠前越先保留。
_COMMON_SCORES = (
    "growth_score",
    "inflation_score",
    "liquidity_score",
    "policy_score",
    "risk_appetite_score",
    "prosperity_score",
    "valuation_score",
    "capital_flow_score",
    "crowding_score",
)
INTENT_FIELD_PRIORITY: dict[Intent, tuple[str, ...]] = {
    Intent.MARKET_ANALYSIS: (
        "cpi", "ppi", "pmi", "interest_rate", "social_financing",
        *_COMMON_SCORES,
        "industry", "news", "publish_date",
    ),
    Intent.INDUSTRY_ANALYSIS: (
        "industry", "prosperity_score", "valuation_score", "capital_flow_score",
        "crowding_score", "policy_score", "change", "news",
    ),
    Intent.SECURITY_RESEARCH: (
        "close_price", "change", "volume", "turnover_rate",
        "pe_ttm", "pb", "roe", "revenue_growth", "fundamental_score", "technical_score",
        "target_price", "rating", "earnings_forecast", "event",
        "industry", "news", "announcement", "research_report",
        "prosperity_score", "valuation_score", "policy_score",
    ),
    Intent.FUND_SCREENING: (
        "fee_rate", "tracking_error", "fund_score", "fund_risk_level", "liquidity_score",
        "close_price", "change", "weight",
    ),
    Intent.CONVERTIBLE_BOND_ANALYSIS: (
        "close_price", "change", "conversion_premium_rate", "pure_bond_premium_rate",
        "yield_to_maturity", "remaining_size", "bond_rating", "conversion_price",
        "pe_ttm", "pb", "roe", "news",
    ),
    Intent.PORTFOLIO_REVIEW: (
        "weight", "portfolio_weight",
        "close_price", "change", "pe_ttm", "pb", "roe", "revenue_growth",
        "fundamental_score", "technical_score", "valuation_score",
        *_COMMON_SCORES,
        "industry", "news",
    ),
    Intent.EDUCATION: (),
    Intent.UNKNOWN: (),
}


def slice_facts_for_model(
    facts: list[FactRecord],
    intent: Intent,
    *,
    limit: int = MAX_MODEL_FACTS,
    now: datetime | None = None,
) -> tuple[list[FactRecord], dict[str, Any]]:
    """返回（送入模型的事实子集, 切片说明）。子集仍是 FactRecord，可直接参与核验。"""

    current = now or datetime.now(timezone.utc)
    priority = INTENT_FIELD_PRIORITY.get(intent, ())
    priority_index = {field: rank for rank, field in enumerate(priority)}

    grouped: dict[tuple[str, str], list[FactRecord]] = defaultdict(list)
    for fact in facts:
        grouped[(fact.entity, fact.field.casefold())].append(fact)

    selected: list[FactRecord] = []
    for (_, field), records in grouped.items():
        if field in MULTI_RECORD_FIELDS:
            records = sorted(records, key=lambda item: item.snapshot_time, reverse=True)[:MULTI_RECORD_LIMIT]
        else:
            records = [max(records, key=lambda item: (item.snapshot_time, item.quality, item.quality))]
        selected.extend(records)

    def rank(fact: FactRecord) -> tuple[int, int, float, float]:
        field = fact.field.casefold()
        # 1) 本意图关心的业务字段优先；2) 派生评分与行情次之；3) 其余最后。
        if field in priority_index:
            tier = priority_index[field]
        elif field in SCORE_FIELDS or field in FAST_MARKET_FIELDS:
            tier = len(priority_index) + 1
        elif field in SLOW_FINANCIAL_FIELDS or field in PORTFOLIO_FIELDS:
            tier = len(priority_index) + 2
        elif field in NEWS_FIELDS:
            tier = len(priority_index) + 3
        else:
            tier = len(priority_index) + 4
        freshness = (fact.snapshot_time - current).total_seconds()
        return (tier, 0 if fact.quality >= 0.6 else 1, freshness, -fact.quality)

    selected.sort(key=rank)
    kept = selected[:limit]
    dropped = len(selected) - len(kept)
    summary = {
        "available": len(facts),
        "selected": len(kept),
        "dropped": dropped,
        "truncated": dropped > 0,
    }
    return kept, summary
