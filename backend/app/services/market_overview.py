"""固定、有限的问财只读概览；不调用模型或产生投资建议。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from backend.app.models.schemas import DataOverviewRequest, DataOverviewResponse, DataOverviewSection

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
                f"{INDEX_UNIVERSE}的指数简称、指数代码、最新价、涨跌幅、成交额", limit=4)),
            ("macro", "宏观指标", lambda: provider.get_macro_data("中国最新公布的CPI、PPI、制造业PMI和社会融资规模，注明统计月份")),
            ("news", "市场资讯", lambda: provider.get_news("最近3天A股市场政策与宏观经济，最多6条")),
        ]
    elif direction == "industry":
        jobs = [
            ("overview", "行业表现", lambda: provider.query(
                "板块筛选：同花顺行业板块，最新涨跌幅排名前8，显示板块简称、板块代码、涨跌幅、成交额、主力资金净流入、市盈率", limit=8, skill_id="hithink-astock-selector")),
            ("news", "行业资讯", lambda: provider.get_news("最近3天行业政策与产业动态，最多6条")),
        ]
    elif direction == "stock":
        jobs = [
            ("overview", "股票行情", lambda: provider.query(
                f"{STOCK_UNIVERSE}的股票简称、股票代码、最新价、涨跌幅、成交额、总市值、市盈率TTM、市净率、所属行业", limit=6)),
            ("detail", "财务指标", lambda: provider.get_financial_metrics(target)),
            ("company", "公司资料", lambda: provider.get_basic_info(target)),
            ("history", "近30期收盘价", lambda: provider.get_price_history(target, asset_type="股票", limit=30)),
        ]
    elif direction == "fund":
        query_target = target or "沪深300ETF，中证500ETF，科创50ETF"
        jobs = [
            ("overview", "基金与ETF", lambda: provider.query(
                f"筛选基金和ETF query={query_target}的基金代码、基金简称、单位净值、基金规模、管理费率，最多5只", limit=8, skill_id="hithink-fund-query")),
        ]
        if target:
            jobs.append(("history", "近30期单位净值", lambda: provider.get_price_history(target, asset_type="基金", limit=30)))
    else:
        query_target = target or "当前正常交易的可转债，按最近交易日成交额从高到低前8只"
        jobs = [
            ("overview", "可转债数据", lambda: provider.query(
                f"{query_target}，显示可转债简称、可转债代码、最新价、涨跌幅、成交额、转股溢价率、纯债溢价率、到期收益率、剩余规模、债券评级、转股价，最多8只", limit=8)),
        ]
        if target:
            jobs.append(("history", "近30期收盘价", lambda: provider.get_price_history(target, asset_type="可转债", limit=30)))

    return jobs


async def load_section(provider, key, title, call):
    if provider is None:
        return DataOverviewSection(key=key, title=title, status="unavailable", message="问财数据源尚未配置，请配置只读密钥并重启分析服务。")
    try:
        facts = await asyncio.wait_for(call(), timeout=26)
        facts = [fact for fact in facts if fact.field != "provider_response"]
        return DataOverviewSection(key=key, title=title, status="ok" if facts else "empty", facts=facts[:500],
                                   message=None if facts else "问财本次未返回可展示的结构化数据。")
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
