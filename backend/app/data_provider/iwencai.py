"""同花顺问财 SkillHub/OpenAPI 的只读数据适配器。

密钥仅从 ``IWENCAI_API_KEY`` 环境变量读取。适配器负责鉴权、限流、重试、
熔断和 FactRecord 标准化，不在日志或响应中暴露密钥。
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import secrets
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import httpx

from backend.app.fact_taxonomy import METADATA_FIELD_NAMES
from backend.app.models import FactRecord


# 实体名由 _entity_from_record 从这些字段提取并写入 FactRecord.entity，
# 因此它们本身不再重复展开成事实；日期类字段同理，避免与 publish_date 口径重叠。
# 问财财务查询实际返回的是 "股票简称"（而非 "证券简称"），两者都要跳过。
SKIPPED_RECORD_FIELDS: frozenset[str] = frozenset(
    {"代码", "证券代码", "股票代码", "名称", "证券简称", "股票简称", "报告期", "日期", "时间"}
)


# 别名表：值为内部规范化字段名。匹配时按别名长度倒序（见 _alias_index），
# 因此长别名优先命中，避免 "市盈率" 把 "静态市盈率" 一并吞掉这类子串误合并。
FIELD_ALIASES = {
    "最新价": "close_price",
    "收盘价": "close_price",
    "收盘价_前复权": "close_price",
    "涨跌幅": "change",
    "最新涨跌幅": "change",
    # "涨跌_前复权" 是涨跌额（元），不是涨跌幅（%）：同一行的这两个值必然不同，
    # 归到同一字段会让一次真实行情取数自己和自己冲突。前端展示口径也随之区分。
    "涨跌_前复权": "change_amount",
    "涨跌额": "change_amount",
    "成交量": "volume",
    "换手率": "turnover_rate",
    # 问财一行会同时返回 TTM / 静态 / 动态三种市盈率口径，它们是三个不同的
    # 指标，必须各自成字段；否则一次真实取数就会产生 3 条互相矛盾的 pe_ttm
    # 事实，被交叉核验判为"同项记录不一致"。（口径写在括号里的变体名由
    # _variant_field 处理。）
    "市盈率ttm": "pe_ttm",
    "市盈率": "pe_ttm",
    "静态市盈率": "pe_static",
    "动态市盈率": "pe_dynamic",
    "市净率": "pb",
    # ROE 同理：公布值里的加权口径与普通口径不是同一个数。
    "加权净资产收益率": "roe_weighted",
    "净资产收益率": "roe",
    "roe": "roe",
    "营业收入同比增长率": "revenue_growth",
    "营业收入": "revenue",
    "归母净利润": "net_profit",
    "归母净利润同比增长率": "net_profit_growth",
    "综合评分": "fundamental_score",
    "综合评分排名": "fundamental_rank",
    "所属同花顺行业": "industry",
    "资金流向": "capital_flow",
    "成交额": "turnover_value",
    "振幅": "amplitude",
    "开盘价_前复权": "open_price",
    "最高价_前复权": "high_price",
    "最低价_前复权": "low_price",
    "总市值": "market_cap",
    "流通市值": "float_market_cap",
    "换手率_前复权": "turnover_rate",
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


# 综合搜索返回的行级元数据。这些字段每行天然不同（各自的 PDF 链接、各自的相关度
# 得分），把它们收成事实会让同一实体的多条公告互相"冲突"，因此不进入事实层。
# 对应下游界面上的 index_name、key、id、url、score 等噪声字段。
ROW_METADATA_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "uid",
        "url",
        "source_url",
        "pdf_url",
        "report_url",
        "链接",
        "原文链接",
        "研报链接",
        "公告链接",
        "新闻链接",
        "score",
        "index",
        "index_name",
        "key",
        "name",
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
        "page",
        "limit",
        "is_cache",
        "expand_index",
        "abtest_info",
        "took",
        "total",
        "realpos",
        "page_num",
        "para_trace",
    }
)

# 辅助容器：其内容属于展示/溯源元数据，不是业务事实，递归时不再下钻。
# 保留 stock_infos 是因为其中的 名称/代码 是实体名回退来源，且这些键本身
# 属于跳过集合，不会产出行级噪声。
AUXILIARY_CONTAINERS: frozenset[str] = frozenset(
    {
        "trace_info",
        "ab",
        "header",
        "extra",
        "pageinfo",
        "layer_exp",
    }
)

# 综合搜索（公告/新闻/研报）的行级字段映射，按频道区分主体字段：同名的
# title/content 在三个频道里分别代表公告、新闻和研报，不能共用一张映射表。
SEARCH_CHANNEL_FIELDS: dict[str, dict[str, str]] = {
    "announcement": {
        "标题": "announcement",
        "公告标题": "announcement",
        "title": "announcement",
        "content": "announcement",
        "摘要": "announcement_summary",
        "summary": "announcement_summary",
        "发布时间": "publish_date",
        "publish_date": "publish_date",
        "发布日期": "publish_date",
    },
    "news": {
        "标题": "news",
        "新闻标题": "news",
        "title": "news",
        "content": "news",
        "摘要": "news_summary",
        "summary": "news_summary",
        "发布时间": "publish_date",
        "publish_date": "publish_date",
        "发布日期": "publish_date",
    },
    "report": {
        "标题": "research_report",
        "研报标题": "research_report",
        "title": "research_report",
        "content": "research_report",
        "摘要": "research_report_summary",
        "summary": "research_report_summary",
        "发布时间": "publish_date",
        "publish_date": "publish_date",
        "发布日期": "publish_date",
    },
}

# 无频道信息（逐条匹配）时使用的主体字段映射。此时标题按公告口径处理，与
# 现有 FIELD_ALIASES 的默认行为保持一致。
RECORD_FIELD_ALIASES: dict[str, str] = dict(SEARCH_CHANNEL_FIELDS["announcement"])


def _alias_index(table: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """把别名表排成"长别名优先"的匹配序列。

    别名是子串匹配（中文没有词边界），所以更具体的别名必须排在更泛的别名前面：
    "加权净资产收益率" 要赢过 "净资产收益率"，"静态市盈率" 要赢过 "市盈率"。
    否则一次真实取数会把不同口径的指标合并成同名字段，凭空制造同项冲突。
    """

    return tuple(sorted(table.items(), key=lambda item: len(item[0]), reverse=True))


# 预排序一次的匹配索引：命中顺序由别名具体程度决定，不再依赖字典书写顺序。
FIELD_ALIAS_INDEX: tuple[tuple[str, str], ...] = _alias_index(FIELD_ALIASES)
RECORD_FIELD_INDEX: tuple[tuple[str, str], ...] = _alias_index(RECORD_FIELD_ALIASES)
SEARCH_CHANNEL_INDEX: dict[str, tuple[tuple[str, str], ...]] = {
    channel: _alias_index(fields) for channel, fields in SEARCH_CHANNEL_FIELDS.items()
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
        # Provider 是应用级单例；复用一个异步客户端才能真正复用 TCP/TLS 连接池。
        # 每次调用仍生成独立追踪 ID，并保留原有超时、重试和熔断语义。
        self._client = httpx.AsyncClient(transport=transport, timeout=timeout_seconds)
        self._failure_count = 0
        self._circuit_open_until: datetime | None = None

    async def aclose(self) -> None:
        """在应用退出时释放问财连接池。"""

        await self._client.aclose()

    @classmethod
    def from_env(cls) -> "IwencaiSkillHubProvider | None":
        api_key = os.getenv("IWENCAI_API_KEY", "").strip()
        if not api_key:
            return None
        return cls(
            api_key,
            base_url=os.getenv("IWENCAI_BASE_URL", "https://openapi.iwencai.com"),
        )

    async def query(
        self,
        query: str,
        *,
        entity_hint: str | None = None,
        limit: int = 30,
        skill_id: str = "hithink-market-query",
        skill_version: str = "1.0.0",
    ) -> list[FactRecord]:
        payload = {
            "query": query,
            "page": "1",
            "limit": str(limit),
            "is_cache": "1",
            "expand_index": "true",
        }
        return await self._request(
            "/v1/query2data",
            payload,
            entity_hint=entity_hint or query,
            skill_id=skill_id,
            skill_version=skill_version,
        )

    async def _comprehensive_search(self, query: str, *, channel: str, entity_hint: str) -> list[FactRecord]:
        skills = {
            "announcement": ("announcement-search", "1.0.0"),
            "news": ("news-search", "1.0.0"),
            "report": ("report-search", "2.0.0"),
        }
        try:
            skill_id, skill_version = skills[channel]
        except KeyError as exc:
            raise ValueError(f"不支持的问财综合搜索频道：{channel}") from exc
        payload = {
            "channels": [channel],
            "app_id": "AIME_SKILL",
            "query": query,
        }
        return await self._request(
            "/v1/comprehensive/search",
            payload,
            entity_hint=entity_hint,
            skill_id=skill_id,
            skill_version=skill_version,
            channel=channel,
        )

    async def _request(
        self,
        path: str,
        payload: dict[str, object],
        *,
        entity_hint: str,
        skill_id: str,
        skill_version: str,
        channel: str | None = None,
        history_metric: str | None = None,
        history_limit: int = 30,
    ) -> list[FactRecord]:
        if self._circuit_open_until and datetime.now(timezone.utc) < self._circuit_open_until:
            raise RuntimeError("问财数据源熔断中，请稍后重试")
        last_error: Exception | None = None
        async with self._semaphore:
            for attempt in range(self.max_retries + 1):
                headers = {
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "X-Claw-Call-Type": "normal" if attempt == 0 else "retry",
                    "X-Claw-Skill-Id": skill_id,
                    "X-Claw-Skill-Version": skill_version,
                    "X-Claw-Plugin-Id": "none",
                    "X-Claw-Plugin-Version": "none",
                    "X-Claw-Trace-Id": secrets.token_hex(32),
                }
                try:
                    response = await self._client.post(
                        f"{self.base_url}{path}", headers=headers, json=payload
                    )
                    response.raise_for_status()
                    self._failure_count = 0
                    payload = response.json()
                    if history_metric:
                        return _history_facts(
                            payload, entity_hint=entity_hint, source_id=self.source_id,
                            metric=history_metric, limit=history_limit,
                        )
                    return self._normalize(payload, entity_hint=entity_hint, channel=channel)
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    status_code = exc.response.status_code
                    retryable = status_code in {408, 425, 429} or status_code >= 500
                    if retryable and attempt < self.max_retries:
                        await asyncio.sleep(0.15 * (2**attempt))
                        continue
                    break
                except (httpx.RequestError, ValueError, TypeError) as exc:
                    last_error = exc
                    if attempt < self.max_retries:
                        await asyncio.sleep(0.15 * (2**attempt))
        self._failure_count += 1
        if self._failure_count >= 3:
            self._circuit_open_until = datetime.now(timezone.utc) + timedelta(seconds=30)
        raise RuntimeError(self._query_error_message(last_error)) from last_error

    @staticmethod
    def _query_error_message(error: Exception | None) -> str:
        if isinstance(error, httpx.HTTPStatusError):
            status_code = error.response.status_code
            if status_code == 401:
                return "问财未接受当前密钥，请确认使用的是该 Skill 对应的 IWENCAI_API_KEY。"
            if status_code == 403:
                return "问财拒绝访问当前接口，请确认账号已开通对应数据能力或来源权限。"
            if status_code == 429:
                return "问财调用频率或额度受限，请稍后重试。"
            if status_code >= 500:
                return "问财上游服务暂时不可用，请稍后重试。"
            return "问财未接受本次请求，请检查查询条件和账号权限。"
        if isinstance(error, httpx.TimeoutException):
            return "连接问财服务超时，请检查网络或代理后重试。"
        if isinstance(error, httpx.RequestError):
            return "无法建立问财服务连接，请检查网络、DNS 或代理设置后重试。"
        if isinstance(error, (ValueError, TypeError)):
            return "问财返回的数据格式异常，请稍后重试。"
        return "问财数据查询失败，请稍后重试。"

    async def get_price_history(
        self, target: str, asset_type: str, *, limit: int = 30
    ) -> list[FactRecord]:
        """沿用已开通的只读查询 Skill，返回有真实日期的价格/净值事实。"""

        if asset_type not in {"股票", "基金", "可转债"} or limit not in {30, 60}:
            raise ValueError("历史走势仅支持股票、基金和可转债的近 30 或 60 期数据")
        metric = "fund_nav" if asset_type == "基金" else "close_price"
        label = "单位净值" if metric == "fund_nav" else "收盘价"
        query = f"{target}近{limit}个交易日每日{label}，逐日列出日期和{label}"
        return await self._request(
            "/v1/query2data",
            {"query": query, "page": "1", "limit": str(limit),
             "is_cache": "1", "expand_index": "true"},
            entity_hint=target,
            skill_id="hithink-fund-query" if metric == "fund_nav" else "hithink-market-query",
            skill_version="1.0.0",
            history_metric=metric,
            history_limit=limit,
        )

    async def get_quote(self, symbol: str) -> list[FactRecord]:
        return await self.query(f"{symbol} 最新价、涨跌幅、成交量、换手率", entity_hint=symbol)

    async def get_financial_metrics(self, symbol: str) -> list[FactRecord]:
        return await self.query(
            f"{symbol} 最新财报的市盈率、市净率、ROE、营业收入同比增长率",
            entity_hint=symbol,
            skill_id="hithink-finance-query",
        )

    async def get_news(self, query: str) -> list[FactRecord]:
        return await self._comprehensive_search(
            f"{query} 最新财经新闻",
            channel="news",
            entity_hint=query,
        )

    async def get_fund_candidates(self, filters: dict[str, object]) -> list[FactRecord]:
        filter_text = " ".join(f"{key}={value}" for key, value in sorted(filters.items()))
        return await self.query(
            f"筛选基金和ETF {filter_text}".strip(),
            entity_hint="基金ETF",
            skill_id="hithink-fund-query",
        )

    async def get_industry_rank(self, window: str) -> list[FactRecord]:
        # 行业节点需要景气度/资金流向/拥挤度等分项，原查询只要"涨跌和排名"，
        # 返回字段无法满足 IndustryAgent，故补齐维度。
        return await self.query(
            f"{window} 行业涨跌、估值、资金流向和景气度排名，"
            "以及景气度评分、估值评分、资金流向评分、拥挤度评分和政策评分",
            entity_hint="行业排名",
        )

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
        # 同时索取原始指标与评分维度：research 的派生规则会用 CPI/PPI/PMI 等原始值
        # 算出 growth_score 等分项，只取评分名会失去派生来源。
        return await self.query(
            f"{query} 宏观数据 CPI、PPI、PMI、利率、汇率、社会融资、"
            "经济增长评分、通胀评分、流动性评分、政策评分和风险偏好评分",
            entity_hint="宏观数据",
            skill_id="hithink-macro-query",
        )

    async def get_institutional_research(self, target: str) -> list[FactRecord]:
        return await self.query(f"{target} 最新机构研究、评级、目标价和盈利预测", entity_hint=target)

    async def get_research_reports(self, target: str) -> list[FactRecord]:
        return await self._comprehensive_search(
            f"{target} 最新券商研报",
            channel="report",
            entity_hint=target,
        )

    async def get_announcements(self, target: str) -> list[FactRecord]:
        return await self._comprehensive_search(
            f"{target} 最新上市公司公告",
            channel="announcement",
            entity_hint=target,
        )

    async def screen_stocks(self, query: str) -> list[FactRecord]:
        return await self.query(
            f"A股筛选：{query}",
            entity_hint="A股筛选",
            skill_id="hithink-astock-selector",
        )

    async def screen_sectors(self, query: str) -> list[FactRecord]:
        return await self.query(
            f"板块筛选：{query}",
            entity_hint="板块筛选",
            skill_id="hithink-astock-selector",
        )

    def _normalize(self, payload: Any, *, entity_hint: str, channel: str | None = None) -> list[FactRecord]:
        records = list(_find_record_lists(payload))
        snapshot_time = datetime.now(timezone.utc)
        # 宽表（宏观、行业等）把多个指标放在同一列名下，靠"指标名称"区分。若不先
        # 展开，CPI/PPI/PMI 等会全部落到同一个字段名上，派生评分与专业节点就再也
        # 找不到所需字段。
        expanded = _expand_indicator_rows(records)
        facts: list[FactRecord] = []
        for record in expanded[:100]:
            entity = _entity_from_record(record, entity_hint)
            # "一行即一条记录"的返回（综合搜索、机构调研/研报、事件等）整行共用
            # 一个逐条标识：同一份记录的多个字段共享 period，同一实体的多条记录
            # 彼此可区分，行级字段（研究员、研究机构、研报链接）不会互相判冲突。
            # 行情/财务/宏观等标量宽表取不到该标识，退回报告期口径，保持原有语义。
            record_scope = _record_scope(record)
            period = record_scope or _period_from_record(record)
            source_url = _source_url(record)
            for raw_field, value in record.items():
                if raw_field in SKIPPED_RECORD_FIELDS:
                    continue
                if isinstance(value, (dict, list)) or value is None:
                    continue
                raw_key = str(raw_field).strip().casefold()
                if raw_key in ROW_METADATA_FIELDS or raw_key in METADATA_FIELD_NAMES:
                    continue
                field = _canonical_field(str(raw_field), channel=channel)
                if field in ROW_METADATA_FIELDS or field in METADATA_FIELD_NAMES:
                    continue
                # 综合搜索的一行是一条记录：整行（标题、摘要、发布日期等）共用同一个
                # 逐条标识，使同一实体的多条公告各自成组；跨来源比对同一份记录仍会
                # 命中同一 period，真实冲突照旧检出。
                facts.append(
                    FactRecord(
                        fact_id=f"IW-{uuid4().hex[:16].upper()}",
                        entity=entity,
                        field=field,
                        value=value,
                        period=period,
                        snapshot_time=snapshot_time,
                        source_id=self.source_id,
                        source_url=source_url,
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


# 未映射到业务名称时使用的中文标签键。
INDICATOR_NAME_KEYS: tuple[str, ...] = ("指标名称", "指标", "项目", "科目", "名称")
# 宽表里数值列叫“宏观@值[20251231]”“指标值”等；用它识别哪些列是指标取值。
INDICATOR_VALUE_MARKERS: tuple[str, ...] = ("@值", "指标值", "数值")
# 一个指标最多保留几个历史取值，供模型做时间对比。
MAX_PERIODS_PER_INDICATOR = 3

# 这些键只是列定义元数据，不是数据行，递归时直接跳过。
COLUMN_DEFINITION_KEYS: frozenset[str] = frozenset({"columns", "column", "header", "headers"})


def _expand_indicator_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把"一行多指标"的宽表展开为"每行一个可识别指标"。

    问财的宏观/行业查询会把 CPI、PPI、PMI 等放在同一个数值列名下，靠"指标名称"
    区分。展开后字段名取自指标本身，派生评分与专业节点才能找到所需字段。
    """

    expanded: list[dict[str, Any]] = []
    for record in records:
        name = ""
        for key in INDICATOR_NAME_KEYS:
            candidate = record.get(key)
            if candidate not in (None, ""):
                name = str(candidate).strip()
                break
        if not name:
            expanded.append(record)
            continue
        indicator = _indicator_code(name)
        if not indicator:
            expanded.append(record)
            continue
        value_columns = [
            (key, value)
            for key, value in record.items()
            if any(marker in str(key) for marker in INDICATOR_VALUE_MARKERS)
            and not isinstance(value, (dict, list))
            and value not in (None, "")
        ]
        if not value_columns:
            expanded.append(record)
            continue
        # 日期后缀越大越新，只保留最近若干期，避免一次宏观查询塞进上百条事实。
        value_columns.sort(key=lambda item: _period_suffix(item[0]), reverse=True)
        for raw_key, value in value_columns[:MAX_PERIODS_PER_INDICATOR]:
            row = {
                "证券简称": name,
                "指标名称": name,
                indicator: value,
            }
            suffix = _period_suffix(raw_key)
            if suffix:
                row["报告期"] = suffix
            expanded.append(row)
    return expanded


