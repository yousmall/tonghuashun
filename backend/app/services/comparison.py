"""最多四个标的的只读对比资料；逐标的校验身份后才返回事实。"""
import asyncio
from datetime import datetime, timezone
import re
from backend.app.models.schemas import DataOverviewRequest, ComparisonItem, ComparisonResponse

DIRECTIONS = {"股票": "stock", "基金": "fund", "可转债": "convertible", "行业": "industry"}
FIELDS = {
    "股票": "股票简称、股票代码、最新价、涨跌幅、市盈率TTM、市净率、所属行业、净资产收益率、营收同比增长、净利润同比增长",
    "基金": "基金简称、基金代码、单位净值、净值涨跌幅、基金规模、管理费率、基金经理、跟踪误差",
    "可转债": "可转债简称、可转债代码、最新价、涨跌幅、转股溢价率、纯债溢价率、到期收益率、剩余规模、债券评级",
    "行业": "板块简称、板块代码、涨跌幅、市盈率、成交额、主力资金净流入",
}


def matching_fact(target, fact):
    code = re.fullmatch(r"(\d{5,6})(?:\.([A-Za-z]{2}))?", target)
    if code:
        actual = str(fact.entity_code or "").upper()
        expected = code.group(1)
        if code.group(2):
            return actual == expected + "." + code.group(2).upper()
        return actual.split(".")[0] == expected
    return target.casefold() == str(fact.entity).strip().casefold()


async def fetch_snapshots(cache, provider, targets, *, wait=True, refresh=False):
    async def load(target, kind, item_id):
        direction = DIRECTIONS[kind]
        skill = "hithink-fund-query" if kind == "基金" else "hithink-astock-selector" if kind == "行业" else "hithink-market-query"
        jobs = [("overview", "行情与指标", lambda: provider.query(f"{target}的{FIELDS[kind]}，逐项注明数据日期和单位", limit=1, skill_id=skill))]
        if kind == "股票":
            jobs.append(("detail", "财务指标", lambda: provider.get_financial_metrics(target)))
        result = await cache.get(provider, DataOverviewRequest(direction=direction, target=target, wait=wait, refresh=refresh),
                                 jobs=jobs, namespace="comparison")
        raw = [fact for section in result.sections if section.status == "ok" for fact in section.facts]
        facts = [fact for fact in raw if matching_fact(target, fact)]
        # 同一请求不能任意拼接多个证券代码；名称匹配但身份冲突时整体拒绝。
        codes = {fact.entity_code for fact in facts if fact.entity_code}
        mismatched = bool(raw and not facts) or len(codes) > 1
        if mismatched:
            facts = []
        loading = any(section.status == "loading" for section in result.sections)
        status = "loading" if loading else "partial" if facts and result.status != "ok" else "ok" if facts else "unavailable" if result.status == "unavailable" else "empty"
        message = "数据返回的标的与请求不一致，未用于展示。" if mismatched else None
        if not facts and not loading and not message:
            message = "未取得可核对的标的数据。"
        return ComparisonItem(target=target, asset_type=kind, watchlist_id=item_id, status=status,
            entity=facts[0].entity if facts else None, entity_code=next(iter(codes)) if len(codes) == 1 and facts else None,
            facts=facts[:80], fetched_at=result.fetched_at, message=message)
    items = await asyncio.gather(*(load(*target) for target in targets))
    identities = set()
    for item in items:
        if item.entity_code and item.entity_code in identities:
            item.status, item.facts, item.message = "empty", [], "解析后与另一标的相同，请选择不同标的。"
        elif item.entity_code:
            identities.add(item.entity_code)
    count = sum(bool(item.facts) for item in items)
    status = "loading" if any(item.status == "loading" for item in items) else "ok" if count == len(items) and all(item.status == "ok" for item in items) else "partial" if count else "unavailable" if any(item.status == "unavailable" for item in items) else "empty"
    return ComparisonResponse(fetched_at=datetime.now(timezone.utc), status=status, items=items)
