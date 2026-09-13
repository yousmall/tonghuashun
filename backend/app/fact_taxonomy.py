"""事实字段分类与时效口径。

不依赖任何业务模块，供协调器（时效核验、冲突分组）与模型输入切片共用，
避免 coordinator 与 services 之间形成循环导入。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from backend.app.models import FactRecord

FAST_MARKET_FIELDS = frozenset({"close_price", "change", "volume", "turnover_rate", "volatility"})
NEWS_FIELDS = frozenset({"news", "announcement", "research_report", "news_summary",
                         "announcement_summary", "research_report_summary"})
SCORE_FIELDS = frozenset(
    {
        "growth_score",
        "inflation_score",
        "liquidity_score",
        "policy_score",
        "risk_appetite_score",
        "prosperity_score",
        "valuation_score",
        "capital_flow_score",
        "crowding_score",
        "fundamental_score",
        "technical_score",
        "event_score",
        "governance_score",
        "fund_score",
    }
)
SLOW_FINANCIAL_FIELDS = frozenset({"pe_ttm", "pb", "roe", "revenue_growth", "fee_rate", "tracking_error"})
PORTFOLIO_FIELDS = frozenset({"weight", "portfolio_weight", "sector_weight", "fund_risk_level"})

# 面向用户的中文指标名：任何展示给用户的文案都必须经它转换，不能回显内部字段编码。
FIELD_LABELS: dict[str, str] = {
    "close_price": "最新价",
    "change": "涨跌幅",
    "volume": "成交量",
    "turnover_rate": "换手率",
    "volatility": "波动率",
    "pe_ttm": "市盈率",
    "pb": "市净率",
    "roe": "净资产收益率",
    "revenue_growth": "营业收入增长率",
    "fee_rate": "费率",
    "tracking_error": "跟踪误差",
    "weight": "持仓权重",
    "portfolio_weight": "持仓权重",
    "sector_weight": "行业权重",
    "fund_risk_level": "基金风险等级",
    "fund_score": "基金综合评分",
    "liquidity_score": "市场流动性",
    "cpi": "居民消费价格指数",
    "ppi": "工业生产者价格指数",
    "pmi": "采购经理指数",
    "social_financing": "社会融资",
    "interest_rate": "利率",
    "exchange_rate": "汇率",
    "growth_score": "经济增长",
    "inflation_score": "通胀环境",
    "policy_score": "政策环境",
    "risk_appetite_score": "风险偏好",
    "prosperity_score": "行业景气度",
    "valuation_score": "估值水平",
    "capital_flow_score": "资金流向",
    "crowding_score": "交易拥挤度",
    "fundamental_score": "基本面评分",
    "technical_score": "技术面评分",
    "event_score": "事件影响评分",
    "governance_score": "公司治理评分",
    "news": "新闻",
    "announcement": "公告",
    "research_report": "研报",
    "news_summary": "新闻摘要",
    "announcement_summary": "公告摘要",
    "research_report_summary": "研报摘要",
    "publish_date": "发布时间",
    "rating": "机构评级",
    "target_price": "目标价",
    "earnings_forecast": "盈利预测",
    "industry": "所属行业",
    "event": "重要事件",
    "institution": "研究机构",
    "provider_response": "查询摘要",
    "company_name": "公司全称",
    "main_business": "主营业务",
    "listing_date": "上市日期",
}

# 供应商返回的识别/展示元数据：既不是业务事实，也会撑大模型输入。
METADATA_FIELD_NAMES = frozenset(
    {
        "fekey",
        "key",
        "id",
        "uid",
        "type",
        "source",
        "domain",
        "label",
        "unit",
        "timestamp",
        "data_version",
        "macro_id",
        "macro_name",
        "indexid",
        "index_id",
        "index",
        "index_name",
        "gps_type",
        "gpstype",
        "is_cache",
        "realpos",
        "page",
        "limit",
        "total",
        "took",
        "status",
        "channel",
        "seq",
        "para_index",
        "site_authority",
        "modify_time",
        "operation_type",
        "traceability_type",
        "data_source",
        "publish_source",
        "app_id",
        "expand_index",
        "abtest_info",
        "t_0",
        "a",
        "指标名称",
        "指标单位",
    }
)


def fact_max_age_seconds(fact: FactRecord) -> int:
    """按数据类型返回可接受最大年龄，避免用统一阈值处理行情和财报。"""

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


# 采信一条事实的最低质量分；低于它的事实既不进结论，也不作为"已有资料"复用。
MIN_FACT_QUALITY = 0.4
# 允许的时钟前瞻：超过该偏差的"未来"时间戳视为异常，不能因为错误时钟长期有效。
FUTURE_FACT_TOLERANCE = timedelta(minutes=5)


def fact_is_current(fact: FactRecord, now: datetime) -> bool:
    """事实是否仍在自身时效窗口内、来源可追溯且质量达标。

    这是核验器（``verify_facts``）与自动取数复用判据共用的唯一口径：只有会通过
    核验的事实才允许被当作"已有资料"跳过取数，否则会出现"省了取数、却让结论
    降级"的隐性损失。
    """

    if not fact.source_id.strip() or fact.quality < MIN_FACT_QUALITY:
        return False
    return (
        now - timedelta(seconds=fact_max_age_seconds(fact))
        <= fact.snapshot_time
        <= now + FUTURE_FACT_TOLERANCE
    )
