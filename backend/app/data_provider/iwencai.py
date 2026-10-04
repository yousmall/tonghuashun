"""同花顺问财 SkillHub/OpenAPI 的只读数据适配器。

密钥仅从 ``IWENCAI_API_KEY`` 环境变量读取。适配器负责鉴权、限流、重试、
熔断和 FactRecord 标准化，不在日志或响应中暴露密钥。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import secrets
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from time import perf_counter

from backend.app.services.call_tracking import calling_user_id
from backend.app.services.provider_errors import ProviderCallError

import httpx

from backend.app.fact_taxonomy import METADATA_FIELD_NAMES
from backend.app.models import FactRecord
from backend.app.fact_units import PERCENT_FIELDS

SKILL_KEY_ENV = {
    "hithink-market-query": "IWENCAI_MARKET_API_KEY",
    "hithink-finance-query": "IWENCAI_FINANCE_API_KEY",
    "hithink-macro-query": "IWENCAI_MACRO_API_KEY",
    "hithink-fund-query": "IWENCAI_FUND_API_KEY",
    "hithink-astock-selector": "IWENCAI_SELECTOR_API_KEY",
    "hithink-basicinfo-query": "IWENCAI_BASICINFO_API_KEY",
    "hithink-industry-query": "IWENCAI_INDUSTRY_API_KEY",
    "hithink-business-query": "IWENCAI_BUSINESS_API_KEY",
    "hithink-management-query": "IWENCAI_MANAGEMENT_API_KEY",
    "hithink-insresearch-query": "IWENCAI_INSRESEARCH_API_KEY",
    "hithink-sector-selector": "IWENCAI_SECTOR_SELECTOR_API_KEY",
    "hithink-event-query": "IWENCAI_EVENT_API_KEY",
    "announcement-search": "IWENCAI_ANNOUNCEMENT_API_KEY",
    "news-search": "IWENCAI_NEWS_API_KEY",
    "report-search": "IWENCAI_REPORT_API_KEY",
}

# Invalidate persistent facts after changing official skill routes or field contracts.
SKILL_CONTRACT_REVISION = "2026-10-04.3"


# 实体名由 _entity_from_record 从这些字段提取并写入 FactRecord.entity，
# 因此它们本身不再重复展开成事实；日期类字段同理，避免与 publish_date 口径重叠。
# 问财财务查询实际返回的是 "股票简称"（而非 "证券简称"），两者都要跳过。
SKIPPED_RECORD_FIELDS: frozenset[str] = frozenset(
    {"代码", "证券代码", "股票代码", "名称", "证券简称", "股票简称", "报告期", "日期", "时间"}
)


# 别名表：值为内部规范化字段名。匹配时按别名长度倒序（见 _alias_index），
# 因此长别名优先命中，避免 "市盈率" 把 "静态市盈率" 一并吞掉这类子串误合并。
FIELD_ALIASES = {
    "上涨家数": "advancing_count",
    "A股总家数": "market_total_count",
    # A name LIKE predicate is not an exchange risk-warning declaration.
    "股票简称like%st%": "name_contains_st",
    "like_st": "name_contains_st",
    "是否停牌": "is_suspended",
    "上市状态": "listing_status",
    "主力净买入额占成交额比例": "capital_flow_ratio",
    "区间涨跌幅": "interval_change",
    "成交额平均值": "interval_avg_turnover",
    "平均成交额": "interval_avg_turnover",
    "近一年最大回撤": "max_drawdown_1y",
    "近20日平均成交额": "avg_turnover_20d",
    "近20个交易日平均成交额": "avg_turnover_20d",
    "是否st": "is_st",
    "交易状态": "trading_status",
    "所属行业": "industry",
    "行业名称": "industry",
    "m2同比增长率": "m2_growth",
    "广义货币同比增长率": "m2_growth",
    "市场上涨家数占比": "market_advancing_ratio",
    "行业营业收入同比增长率": "industry_revenue_growth",
    "行业换手率历史百分位": "industry_turnover_percentile",
    "毛利率": "gross_margin",
    "净利率": "net_margin",
    "营业利润率": "operating_margin",
    "cpi同比": "cpi",
    "ppi同比": "ppi",
    "制造业pmi": "pmi",
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
    "所属同花顺三级行业": "industry",
    "资金流向": "capital_flow",
    "主力净买入额": "capital_flow",
    "主力资金净流入": "capital_flow",
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
    "单位净值增长率": "nav_change",
    "净值增长率": "nav_change",
    "单位净值": "fund_nav",
    "最新净值日期": "nav_date",
    "基金规模": "fund_size",
    "基金经理": "fund_manager",
    "成立日期": "inception_date",
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
    "中文名称": "company_name",
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
    "债券余额": "remaining_size",
    "转股价值": "conversion_value",
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

    cache_method_aliases = {"get_event_data": "get_announcements"}

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
        skill_api_keys: Mapping[str, str] | None = None,
        public_recovery: Any | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("IWENCAI_API_KEY 不能为空")
        self._api_key = api_key.strip()
        self.public_recovery = public_recovery
        self._skill_api_keys = {skill: key.strip() for skill, key in (skill_api_keys or {}).items()
                                if skill in SKILL_KEY_ENV and key.strip()}
        # Partition public caches by entitlement without persisting credentials.
        self.cache_namespace = hashlib.sha256(json.dumps([SKILL_CONTRACT_REVISION, base_url, self._api_key,
            self._skill_api_keys, public_recovery is not None], sort_keys=True).encode()).hexdigest()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.transport = transport
        self._semaphore = asyncio.Semaphore(max_concurrency)
        # Provider 是应用级单例；复用一个异步客户端才能真正复用 TCP/TLS 连接池。
        # 每次调用仍生成独立追踪 ID，并保留原有超时、重试和熔断语义。
        self._client = httpx.AsyncClient(transport=transport, timeout=timeout_seconds,
            limits=httpx.Limits(max_connections=max_concurrency, max_keepalive_connections=max_concurrency))
        self.call_observer: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._failure_count = 0
        self._circuit_open_until: datetime | None = None
        self._quota_cooldowns: dict[str, tuple[datetime, int]] = {}
        self._calendar_tasks: dict[tuple[str, str], tuple[datetime, asyncio.Task]] = {}

    async def aclose(self) -> None:
        """在应用退出时释放问财连接池。"""

        for _, task in self._calendar_tasks.values():
            if not task.done():
                task.cancel()
        if self._calendar_tasks:
            await asyncio.gather(*(task for _, task in self._calendar_tasks.values()), return_exceptions=True)
        await self._client.aclose()
        if self.public_recovery is not None:
            await self.public_recovery.aclose()

    @classmethod
    def from_env(cls) -> "IwencaiSkillHubProvider | None":
        api_key = os.getenv("IWENCAI_API_KEY", "").strip()
        if not api_key:
            return None
        from backend.app.data_provider.public_recovery import PublicMarketRecovery
        return cls(
            api_key,
            base_url=os.getenv("IWENCAI_BASE_URL", "https://openapi.iwencai.com"),
            skill_api_keys={skill: os.getenv(env_name, "") for skill, env_name in SKILL_KEY_ENV.items()},
            public_recovery=PublicMarketRecovery() if os.getenv('IWENCAI_PUBLIC_RECOVERY_ENABLED', '1') == '1' else None,
        )

    async def get_adjusted_stock_history(self, code: str, start: str, end: str):
        return await self.public_recovery.get_adjusted_stock_history(code, start, end) if self.public_recovery else []

    async def get_exchange_calendar(self, start: str, end: str):
        return await self.public_recovery.get_exchange_calendar(start, end) if self.public_recovery else []

    async def get_stock_risk_state(self, code: str):
        return await self.public_recovery.get_stock_risk_state(code) if self.public_recovery else []

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
            "report": ("report-search", "1.0.0"),
        }
        try:
            skill_id, skill_version = skills[channel]
        except KeyError as exc:
            raise ValueError(f"不支持的问财综合搜索频道：{channel}") from exc
        payload = {
            "channels": [channel],
            "app_id": "AIME_SKILL",
            "query": query,
            "size": 10,
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
            blocked = self._quota_cooldowns.get(skill_id)
            if blocked and datetime.now(timezone.utc) < blocked[0]:
                raise ProviderCallError("问财当前技能的查询额度已用完，额度恢复后可继续查询。",
                                        code="PROVIDER_QUOTA_EXHAUSTED", status_code=blocked[1])
            for attempt in range(self.max_retries + 1):
                headers = {
                    "Authorization": f"Bearer {self._skill_api_keys.get(skill_id, self._api_key)}",
                    "Content-Type": "application/json",
                    "X-Claw-Call-Type": "normal" if attempt == 0 else "retry",
                    "X-Claw-Skill-Id": skill_id,
                    "X-Claw-Skill-Version": skill_version,
                    "X-Claw-Plugin-Id": "none",
                    "X-Claw-Plugin-Version": "none",
                    "X-Claw-Trace-Id": secrets.token_hex(32),
                }
                try:
                    facts = await self._tracked_attempt(
                        path, payload, headers, attempt=attempt, entity_hint=entity_hint,
                        skill_id=skill_id, channel=channel, history_metric=history_metric,
                        history_limit=history_limit,
                    )
                    self._failure_count = 0
                    return facts
                except ProviderCallError as exc:
                    if exc.code == "PROVIDER_QUOTA_EXHAUSTED":
                        # Avoid repeating the same exhausted skill throughout
                        # a candidate batch. This is a short cooldown, not an
                        # assertion about the vendor's daily reset time.
                        self._quota_cooldowns[skill_id] = (
                            datetime.now(timezone.utc) + timedelta(minutes=5), exc.status_code or 200)
                    raise
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
        # Permission/parameter rejection for one query is not proof that the
        # whole provider is unavailable. Only transient service/network faults
        # participate in the provider-wide breaker; 4xx queries still fail.
        provider_fault = not isinstance(last_error, httpx.HTTPStatusError) or (
            last_error.response.status_code in {408, 425, 429}
            or last_error.response.status_code >= 500
        )
        if provider_fault:
            self._failure_count += 1
            if self._failure_count >= 3:
                self._circuit_open_until = datetime.now(timezone.utc) + timedelta(seconds=30)
        status = last_error.response.status_code if isinstance(last_error, httpx.HTTPStatusError) else None
        code = {401: "AUTHENTICATION_REJECTED", 403: "CAPABILITY_FORBIDDEN",
                429: "PROVIDER_RATE_LIMITED"}.get(status, "PROVIDER_UNAVAILABLE")
        raise ProviderCallError(self._query_error_message(last_error), code=code, status_code=status) from last_error

    async def _tracked_attempt(
        self, path: str, payload: dict[str, object], headers: dict[str, str], *,
        attempt: int, entity_hint: str, skill_id: str, channel: str | None,
        history_metric: str | None, history_limit: int,
    ) -> list[FactRecord]:
        started = perf_counter()
        event: dict[str, Any] = {
            "user_id": calling_user_id.get(), "endpoint": path, "skill_id": skill_id,
            "attempt": attempt, "status": "failed", "status_code": None,
            "fact_count": 0, "error_type": None, "created_at": datetime.now(timezone.utc),
        }
        try:
            response = await self._client.post(f"{self.base_url}{path}", headers=headers, json=payload)
            event["status_code"] = response.status_code
            if _quota_exhausted(response):
                raise ProviderCallError("问财当前技能的查询额度已用完，额度恢复后可继续查询。",
                                        code="PROVIDER_QUOTA_EXHAUSTED", status_code=response.status_code)
            response.raise_for_status()
            result = response.json()
            if history_metric == 'industry_revenue_aggregate':
                from backend.app.data_provider.industry_evidence import aggregate_industry_revenue
                facts = aggregate_industry_revenue(result, target=entity_hint, source_id=self.source_id)
            else:
                facts = (_history_facts(result, entity_hint=entity_hint, source_id=self.source_id,
                                    metric=history_metric, limit=history_limit)
                     if history_metric else self._normalize(result, entity_hint=entity_hint, channel=channel))
            event["status"] = "success" if facts else "empty"
            event["fact_count"] = len(facts)
            return facts
        except BaseException as exc:
            event["error_type"] = type(exc).__name__
            raise
        finally:
            event["duration_ms"] = (perf_counter() - started) * 1000
            if self.call_observer is not None:
                await self.call_observer(event)

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

    async def get_stock_risk_metrics(self, symbol: str) -> list[FactRecord]:
        return await self.query(
            f"{symbol} 近一年最大回撤、近20日平均成交额、是否ST、交易状态",
            entity_hint=symbol,
        )

    async def _recovery_series(self, target: str, start: str, end: str, *, kind: str, query: str):
        start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
        if not 350 <= (end_date - start_date).days <= 366:
            raise ValueError("恢复序列仅支持完整一年窗口")
        facts, signatures = [], set()
        for page in range(1, 4):
            batch = await self._request('/v1/query2data',
                {'query': query, 'page': str(page), 'limit': '100', 'is_cache': '1', 'expand_index': 'true'},
                entity_hint=target, skill_id='hithink-industry-query' if kind == 'industry_series' else 'hithink-market-query',
                skill_version='1.0.0', history_metric=kind)
            batch = [f for f in batch if start <= (f.period or '') <= end]
            signature = frozenset((f.entity_code, f.field, f.period, str(f.value), f.unit) for f in batch)
            if not batch or signature in signatures:
                break
            signatures.add(signature)
            facts.extend(batch)
        return facts

    async def get_stock_daily_history(self, symbol: str, start: str, end: str) -> list[FactRecord]:
        return await self._recovery_series(symbol, start, end, kind='stock_series',
            query=f'{symbol} {start}至{end} 每个交易日前复权收盘价、成交额，逐日列出交易日期及单位')

    async def get_market_calendar(self, start: str, end: str) -> list[FactRecord]:
        if self.public_recovery is not None:
            key, now = (start, end), datetime.now(timezone.utc)
            entry = self._calendar_tasks.get(key)
            if entry is None or (now - entry[0]).total_seconds() > 60 or (
                    entry[1].done() and (entry[1].cancelled() or entry[1].exception() is not None)):
                self._calendar_tasks = {k: v for k, v in self._calendar_tasks.items()
                                        if not v[1].done() or (now-v[0]).total_seconds() <= 60}
                entry = (now, asyncio.create_task(self.public_recovery.get_exchange_calendar(start, end)))
                self._calendar_tasks[key] = entry
            return await asyncio.shield(entry[1])
        return await self._recovery_series('中国A股交易日历', start, end, kind='calendar_series',
            query=f'沪深北交易所 {start}至{end} 交易日历，逐日列出交易日期、区间交易日总数、区间开始日期、区间结束日期，不含非交易日')

    async def get_stock_trading_status(self, symbol: str) -> list[FactRecord]:
        return await self.query(f'{symbol} 是否ST、是否停牌、上市状态、交易状态', entity_hint=symbol)

    async def get_industry_fundamentals(self, industry: str) -> list[FactRecord]:
        from backend.app.data_provider.industry_evidence import industry_query_name
        facts = await self.query(f'{industry_query_name(industry)}行业 营业收入同比增长率', entity_hint=industry,
                                 skill_id='hithink-industry-query')
        facts = [f.model_copy(update={'entity': industry}) if f.entity == industry_query_name(industry)
                 and str(f.entity_code or '').endswith('.TI') else f for f in facts]
        direct = [f.model_copy(update={'field': 'industry_revenue_growth'}) if f.field == 'revenue_growth' else f
                for f in facts if f.entity == industry and f.entity_code and f.entity_code.upper().endswith('.TI')
                and f.field in {'revenue_growth', 'industry_revenue_growth'}]
        if direct:
            return direct
        codes = {f.entity_code for f in facts if f.entity == industry and f.entity_code and f.entity_code.endswith('.TI')}
        if len(codes) != 1:
            return []
        from backend.app.data_provider.industry_evidence import latest_report_period, report_label
        current, previous = latest_report_period(datetime.now(timezone.utc))
        return await self._request('/v1/query2data', {'query':
            f'{industry_query_name(industry)}行业成分股 {report_label(current)}营业收入 {report_label(previous)}营业收入 所属同花顺行业',
            'page': '1', 'limit': '100', 'is_cache': '1', 'expand_index': 'true'},
            entity_hint=f'{industry}|{next(iter(codes))}|{current}|{previous}',
            skill_id='hithink-finance-query', skill_version='1.0.0', history_metric='industry_revenue_aggregate')

    async def get_industry_policy(self, industry: str) -> list[FactRecord]:
        return await self._comprehensive_search(f'{industry} 行业政策 官方发布', channel='news', entity_hint=industry)

    async def get_macro_policy(self, target: str) -> list[FactRecord]:
        return await self._comprehensive_search('中国 近期货币政策 财政政策 官方发布', channel='news', entity_hint=target)

    async def get_market_breadth(self, target: str) -> list[FactRecord]:
        facts = await self.query('同花顺全A(沪深京)指数 最新上涨家数、成份股总数、统计日期，两项统计范围及日期一致', entity_hint=target)
        # Only the explicitly queried all-A index can represent this China scope.
        return [f.model_copy(update={'entity': target}) if f.entity_code == '883957.TI' else f for f in facts]

    async def get_industry_flow(self, industry: str) -> list[FactRecord]:
        from backend.app.data_provider.industry_evidence import industry_query_name
        facts = await self.query(f'{industry_query_name(industry)}行业 主力净买入额 成交额 市盈率 市净率',
                                 entity_hint=industry, skill_id='hithink-industry-query')
        facts = [f.model_copy(update={'entity': industry}) if f.entity == industry_query_name(industry)
                 and str(f.entity_code or '').endswith('.TI') else f for f in facts]
        return [f for f in facts if f.entity == industry and f.entity_code and f.entity_code.upper().endswith('.TI')
                and f.field in {'capital_flow', 'turnover_value', 'capital_flow_ratio', 'pe_ttm', 'pb'}]

    async def get_industry_turnover_history(self, industry: str, start: str, end: str) -> list[FactRecord]:
        if self.public_recovery is not None:
            calendar = await self.get_market_calendar(start, end)
            days = sorted({f.period for f in calendar if f.field == 'market_session' and f.value == 1})
            declarations = {int(f.value) for f in calendar if f.field == 'market_session_count'
                            and f.period == f'{start}/{end}'}
            if declarations != {len(days)} or not 200 <= len(days) <= 270:
                return []
            # A range query returns one interval turnover, not daily observations.
            # Use bounded batches of actual exchange sessions, retaining column dates.
            stopped = asyncio.Event()
            lanes = asyncio.Semaphore(8)
            async def fetch(batch):
                from backend.app.data_provider.industry_evidence import industry_query_name
                query = f'{industry_query_name(industry)}行业 ' + ' '.join(
                    f'{day[:4]}年{int(day[5:7])}月{int(day[8:])}日换手率' for day in batch)
                async with lanes:
                    if stopped.is_set():
                        return []
                    try:
                        return await self._request('/v1/query2data',
                            {'query': query, 'page': '1', 'limit': '1', 'is_cache': '1', 'expand_index': 'true'},
                            entity_hint=industry, skill_id='hithink-industry-query', skill_version='1.0.0', history_metric='industry_series')
                    except ProviderCallError as error:
                        if error.code in {'AUTHENTICATION_REJECTED', 'CAPABILITY_FORBIDDEN', 'PROVIDER_QUOTA_EXHAUSTED'}:
                            stopped.set()
                        raise
            # The gateway resolves ten explicit daily indicators reliably;
            # longer compounds can silently return just an interval or no data.
            groups = [days[i:i+10] for i in range(0, len(days), 10)]
            results = await asyncio.gather(*(fetch(group) for group in groups), return_exceptions=True)
            successful = [f for result in results if not isinstance(result, BaseException) for f in result
                          if f.field == 'industry_turnover_history' and f.period in days]
            missing = sorted(set(days) - {f.period for f in successful})
            # Repair at most three missing observations once; never replace them
            # with an interval average or a prior day's turnover.
            if successful and 0 < len(missing) <= 3 and not stopped.is_set():
                try:
                    repaired = await fetch(missing)
                    successful.extend(f for f in repaired if f.field == 'industry_turnover_history' and f.period in missing)
                except ProviderCallError:
                    pass  # Preserve successful dates; coverage verification remains partial.
            if not successful:
                error = next((r for r in results if isinstance(r, Exception)), None)
                if error:
                    raise error
            return [*calendar, *successful]
        return await self._recovery_series(industry, start, end, kind='industry_series',
            query=f'{industry}同花顺行业板块 {start}至{end} 每个交易日换手率，逐日列出交易日期及单位')

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
        # Query actual columns rather than requesting our internal score names.
        facts = await self.query(
            f"{window}行业板块 市盈率、主力资金净流入、成交额",
            entity_hint="行业排名",
            skill_id="hithink-industry-query",
        )
        # The profile uses Tonghuashun industries. Shenwan (.SL) indexes may
        # share their display names but have different constituents/valuations.
        return [f for f in facts if not f.entity_code or f.entity_code.upper().endswith('.TI')]

    async def get_convertible_bond(self, target: str) -> list[FactRecord]:
        return await self.query(
            f"{target} 可转债最新价、涨跌幅、转股溢价率、纯债溢价率、到期收益率、剩余规模、债券评级和转股价",
            entity_hint=target,
        )

    async def get_basic_info(self, target: str) -> list[FactRecord]:
        return await self.query(
            f"{target} 所属同花顺三级行业、上市日期和主营业务",
            entity_hint=target,
            skill_id="hithink-basicinfo-query",
        )

    async def get_company_operations(self, target: str) -> list[FactRecord]:
        return await self.query(
            f"{target} 主营构成、主要客户、主要供应商、参控股公司和重大合同",
            entity_hint=target,
            skill_id="hithink-business-query",
        )

    async def get_shareholder_equity(self, target: str) -> list[FactRecord]:
        return await self.query(
            f"{target} 控股股东、实际控制人、总股本、流通股本、股东人数和机构持股",
            entity_hint=target,
            skill_id="hithink-management-query",
        )

    async def get_event_data(self, target: str) -> list[FactRecord]:
        # Events are disclosure records, not scalar market-query columns.
        # Keep their document dates and links through the announcement adapter.
        return await self.get_announcements(target)

    async def get_macro_data(self, query: str) -> list[FactRecord]:
        # 同时索取原始指标与评分维度：research 的派生规则会用 CPI/PPI/PMI 等原始值
        # 算出 growth_score 等分项，只取评分名会失去派生来源。
        facts = await self.query(
            query if query == "中国最新M2同比增长率" else
            f"{query} 宏观数据 CPI同比、PPI同比、制造业PMI、利率、汇率、社会融资、M2同比增长率、市场上涨家数占比、"
            "经济增长评分、通胀评分、流动性评分、政策评分和风险偏好评分",
            entity_hint="宏观数据",
            skill_id="hithink-macro-query",
            limit=3 if query == "中国最新M2同比增长率" else 30,
        )
        # Indicator names identify columns, not separate economic regions.
        # Only this server-controlled China query and exact known indicator
        # names share a scope. User snapshots, custom/multi-country queries and
        # differently named regional series retain their original entities.
        if query in {"中国最新宏观经济", "中国最新M2同比增长率"}:
            indicators = {
                "制造业PMI": "pmi", "CPI:当月同比": "cpi", "PPI:当月同比": "ppi",
                "CPI同比": "cpi", "PPI同比": "ppi",
                "居民消费价格指数": "cpi", "工业生产者出厂价格指数": "ppi",
                "采购经理指数": "pmi",
                "M2同比增长率": "m2_growth", "广义货币同比增长率": "m2_growth",
                "市场上涨家数占比": "market_advancing_ratio",
            }
            return [fact.model_copy(update={"entity": "中国宏观经济"})
                    if indicators.get(fact.entity.removeprefix("中国:")) == fact.field else fact for fact in facts]
        return facts

    async def get_institutional_research(self, target: str) -> list[FactRecord]:
        facts = await self.query(
            f"{target} 研报目标价",
            entity_hint=target,
            skill_id="hithink-insresearch-query",
        )
        # Publication dates cannot date live prices embedded in report rows.
        return [f for f in facts if f.field not in {'close_price', 'change'}]

    async def get_target_prices(self, target: str) -> list[FactRecord]:
        facts = await self.query(f'{target} 研报目标价',
                                 entity_hint=target, skill_id='hithink-insresearch-query', limit=10)
        # Irrelevant prices/profit forecasts cannot inflate a target-price repair.
        return [f for f in facts if f.field == 'target_price']

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

    async def get_governance_disclosures(self, target: str) -> list[FactRecord]:
        return await self._comprehensive_search(
            f"{target} 年度报告 审计意见 信息披露 监管",
            channel="announcement",
            entity_hint=target,
        )

    async def get_stock_disclosure_details(self, target: str) -> list[FactRecord]:
        # Distinct searches improve coverage without turning absence into a
        # claim of clean governance. All results still require exact citations.
        groups = await asyncio.gather(*(self._comprehensive_search(
            f'{target} {topic}', channel='announcement', entity_hint=target)
            for topic in ('最新年度报告 审计意见', '报告期内处罚与整改情况 监管措施',
                          '定期报告信息披露 及时披露 延期披露', '近期业绩预告 利润分配 回购 限售解禁')))
        return [fact for group in groups for fact in group]

    async def get_structured_events(self, target: str) -> list[FactRecord]:
        # A compound query can return an empty table for otherwise available events.
        groups = await asyncio.gather(*(self.query(f'{target} {topic}', entity_hint=target,
            skill_id='hithink-event-query', limit=10) for topic in
            ('最新业绩预告 公告日期', '近期限售解禁 解禁日期')))
        facts = [f for group in groups for f in group]
        if any(f.field in {'announcement', 'announcement_summary', 'event'} and f.source_url and f.period for f in facts):
            return facts
        documents = await self._comprehensive_search(f'{target} 最新业绩预告 半年度报告 权益分派',
                                                    channel='announcement', entity_hint=target)
        # Embedded live quotes are not dated by an event announcement date.
        return [f for f in facts if f.field not in {'close_price','change'}] + documents

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
            skill_id="hithink-sector-selector",
        )

    def _normalize(self, payload: Any, *, entity_hint: str, channel: str | None = None) -> list[FactRecord]:
        records = list(_find_record_lists(payload))
        snapshot_time = datetime.now(timezone.utc)
        columns = _root_column_metadata(payload)
        # 宽表（宏观、行业等）把多个指标放在同一列名下，靠"指标名称"区分。若不先
        # 展开，CPI/PPI/PMI 等会全部落到同一个字段名上，派生评分与专业节点就再也
        # 找不到所需字段。
        expanded = _expand_indicator_rows(records)
        facts: list[FactRecord] = []
        requested_codes = set(re.findall(r"(?<!\d)\d{6}(?!\d)", entity_hint)) if channel is None else set()
        for record in expanded[:100]:
            if channel in {'announcement', 'report', 'news'}:
                record = _search_entity_record(record, entity_hint)
            returned_code = _entity_code_from_record(record)
            # 上游会把已摘牌/不存在的代码模糊匹配成另一只证券，不能沿用为目标行情。
            if requested_codes and returned_code:
                matched = re.search(r"(?<!\d)\d{6}(?!\d)", returned_code)
                if matched and matched.group() not in requested_codes:
                    continue
            entity = _entity_from_record(record, entity_hint)
            # "一行即一条记录"的返回（综合搜索、机构调研/研报、事件等）整行共用
            # 一个逐条标识：同一份记录的多个字段共享 period，同一实体的多条记录
            # 彼此可区分，行级字段（研究员、研究机构、研报链接）不会互相判冲突。
            # 行情/财务/宏观等标量宽表取不到该标识，退回报告期口径，保持原有语义。
            record_scope = _record_scope(record)
            period = record_scope or _period_from_record(record)
            source_url = _source_url(record)
            for raw_field, value in record.items():
                if raw_field in {"币种", "currency", "目标价币种", "价格币种"}:
                    continue
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
                        unit=_percentage_unit(field, str(raw_field), record,
                                              column_unit=columns.get(str(raw_field), {}).get("unit")),
                        period=period if record_scope else (_field_period(str(raw_field)) or
                            _column_period(columns.get(str(raw_field), {})) or period),
                        observation_date=_observation_date(record, field, _field_period(str(raw_field)) or
                            _column_period(columns.get(str(raw_field), {}))),
                        entity_code=_entity_code_from_record(record),
                        source_field=(f"{record['指标名称']} ({raw_field})" if record.get("指标名称") else str(raw_field)),
                        snapshot_time=snapshot_time,
                        source_id=self.source_id,
                        source_url=source_url,
                        quality=0.90,
                    )
                )
        if facts:
            return facts
        if records or (isinstance(payload, dict) and isinstance(payload.get("datas"), list)):
            # Empty gateway tables have QTime/token/status metadata, not an answer.
            return []
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


def _quota_exhausted(response: httpx.Response) -> bool:
    """Recognize vendor quota rejection without retaining its message or tokens."""
    if response.status_code not in {200, 401, 403, 429}:
        return False
    try:
        payload = response.json()
    except ValueError:
        payload = response.text[:4096]
    if isinstance(payload, str):
        messages = [payload[:4096]]
    elif isinstance(payload, dict):
        if response.status_code == 200 and isinstance(payload.get("datas"), list) and payload["datas"]:
            return False
        messages = [payload.get(key) for key in ("message", "msg", "status_msg", "error_description")]
    else:
        return False
    # Restrict to gateway message fields, never scan returned facts or queries.
    phrases = ("今天的次数已用完", "今日查询额度已用完", "查询次数已用完",
               "查询额度已用完", "额度不足", "查询次数超限", "今日次数已用尽")
    return any(isinstance(message, str) and any(phrase in message for phrase in phrases)
               for message in messages)


def _root_column_metadata(payload: Any) -> dict[str, dict]:
    """Use units/dates from this table only; nested tables keep independent scopes."""
    columns = payload.get("columns") if isinstance(payload, dict) else None
    if not isinstance(columns, list):
        return {}
    metadata = {}
    for column in columns:
        if not isinstance(column, dict):
            continue
        key = column.get("key")
        if isinstance(key, str):
            if key in metadata and metadata[key] != column:
                metadata[key] = {}  # conflicting declarations are not evidence
            else:
                metadata[key] = column
    return metadata


def _column_period(column: Mapping[str, Any]) -> str | None:
    parsed = _history_date(column.get("timestamp"))
    return parsed.isoformat() if parsed else None


def _observation_date(record: Mapping[str, Any], field: str, column_date: str | None) -> date | None:
    if field == "target_price":
        # 盈利预测的财报期不能冒充机构目标价的发布日期。
        for key in ("研报发布日期", "发布日期", "publish_date", "发布时间"):
            parsed = _history_date(record.get(key))
            if parsed:
                return parsed
    # Range facts retain both endpoints in period; observation is the final day.
    if column_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}/\d{4}-\d{2}-\d{2}", column_date):
        column_date = column_date.split("/")[1]
    parsed = _history_date(column_date)
    if parsed:
        return parsed
    for key in ("交易日期", "统计日期", "净值日期", "最新净值日期", "日期", "时间",
                "研报发布日期", "发布日期", "publish_date", "发布时间", "公告日期"):
        parsed = _history_date(record.get(key))
        if parsed:
            return parsed
    return _history_date(record.get("报告期"))


def _percentage_unit(field: str, source_field: str, record: Mapping[str, Any], *,
                     column_unit: str | None = None) -> str | None:
    declared = str(record.get("指标单位") or record.get("单位") or record.get("unit") or "").strip()
    metadata_unit = str(column_unit or "").strip()
    units = {"%": "percent", "％": "percent", "元": "CNY", "人民币": "CNY", "RMB": "CNY",
             "港元": "HKD", "港币": "HKD", "美元": "USD", "百分比": "percent", "小数比例": "ratio"}
    if declared and metadata_unit and units.get(declared, declared) != units.get(metadata_unit, metadata_unit):
        return "conflicting"
    declared = metadata_unit or declared
    if field in {"close_price", "target_price", "conversion_price", "fund_nav"}:
        currencies = {"元": "CNY", "人民币": "CNY", "CNY": "CNY", "RMB": "CNY",
                      "港元": "HKD", "港币": "HKD", "HKD": "HKD", "美元": "USD", "USD": "USD"}
        specific = str((record.get("目标价币种") if field == "target_price" else record.get("价格币种")) or "").strip()
        specific = specific or str(record.get("币种") or record.get("currency") or "").strip()
        currency = currencies.get(specific.upper())
        declared_currency = currencies.get(declared.upper())
        if currency and declared_currency and currency != declared_currency:
            return "conflicting"
        if currency or declared_currency:
            return currency or declared_currency
        match = re.search(r"[\[(（](人民币|港元|港币|美元|CNY|HKD|USD|元)[\])）]", source_field)
        return currencies.get(match[1]) if match else None
    if field in {"capital_flow", "turnover_value", "avg_turnover_20d", "interval_avg_turnover"}:
        if declared in {"CNY", "元", "万元", "亿元"}:
            return "CNY" if declared == "元" else declared
        match = re.search(r"[\[(（](亿元|万元|元)[\])）]", source_field)
        return {"元": "CNY"}.get(match[1], match[1]) if match else None
    if field not in PERCENT_FIELDS:
        return None
    declared = declared.casefold()
    source_field = source_field + " " + str(record.get("指标名称") or "")
    if ("%" in source_field or "％" in source_field) and declared and declared not in {
            "%", "％", "percent", "百分比"}:
        return "conflicting"
    if declared in {"ratio", "小数比例"}:
        return "ratio"
    if declared in {"%", "％", "percent", "百分比"} or "%" in source_field or "％" in source_field:
        return "percent"
    if declared:
        return declared  # e.g. an index with base 100 is not a percentage rate
    # Query2data rate fields use percentage points. CPI/PPI indices require an
    # explicit percentage/yoy label; a bare index must never become inflation.
    if field in {"cpi", "ppi"} and not any(token in source_field.casefold() for token in ("同比", "增长", "yoy")):
        return None
    return "percent"


class CompositeProvider:
    """并行读取多个 DataProvider，并按实体/字段/报告期选择最新高质量事实。"""

    def __init__(self, providers: Iterable[Any]) -> None:
        self.providers = list(providers)
        self.public_recovery = any(getattr(provider, 'public_recovery', None) for provider in self.providers)

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

    async def get_stock_risk_metrics(self, symbol: str) -> list[FactRecord]:
        return await self._merge("get_stock_risk_metrics", symbol)

    async def get_stock_daily_history(self, symbol: str, start: str, end: str) -> list[FactRecord]:
        return await self._merge('get_stock_daily_history', symbol, start, end)

    async def get_market_calendar(self, start: str, end: str) -> list[FactRecord]:
        return await self._merge('get_market_calendar', start, end)

    async def get_exchange_calendar(self, start: str, end: str) -> list[FactRecord]:
        return await self._merge('get_exchange_calendar', start, end)

    async def get_adjusted_stock_history(self, code: str, start: str, end: str) -> list[FactRecord]:
        return await self._merge('get_adjusted_stock_history', code, start, end)

    async def get_stock_risk_state(self, code: str) -> list[FactRecord]:
        return await self._merge('get_stock_risk_state', code)

    async def get_stock_trading_status(self, symbol: str) -> list[FactRecord]:
        return await self._merge('get_stock_trading_status', symbol)

    async def get_industry_fundamentals(self, industry: str) -> list[FactRecord]:
        return await self._merge('get_industry_fundamentals', industry)

    async def get_industry_flow(self, industry: str) -> list[FactRecord]:
        return await self._merge('get_industry_flow', industry)

    async def get_industry_turnover_history(self, industry: str, start: str, end: str) -> list[FactRecord]:
        return await self._merge('get_industry_turnover_history', industry, start, end)

    async def get_stock_disclosure_details(self, target: str) -> list[FactRecord]:
        return await self._merge('get_stock_disclosure_details', target)

    async def get_structured_events(self, target: str) -> list[FactRecord]:
        return await self._merge('get_structured_events', target)

    async def get_industry_policy(self, industry: str) -> list[FactRecord]:
        return await self._merge('get_industry_policy', industry)

    async def get_macro_policy(self, target: str) -> list[FactRecord]:
        return await self._merge('get_macro_policy', target)

    async def get_market_breadth(self, target: str) -> list[FactRecord]:
        return await self._merge('get_market_breadth', target)

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

    async def get_governance_disclosures(self, target: str) -> list[FactRecord]:
        return await self._merge("get_governance_disclosures", target)

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
            if (any(marker in str(key) for marker in INDICATOR_VALUE_MARKERS)
                or (key not in INDICATOR_NAME_KEYS and _canonical_field(str(key)) == indicator))
            and not isinstance(value, (dict, list))
            and value not in (None, "")
        ]
        if not value_columns:
            expanded.append(record)
            continue
        # 日期后缀越大越新，只保留最近若干期，避免一次宏观查询塞进上百条事实。
        value_columns.sort(key=lambda item: _period_suffix(item[0]), reverse=True)
        for raw_key, value in value_columns[:MAX_PERIODS_PER_INDICATOR]:
            entity = _entity_from_record(record, name)
            # A declared region must survive indicator expansion. A generic
            # CPI label from a foreign row cannot become a China-scoped input.
            region = next((str(record[key]).strip() for key in ("国家", "地区", "country", "region")
                           if record.get(key) not in (None, "")), "")
            if region and region not in entity:
                entity = f"{region}:{entity}"
            row = {
                "证券简称": entity,
                "指标名称": name,
                indicator: value,
            }
            for unit_key in ("指标单位", "单位", "unit"):
                if unit_key in record:
                    row[unit_key] = record[unit_key]
            suffix = _period_suffix(raw_key)
            observed_period = suffix or _period_from_record(record)
            if observed_period:
                row["报告期"] = observed_period
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

    if metric in {'stock_series', 'industry_series', 'calendar_series'}:
        from backend.app.data_provider.recovery_series import parse_recovery_series
        return parse_recovery_series(payload, target=entity_hint, source_id=source_id, kind=metric)

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
        from backend.app.services.disclosure_reader import canonical_disclosure_url
        value = canonical_disclosure_url(value)
        try:
            return FactRecord.source_url_must_be_public_https(value)
        except ValueError:
            continue
    return None


def _search_entity_record(record, fallback):
    """Search relevance is not entity identity; use row security metadata first."""
    record = dict(record)
    infos = record.get('stock_infos')
    securities = [item for item in infos if isinstance(item, dict)
                  and re.fullmatch(r'\d{6}(?:\.(?:SH|SZ|BJ))?', str(item.get('code', '')))] if isinstance(infos, list) else []
    title = str(record.get('title') or record.get('标题') or '')
    title_codes = set(re.findall(r'(?<!\d)\d{6}(?!\d)', title))
    selected = [item for item in securities if str(item['code']).split('.')[0] in title_codes]
    if not selected and len(securities) == 1:
        selected = securities
    if len(selected) == 1:
        record['证券代码'] = str(selected[0]['code'])
        record['证券简称'] = selected[0].get('name') or str(selected[0]['code'])
    elif len(title_codes) == 1:
        record['证券代码'] = next(iter(title_codes))
        record['证券简称'] = next(iter(title_codes))
    elif '：' in title or ':' in title:
        issuer = re.split('[：:]', title, maxsplit=1)[0].strip()
        if 1 < len(issuer) <= 25:
            record['证券简称'] = issuer
    elif securities:
        # Multi-company articles are not evidence for the queried security alone.
        record['证券简称'] = '多证券资料'
    return record


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
    for key in ("证券简称", "名称", "股票简称", "基金简称", "基金名称", "指数简称", "指数名称",
                "可转债简称", "债券简称", "转债简称", "行业名称", "板块名称",
                "代码", "证券代码", "股票代码", "基金代码", "指数代码", "转债代码"):
        value = record.get(key)
        if value not in (None, ""):
            return str(value)
    return fallback


def _entity_code_from_record(record: dict[str, Any]) -> str | None:
    for key in ("证券代码", "股票代码", "基金代码", "指数代码", "可转债代码", "转债代码", "代码"):
        value = record.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _field_period(raw_field: str) -> str | None:
    """保留供应商逐列日期，财报期与行情日期不能互相代替。"""
    interval = re.search(r"\[(\d{8})-(\d{8})\]$", raw_field)
    if interval:
        try:
            start, end = (datetime.strptime(value, "%Y%m%d").date() for value in interval.groups())
            return f"{start.isoformat()}/{end.isoformat()}" if start <= end else None
        except ValueError:
            return None
    match = re.search(r"\[(\d{8})\]$", raw_field)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y%m%d").date().isoformat()
        except ValueError:
            pass
    return None


def _period_from_record(record: dict[str, Any]) -> str | None:
    for key in ("报告期", "统计日期", "日期", "时间", "交易日期", "净值日期", "最新净值日期",
                "研报发布日期", "发布日期", "发布时间", "公告日期", "publish_date"):
        value = record.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _canonical_field(raw: str, *, channel: str | None = None) -> str:
    normalized = re.sub(r"\[[^]]*]", "", raw).strip()
    key = normalized.casefold().replace(" ", "")
    if key == "停牌":
        return "is_suspended"
    # A date-range return must not conflict with the latest daily price change.
    if key == "涨跌幅" and re.search(r"\[\d{8}-\d{8}\]$", raw):
        return "interval_change"
    for marker, prefix in (("行业中值", "industry_median"), ("行业均值", "industry_mean"), ("行业平均", "industry_mean")):
        if marker in key:
            return f"{prefix}_{_canonical_field(key.replace(marker, ''))}"
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
