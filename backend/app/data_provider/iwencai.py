"""同花顺问财 SkillHub/OpenAPI 的只读数据适配器。

密钥仅从 ``IWENCAI_API_KEY`` 环境变量读取。适配器负责鉴权、限流、重试、
熔断和 FactRecord 标准化，不在日志或响应中暴露密钥。
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import httpx

from backend.app.models import FactRecord


FIELD_ALIASES = {
    "最新价": "close_price",
    "收盘价": "close_price",
    "涨跌幅": "change",
    "成交量": "volume",
    "换手率": "turnover_rate",
    "市盈率": "pe_ttm",
    "市盈率ttm": "pe_ttm",
    "市净率": "pb",
    "净资产收益率": "roe",
    "roe": "roe",
    "营业收入同比增长率": "revenue_growth",
    "基本面评分": "fundamental_score",
    "估值评分": "valuation_score",
    "技术面评分": "technical_score",
    "事件评分": "event_score",
    "治理评分": "governance_score",
    "经济增长评分": "growth_score",
    "通胀评分": "inflation_score",
    "流动性评分": "liquidity_score",
    "政策评分": "policy_score",
    "风险偏好评分": "risk_appetite_score",
    "景气度评分": "prosperity_score",
    "资金流向评分": "capital_flow_score",
    "拥挤度评分": "crowding_score",
    "基金评分": "fund_score",
    "基金风险等级": "fund_risk_level",
    "风险等级": "fund_risk_level",
    "管理费率": "fee_rate",
    "跟踪误差": "tracking_error",
    "居民消费价格指数": "cpi",
    "工业生产者出厂价格指数": "ppi",
    "采购经理指数": "pmi",
    "社会融资": "social_financing",
    "利率": "interest_rate",
    "新闻标题": "news",
    "公告标题": "announcement",
    "研报标题": "research_report",
    "标题": "news",
    "公司全称": "company_name",
    "所属行业": "industry",
    "上市日期": "listing_date",
    "主营业务": "main_business",
    "主营构成": "revenue_composition",
    "主要客户": "major_customer",
    "主要供应商": "major_supplier",
    "重大合同": "major_contract",
    "控股股东": "controlling_shareholder",
    "实际控制人": "actual_controller",
    "总股本": "total_shares",
    "流通股本": "float_shares",
    "股东人数": "shareholder_count",
    "事件标题": "event",
    "机构名称": "institution",
    "机构评级": "rating",
    "目标价": "target_price",
    "盈利预测": "earnings_forecast",
    "转股溢价率": "conversion_premium_rate",
    "纯债溢价率": "pure_bond_premium_rate",
    "到期收益率": "yield_to_maturity",
    "剩余规模": "remaining_size",
    "债券评级": "bond_rating",
    "转股价": "conversion_price",
}


class IwencaiSkillHubProvider:
    """把问财自然语言查询结果转换为带来源和时点的 FactRecord。"""

    source_id = "IWENCAI_SKILLHUB"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://openapi.iwencai.com",
        timeout_seconds: float = 8.0,
        max_retries: int = 2,
        max_concurrency: int = 8,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("IWENCAI_API_KEY 不能为空")
        self._api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.transport = transport
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._failure_count = 0
        self._circuit_open_until: datetime | None = None

    @classmethod
    def from_env(cls) -> "IwencaiSkillHubProvider | None":
        api_key = os.getenv("IWENCAI_API_KEY", "").strip()
        if not api_key:
            return None
        return cls(
            api_key,
            base_url=os.getenv("IWENCAI_BASE_URL", "https://openapi.iwencai.com"),
        )

    async def query(self, query: str, *, entity_hint: str | None = None, limit: int = 30) -> list[FactRecord]:
        if self._circuit_open_until and datetime.now(timezone.utc) < self._circuit_open_until:
            raise RuntimeError("问财数据源熔断中，请稍后重试")
        payload = {"query": query, "source": "wence_advisor", "page": "1", "limit": str(limit), "is_cache": "0"}
        headers = {"Authorization": f"Bearer {self._api_key}"}
        last_error: Exception | None = None
        async with self._semaphore:
            for attempt in range(self.max_retries + 1):
                try:
                    async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout_seconds) as client:
                        response = await client.post(f"{self.base_url}/v1/query2data", headers=headers, json=payload)
                    response.raise_for_status()
                    self._failure_count = 0
                    return self._normalize(response.json(), entity_hint=entity_hint or query)
                except (httpx.HTTPError, ValueError, TypeError) as exc:
                    last_error = exc
                    if attempt < self.max_retries:
                        await asyncio.sleep(0.15 * (2**attempt))
        self._failure_count += 1
        if self._failure_count >= 3:
            self._circuit_open_until = datetime.now(timezone.utc) + timedelta(seconds=30)
        raise RuntimeError("问财数据查询失败") from last_error

    async def get_quote(self, symbol: str) -> list[FactRecord]:
        return await self.query(f"{symbol} 最新价、涨跌幅、成交量、换手率", entity_hint=symbol)

    async def get_financial_metrics(self, symbol: str) -> list[FactRecord]:
        return await self.query(f"{symbol} 最新财报的市盈率、市净率、ROE、营业收入同比增长率", entity_hint=symbol)

    async def get_news(self, query: str) -> list[FactRecord]:
        return await self.query(f"{query} 最新财经新闻", entity_hint=query)

    async def get_fund_candidates(self, filters: dict[str, object]) -> list[FactRecord]:
        filter_text = " ".join(f"{key}={value}" for key, value in sorted(filters.items()))
        return await self.query(f"筛选基金和ETF {filter_text}".strip(), entity_hint="基金ETF")

    async def get_industry_rank(self, window: str) -> list[FactRecord]:
        return await self.query(f"{window} 行业涨跌、估值、资金流向和景气度排名", entity_hint="行业排名")

    async def get_convertible_bond(self, target: str) -> list[FactRecord]:
        return await self.query(
            f"{target} 可转债最新价、涨跌幅、转股溢价率、纯债溢价率、到期收益率、剩余规模、债券评级和转股价",
            entity_hint=target,
        )

    async def get_basic_info(self, target: str) -> list[FactRecord]:
        return await self.query(f"{target} 基本资料、所属行业、上市日期和主营业务", entity_hint=target)

    async def get_company_operations(self, target: str) -> list[FactRecord]:
        return await self.query(
            f"{target} 主营构成、主要客户、主要供应商、参控股公司和重大合同",
            entity_hint=target,
        )

    async def get_shareholder_equity(self, target: str) -> list[FactRecord]:
        return await self.query(
            f"{target} 控股股东、实际控制人、总股本、流通股本、股东人数和机构持股",
            entity_hint=target,
        )

    async def get_event_data(self, target: str) -> list[FactRecord]:
        return await self.query(
            f"{target} 最新业绩预告、增减持、股权质押、限售解禁、机构调研、监管函和重大事件",
            entity_hint=target,
        )

    async def get_macro_data(self, query: str) -> list[FactRecord]:
        return await self.query(f"{query} 宏观数据 CPI、PPI、PMI、利率、汇率和社会融资", entity_hint="宏观数据")

    async def get_institutional_research(self, target: str) -> list[FactRecord]:
        return await self.query(f"{target} 最新机构研究、评级、目标价和盈利预测", entity_hint=target)

    async def get_research_reports(self, target: str) -> list[FactRecord]:
        return await self.query(f"{target} 最新券商研报", entity_hint=target)

    async def get_announcements(self, target: str) -> list[FactRecord]:
        return await self.query(f"{target} 最新上市公司公告", entity_hint=target)

    async def screen_stocks(self, query: str) -> list[FactRecord]:
        return await self.query(f"A股筛选：{query}", entity_hint="A股筛选")

    async def screen_sectors(self, query: str) -> list[FactRecord]:
        return await self.query(f"板块筛选：{query}", entity_hint="板块筛选")

    def _normalize(self, payload: Any, *, entity_hint: str) -> list[FactRecord]:
        records = list(_find_record_lists(payload))
        snapshot_time = datetime.now(timezone.utc)
        facts: list[FactRecord] = []
        for record in records[:100]:
            entity = _entity_from_record(record, entity_hint)
            period = _period_from_record(record)
            for raw_field, value in record.items():
                if raw_field in {"代码", "证券代码", "股票代码", "名称", "证券简称", "报告期", "日期", "时间"}:
                    continue
                if isinstance(value, (dict, list)) or value is None:
                    continue
                field = _canonical_field(str(raw_field))
                facts.append(
                    FactRecord(
                        fact_id=f"IW-{uuid4().hex[:16].upper()}",
                        entity=entity,
                        field=field,
                        value=value,
                        period=period,
                        snapshot_time=snapshot_time,
                        source_id=self.source_id,
                        quality=0.90,
                    )
                )
        if facts:
            return facts
        # 即使供应商返回非表格答案，也保留为不可计算但可追溯的文本证据。
        summary = _first_scalar(payload)
        if summary is None:
            return []
        return [
            FactRecord(
                fact_id=f"IW-{uuid4().hex[:16].upper()}",
                entity=entity_hint,
                field="provider_response",
                value=str(summary),
                snapshot_time=snapshot_time,
                source_id=self.source_id,
                quality=0.75,
            )
        ]


class CompositeProvider:
    """并行读取多个 DataProvider，并按实体/字段/报告期选择最新高质量事实。"""

    def __init__(self, providers: Iterable[Any]) -> None:
        self.providers = list(providers)

    async def _merge(self, method: str, *args: Any) -> list[FactRecord]:
        results = await asyncio.gather(
            *(getattr(provider, method)(*args) for provider in self.providers),
            return_exceptions=True,
        )
        merged: dict[tuple[str, str, str | None], FactRecord] = {}
        for result in results:
            if isinstance(result, BaseException):
                continue
            for fact in result:
                key = (fact.entity, fact.field.casefold(), fact.period)
                current = merged.get(key)
                if current is None or (fact.quality, fact.snapshot_time) > (current.quality, current.snapshot_time):
                    merged[key] = fact
        return list(merged.values())

    async def get_quote(self, symbol: str) -> list[FactRecord]:
        return await self._merge("get_quote", symbol)

    async def get_financial_metrics(self, symbol: str) -> list[FactRecord]:
        return await self._merge("get_financial_metrics", symbol)

    async def get_news(self, query: str) -> list[FactRecord]:
        return await self._merge("get_news", query)

    async def get_fund_candidates(self, filters: dict[str, object]) -> list[FactRecord]:
        return await self._merge("get_fund_candidates", filters)

    async def get_industry_rank(self, window: str) -> list[FactRecord]:
        return await self._merge("get_industry_rank", window)

    async def get_convertible_bond(self, target: str) -> list[FactRecord]:
        return await self._merge("get_convertible_bond", target)

    async def get_basic_info(self, target: str) -> list[FactRecord]:
        return await self._merge("get_basic_info", target)

    async def get_company_operations(self, target: str) -> list[FactRecord]:
        return await self._merge("get_company_operations", target)

    async def get_shareholder_equity(self, target: str) -> list[FactRecord]:
        return await self._merge("get_shareholder_equity", target)

    async def get_event_data(self, target: str) -> list[FactRecord]:
        return await self._merge("get_event_data", target)

    async def get_macro_data(self, query: str) -> list[FactRecord]:
        return await self._merge("get_macro_data", query)

    async def get_institutional_research(self, target: str) -> list[FactRecord]:
        return await self._merge("get_institutional_research", target)

    async def get_research_reports(self, target: str) -> list[FactRecord]:
        return await self._merge("get_research_reports", target)

    async def get_announcements(self, target: str) -> list[FactRecord]:
        return await self._merge("get_announcements", target)

    async def screen_stocks(self, query: str) -> list[FactRecord]:
        return await self._merge("screen_stocks", query)

    async def screen_sectors(self, query: str) -> list[FactRecord]:
        return await self._merge("screen_sectors", query)


def _find_record_lists(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, list):
        if value and all(isinstance(item, dict) for item in value):
            yield from value
        else:
            for item in value:
                yield from _find_record_lists(item)
    elif isinstance(value, dict):
        for child in value.values():
            yield from _find_record_lists(child)


def _entity_from_record(record: dict[str, Any], fallback: str) -> str:
    for key in ("证券简称", "名称", "股票简称", "代码", "证券代码", "股票代码"):
        value = record.get(key)
        if value not in (None, ""):
            return str(value)
    return fallback


def _period_from_record(record: dict[str, Any]) -> str | None:
    for key in ("报告期", "日期", "时间"):
        value = record.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _canonical_field(raw: str) -> str:
    normalized = re.sub(r"\[[^]]*]", "", raw).strip()
    key = normalized.casefold().replace(" ", "")
    for alias, canonical in FIELD_ALIASES.items():
        if alias.casefold() in key:
            return canonical
    ascii_name = re.sub(r"[^a-zA-Z0-9_]+", "_", normalized).strip("_").lower()
    return ascii_name or normalized


def _first_scalar(value: Any) -> Any | None:
    if isinstance(value, dict):
        for child in value.values():
            found = _first_scalar(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_scalar(child)
            if found is not None:
                return found
    elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return value
    return None