def _variant_field(key: str) -> str:
    """按口径关键词消歧；无口径标记时返回空字符串。

    问财一行会同时返回 TTM / 静态 / 动态三种市盈率，财报里还有普通与加权两种
    ROE。子串别名只能命中其中一个口径，所以先用最具体的关键词判定口径，
    只有确实没有口径标记时才交给别名表落到默认字段。
    """

    if "市盈率" in key:
        if "静态" in key:
            return "pe_static"
        if "动态" in key:
            return "pe_dynamic"
        return "pe_ttm"
    if "净资产收益率" in key or "roe" in key:
        return "roe_weighted" if "加权" in key else "roe"
    return ""


def _indicator_code(text: str) -> str:
    """把中文指标名解析为内部字段名；解析不出时返回空字符串。"""

    stripped = re.sub(r"[\s（）()【】\[\]]", "", text)
    if not stripped:
        return ""
    # 宽表指标名会把口径写在括号里（"净资产收益率roe(加权,公布值)"），
    # 去掉括号后再看关键词，避免泛别名把加权口径并成普通 ROE。
    variant = _variant_field(stripped.casefold())
    if variant:
        return variant
    for alias, canonical in FIELD_ALIAS_INDEX:
        if alias.casefold() in stripped.casefold():
            return canonical
    return _canonical_field(stripped)


