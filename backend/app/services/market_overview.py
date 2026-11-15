"""固定、有限的问财只读概览；不调用模型或产生投资建议。"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any

from backend.app.models.schemas import DataOverviewRequest, DataOverviewResponse, DataOverviewSection
from backend.app.services.provider_errors import ProviderCallError
from backend.app.services.comparison import matching_fact

STOCK_UNIVERSE = "贵州茅台、宁德时代、招商银行、中国平安、美的集团、长江电力"
INDEX_UNIVERSE = "上证指数、深证成指、创业板指、沪深300"


def overview_jobs(provider: Any, request: DataOverviewRequest) -> list:
    direction = request.direction
    target = request.target or ("600519" if direction == "stock" else None)
    jobs = []
    # 所有方法、SkillHub 能力与条数均来自固定模板，目标只作为自然语言查询对象。
    if direction == "market":
        jobs = [
            ("overview", "主要指数", lambda: provider.query(
                f"{target or INDEX_UNIVERSE}的指数简称、指数代码、最新价、涨跌幅、成交额", limit=4)),
            ("macro", "宏观指标", lambda: provider.get_macro_data("中国最新公布的CPI、PPI、制造业PMI和社会融资规模，注明统计月份")),
            ("news", "市场资讯", lambda: provider.get_news("最近3天A股市场政策与宏观经济，最多6条")),
        ]
    elif direction == "industry":
        query = (f"{target} 行业板块的板块简称、板块代码、涨跌幅、成交额、主力资金净流入、市盈率TTM、市净率，注明行情日期"
                 if target else "板块筛选：同花顺行业板块，最新涨跌幅排名前8，显示板块简称、板块代码、涨跌幅、成交额、主力资金净流入、市盈率")
        jobs = [
            ("overview", "行业表现", lambda: provider.query(
                query, limit=8, skill_id="hithink-industry-query" if target else "hithink-astock-selector")),
            ("news", "行业资讯", lambda: provider.get_news(
                f"最近3天{target or ''}行业政策与产业动态，最多6条")),
        ]
    elif direction == "stock":
        jobs = [
            ("overview", "股票行情", lambda: provider.query(
                f"{target}的股票简称、股票代码、最新价、涨跌幅、成交额、总市值、市盈率TTM、市净率、所属行业", limit=6)),
            ("detail", "财务指标", lambda: scoped_snapshot(provider, "get_financial_metrics", target)),
            ("company", "公司资料", lambda: scoped_snapshot(provider, "get_basic_info", target)),
            ("history", "近30期收盘价", lambda: provider.get_price_history(target, asset_type="股票", limit=30)),
        ]
    elif direction == "fund":
        query_target = target or "沪深300ETF，中证500ETF，科创50ETF"
        jobs = [
            ("overview", "基金与ETF", lambda: provider.query(
                f"{query_target}的基金代码、基金简称、单位净值、净值增长率、累计净值、基金规模、基金类型、基金经理、管理费率，最多5只", limit=8, skill_id="hithink-fund-query")),
        ]
        if target:
            jobs.extend([
                ("profile", "基金概况与费率", lambda: fund_snapshot(provider, target,
                    "基金类型、基金管理人、基金经理、基金托管人、成立日期、基金规模、跟踪标的、业绩比较基准、申购状态、赎回状态、管理费率、托管费率、销售服务费率、申购费率、赎回费率")),
                ("performance", "阶段收益与风险", lambda: fund_snapshot(provider, target,
                    "近1月收益率、近3月收益率、近6月收益率、近1年收益率、近3年收益率、今年以来收益率、基金风险等级、近1年最大回撤、近1年年化波动率、近1年夏普比率、跟踪误差")),
                ("allocation", "持仓与配置", lambda: fund_snapshot(provider, target,
                    "股票仓位、债券仓位、现金仓位、前十大重仓股占比、重仓证券、行业配置")),
                ("history", "近30期单位净值", lambda: provider.get_price_history(target, asset_type="基金", limit=30)),
                ("news", "基金资讯", lambda: provider.get_news(f"最近3天{target}基金相关资讯，最多6条")),
            ])
            if callable(getattr(provider, "get_fund_reference", None)):
                jobs.append(("reference", "基金公开档案与同源核对", lambda: provider.get_fund_reference(target)))
    else:
        # 问财会把冗长的排序、状态和指标描述一起当作筛选条件，导致正常行情返回零行。
        # 条数由 API 的 limit 控制；缺失的指标在页面上明确显示为缺失。
        query = (f"{target} 可转债最新价、涨跌幅、转股溢价率" if target else
                 "可转债按成交额从高到低前8只，最新价、涨跌幅、转股溢价率")
        jobs = [
            ("overview", "可转债数据", lambda: provider.query(
                query, limit=8)),
        ]
        if target:
            jobs.append(("history", "近30期收盘价", lambda: provider.get_price_history(target, asset_type="可转债", limit=30)))

    return jobs


async def fund_snapshot(provider, target, fields):
    is_code = bool(re.fullmatch(r"\d{5,6}(?:\.[A-Za-z]{2})?", target))
    facts = await provider.query(f"{target}的基金简称、基金代码、{fields}，逐项注明数据日期和单位",
                                 limit=1 if is_code else 5, skill_id="hithink-fund-query")
    # 配置宽表必须归属于查询基金；拒绝把重仓证券等其他实体混入基金指标。
    if not is_code:
        identities = {(fact.entity, fact.entity_code) for fact in facts if fact.entity_code}
        # 简称与数据源全称可能不同；只有唯一的返回身份才接受简称解析。
        if len(identities) == 1:
            identity = next(iter(identities))
            return [fact for fact in facts if (fact.entity, fact.entity_code) == identity]
    return [fact for fact in facts if matching_fact(target, fact)]


async def scoped_snapshot(provider, method, target):
    """Mark exact capability results so research can reuse the same batch."""
    facts = await getattr(provider, method)(target)
    scope = re.sub(r"\s+", "", target.strip()).casefold()
    return [fact.model_copy(update={"produced_by": f"{method}@{scope}"}) for fact in facts]


async def load_section(provider, key, title, call):
    if provider is None:
        return DataOverviewSection(key=key, title=title, status="unavailable", message="问财数据源尚未配置，请配置只读密钥并重启分析服务。")
    try:
        facts = await asyncio.wait_for(call(), timeout=26)
        facts = [fact for fact in facts if fact.field != "provider_response"]
        return DataOverviewSection(key=key, title=title, status="ok" if facts else "empty", facts=facts[:500],
                                   message=None if facts else "问财本次未返回可展示的结构化数据。")
    except ProviderCallError as exc:
        return DataOverviewSection(key=key, title=title, status="unavailable", message=str(exc))
    except (RuntimeError, TimeoutError, ValueError):
        return DataOverviewSection(key=key, title=title, status="unavailable", message="本栏问财数据暂未取得，请稍后刷新。")


def overview_response(request, sections):
    available = sum(section.status == "ok" for section in sections)
    status = "loading" if any(section.status == "loading" for section in sections) else (
        "ok" if available == len(sections) else "partial" if available else
        "unavailable" if any(section.status == "unavailable" for section in sections) else "empty")
    observed = [section.fetched_at for section in sections if section.fetched_at]
    return DataOverviewResponse(direction=request.direction,
        target=request.target or ("600519" if request.direction == "stock" else None),
        fetched_at=min(observed) if observed else datetime.now(timezone.utc), status=status, sections=sections)


async def fetch_overview(provider: Any, request: DataOverviewRequest) -> DataOverviewResponse:
    sections = await asyncio.gather(*(load_section(provider, *job) for job in overview_jobs(provider, request)))
    return overview_response(request, sections)
