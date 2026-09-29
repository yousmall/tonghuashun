"""管理员统计使用的固定领域与咨询主题口径。"""

DOMAIN_LABELS = {
    "security_research": "股票",
    "fund_screening": "基金",
    "industry_analysis": "行业",
    "convertible_bond_analysis": "可转债",
    "market_analysis": "宏观与市场",
    "portfolio_review": "组合配置",
    "education": "投资知识",
    "unknown": "其他 / 未分类",
}


def consultation_metadata(intent: str, target: str | None, query: str) -> dict[str, str]:
    """复用本轮已识别的意图和对象；没有对象时按原问题主题统计。"""
    domain = str(intent) if str(intent) in DOMAIN_LABELS else "unknown"
    topic = " ".join((target or query).split()).casefold()[:200]
    return {"domain": domain, "topic": topic or "未命名主题"}