def _period_suffix(raw_key: Any) -> str:
    """从“宏观@值[20251231]”这类列名中取出报告期。"""

    match = re.search(r"\[(\d{6,8})\]", str(raw_key))
    return match.group(1) if match else ""


def _history_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    try:
        if re.fullmatch(r"\d{8}", text):
            return datetime.strptime(text, "%Y%m%d").date()
        if re.match(r"^\d{4}-\d{2}-\d{2}(?:$|[ T])", text):
            return date.fromisoformat(text[:10])
    except ValueError:
        pass
    return None


def _history_value(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _history_record_code(record: dict[str, Any]) -> str | None:
    for key in ("股票代码", "证券代码", "基金代码", "代码"):
        match = re.search(r"(?<!\d)(\d{6})(?!\d)", str(record.get(key) or ""))
        if match:
            return match.group(1)
    return None


def _history_metric_matches(raw_key: str, metric: str) -> bool:
    name = raw_key.strip()
    return name.startswith("单位净值") if metric == "fund_nav" else name.startswith("收盘价")


def _history_facts(
    payload: Any, *, entity_hint: str, source_id: str, metric: str, limit: int
) -> list[FactRecord]:
    """只接受明确标注日期的数值，不用抓取时间补齐缺失的交易日期。"""

    records = list(_find_record_lists(payload))[:100]
    target_match = re.search(r"(?<!\d)(\d{6})(?!\d)", entity_hint)
    target_code = target_match.group(1) if target_match else None
    codes = {_history_record_code(record) for record in records}
    codes.discard(None)
    if not target_code and len(codes) > 1:
        return []
    values: dict[date, tuple[float, str | None]] = {}
    conflicts: set[date] = set()
    today = datetime.now(timezone(timedelta(hours=8))).date()
    for record in records:
        record_code = _history_record_code(record)
        if target_code and record_code != target_code:
            continue
        latest_nav = _history_date(record.get("最新净值日期")) if metric == "fund_nav" else None
        source_url = _source_url(record)
        row_date = None
        for key in ("交易日期", "日期", "净值日期", "date"):
            row_date = _history_date(record.get(key))
            if row_date:
                break
        for raw_key, raw_value in record.items():
            key = str(raw_key)
            suffix = re.search(r"\[(\d{8})\]$", key)
            column_name = key[:suffix.start()] if suffix else key
            if not _history_metric_matches(column_name, metric):
                continue
            observed = _history_date(suffix.group(1)) if suffix else row_date
            amount = _history_value(raw_value)
            if not observed or not amount or observed > today or (latest_nav and observed > latest_nav):
                continue
            previous = values.get(observed)
            if previous and not math.isclose(previous[0], amount, rel_tol=1e-9):
                conflicts.add(observed)
            elif observed not in conflicts:
                values[observed] = (amount, source_url or (previous[1] if previous else None))
    captured_at = datetime.now(timezone.utc)
    return [
        FactRecord(
            fact_id=f"IW-{uuid4().hex[:16].upper()}", entity=entity_hint,
            field=metric, value=amount, period=observed.isoformat(),
            snapshot_time=captured_at, source_id=source_id, source_url=source_url,
            quality=0.90,
        )
        for observed, (amount, source_url) in [
            item for item in sorted(values.items()) if item[0] not in conflicts
        ][-limit:]
    ]


def _find_record_lists(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, list):
        if value and all(isinstance(item, dict) for item in value):
            yield from value
        else:
            for item in value:
                yield from _find_record_lists(item)
    elif isinstance(value, dict):
        for key, child in value.items():
            # 辅助容器装的是溯源/展示元数据，不是业务事实，不再下钻。
            if str(key).strip().casefold() in AUXILIARY_CONTAINERS:
                continue
            # 列定义（columns/header）描述字段类型与来源，不是数据行。
            if str(key).strip().casefold() in COLUMN_DEFINITION_KEYS:
                continue
            yield from _find_record_lists(child)


def _source_url(record: dict[str, Any]) -> str | None:
    """保留供应商记录中的公开原文地址；无效地址只丢弃链接，不丢弃事实。"""
    for key in ("url", "source_url", "pdf_url", "report_url", "链接", "原文链接", "研报链接", "公告链接", "新闻链接"):
        value = record.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 2048:
            continue
        try:
            return FactRecord.source_url_must_be_public_https(value)
        except ValueError:
            continue
    return None


def _record_scope(record: dict[str, Any]) -> str:
    """为"一行即一条记录"的返回生成逐条比对用的稳定标识。

    只用能区分"这一条记录"的字段：链接/ID 优先，其次是标题或正文（同一实体名下
    的多条公告、多份研报，标题与正文天然不同）。发布日期只说明时点、区分不了
    同一天的多个条目，因此绝不单独作为标识，否则同一天的两条新闻会被折叠成
    "同一项记录"而误报取值冲突，或被 CompositeProvider._merge 误当成同一条而
    丢掉一条。逐条记录的字段（研究员、研究机构、研报链接等）不再互相判冲突。

    行情/财务/宏观这类标量宽表没有这些字段，返回空字符串，仍按报告期口径处理。
    """

    for key in ("url", "source_url", "pdf_url", "report_url", "id", "uid", "链接", "原文链接", "研报链接", "公告链接", "新闻链接"):
        value = record.get(key)
        if value not in (None, ""):
            digest = hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:12]
            return f"REC-{digest}"
    # 标题（或正文）是逐条记录的判别字段；日期只作为同标题重复发布时的补充。
    discriminator = ""
    for key in ("title", "标题", "新闻标题", "公告标题", "研报标题", "研报", "事件标题",
                "summary", "摘要", "content", "内容", "正文"):
        value = record.get(key)
        if value not in (None, ""):
            discriminator = str(value)
            break
    if not discriminator:
        return ""
    date_part = ""
    for key in ("publish_date", "发布时间", "发布日期", "公告日期"):
        value = record.get(key)
        if value not in (None, ""):
            date_part = str(value)
            break
    digest = hashlib.sha1(f"{date_part}|{discriminator}".encode("utf-8")).hexdigest()[:12]
    return f"REC-{digest}"


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


def _canonical_field(raw: str, *, channel: str | None = None) -> str:
    normalized = re.sub(r"\[[^]]*]", "", raw).strip()
    key = normalized.casefold().replace(" ", "")
    if channel is not None:
        # 综合搜索按频道解析：同名的 title/content 在公告、新闻、研报中含义不同。
        for alias, canonical in SEARCH_CHANNEL_INDEX.get(channel, RECORD_FIELD_INDEX):
            if alias.casefold() in key:
                return canonical
    # 口径消歧优先于别名表：否则泛别名会吞掉"静态市盈率""加权净资产收益率"。
    variant = _variant_field(key)
    if variant:
        return variant
    # 长别名优先，保证"静态市盈率""加权净资产收益率"不会被泛别名吞掉。
    for alias, canonical in FIELD_ALIAS_INDEX:
        if alias.casefold() in key:
            return canonical
    ascii_name = re.sub(r"[^a-zA-Z0-9_]+", "_", normalized).strip("_").lower()
    # 去掉供应商附加的日期后缀，例如 宏观@值[20251231] -> 宏观_值。
    ascii_name = re.sub(r"_?\d{6,8}$", "", ascii_name)
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
