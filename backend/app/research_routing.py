"""五个研究入口的受控知识卡及轻量检索。

这里只检索产品自身的能力边界，不把检索文本当成市场事实或投资证据。
模型仍负责理解自然语言；后端只接受固定 Intent 并执行固定路由。
"""
from __future__ import annotations

import re

from backend.app.models import Intent


DIRECTION_FOR_INTENT: dict[Intent, str] = {
    Intent.MARKET_ANALYSIS: "市场解读",
    Intent.INDUSTRY_ANALYSIS: "行业分析",
    Intent.SECURITY_RESEARCH: "个股研究",
    Intent.FUND_SCREENING: "基金筛选",
    Intent.CONVERTIBLE_BOND_ANALYSIS: "可转债分析",
}

ROUTE_CARDS = (
    (Intent.MARKET_ANALYSIS, "市场解读", "大盘、指数、宏观环境、全市场走势和总体风险；不分析某只股票、基金或可转债。",
     "当前 A 股市场如何；沪深 300 走势；宏观政策对市场的影响"),
    (Intent.INDUSTRY_ANALYSIS, "行业分析", "行业或板块的景气、估值、资金和对比；板块筛选也属于这里。",
     "比较半导体和白酒行业；筛选景气较高的行业板块"),
    (Intent.SECURITY_RESEARCH, "个股研究", "A 股上市公司及股票的研究、比较和条件筛选；股票池筛选属于这里，不属于基金筛选。",
     "研究贵州茅台；比较两只股票；筛选沪深 A 股中 ROE 大于 8% 的非 ST 公司"),
    (Intent.FUND_SCREENING, "基金筛选", "公募基金和 ETF 的筛选、比较、费率、净值及跟踪质量；不筛选股票。",
     "筛选低费率沪深 300 ETF；比较两只基金"),
    (Intent.CONVERTIBLE_BOND_ANALYSIS, "可转债分析", "可转债的价格、转股溢价率、债性和发行人风险；不把可转债当普通股票。",
     "分析某只可转债；比较转股溢价率和到期收益率"),
)


def user_question(query: str) -> str:
    """旧会话中的页名前缀只是界面标记，不参与本轮意图判断。"""
    for _, label, _, _ in ROUTE_CARDS:
        prefix = f"{label}："
        if query.startswith(prefix):
            return query[len(prefix):].strip()
    return query.strip()


def _terms(value: str) -> set[str]:
    value = value.casefold()
    chinese = re.findall(r"[\u4e00-\u9fff]+", value)
    return {word for word in re.findall(r"[a-z]+|\d+", value) if len(word) > 1} | {
        segment[index:index + 2] for segment in chinese for index in range(len(segment) - 1)
    }


def route_guidance(query: str, *, context: list[str] | None = None, limit: int = 3) -> list[dict[str, str]]:
    """从固定入口知识卡检索相关说明；弱匹配时保留全部卡片供模型判断。"""
    question = user_question(query)
    previous = " ".join(user_question(item) for item in (context or [])[-2:])
    terms = _terms(question) or _terms(previous)
    scored = []
    for intent, label, scope, examples in ROUTE_CARDS:
        score = len(terms & _terms(f"{scope} {examples}"))
        scored.append((score, intent, label, scope, examples))
    if not any(score for score, *_ in scored):
        chosen = scored
    else:
        chosen = sorted(scored, key=lambda item: -item[0])[:limit]
    return [
        {"intent": intent.value, "direction": label, "scope": scope, "examples": examples}
        for _, intent, label, scope, examples in chosen
    ]
