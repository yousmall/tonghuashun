"""主要指数看板：报价与历史点位并行读取，输出描述性走势观察。

与 ``market_overview`` 一样，查询文本与能力都来自固定模板，用户不能指定
任意 URL、方法或参数。历史点位只用于图表与走势描述，不进入投资建议的
证据链与合规审核。
"""
from __future__ import annotations

import asyncio
import math
from datetime import date, datetime, timezone
from typing import Any

from backend.app.models.index_board import IndexBoardResponse, IndexQuoteItem
from backend.app.services.trend_analytics import analyze_trend, close_series

INDEXES: tuple[tuple[str, str], ...] = (
    ("上证指数", "000001.SH"),
    ("深证成指", "399001.SZ"),
    ("创业板指", "399006.SZ"),
    ("沪深300", "000300.SH"),
    ("中证500", "000905.SH"),
    ("科创50", "000688.SH"),
)
BENCHMARK = "沪深300"
HISTORY_LIMIT = 30


def _public_error(error: BaseException) -> str:
    """保留额度/鉴权/超时原因，不把原始异常里的请求详情带到页面。"""
    messages = {
        "PROVIDER_QUOTA_EXHAUSTED":"问财当前技能的查询额度已用完，额度恢复后可继续查询。",
        "AUTHENTICATION_REJECTED":"问财密钥无效或已失效，请检查数据源配置。",
        "CAPABILITY_FORBIDDEN":"当前数据源未开通所需查询权限。",
    }
    if getattr(error,"code",None) in messages:
        return messages[error.code]
    return "指数数据查询超时，请稍后刷新。" if isinstance(error,TimeoutError) else "指数数据源暂不可用，请稍后刷新。"


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _matches(fact: Any, name: str, code: str) -> bool:
    actual = str(getattr(fact, "entity_code", "") or "").upper()
    if actual:
        return actual.split(".")[0] == code.split(".")[0]
    return str(getattr(fact, "entity", "") or "").strip() == name


def _quote_item(facts: list[Any], name: str, code: str) -> dict[str, Any]:
    matched = [fact for fact in facts if _matches(fact, name, code)]
    fields: dict[str, Any] = {}
    for fact in matched:
        if fact.field in {"close_price", "change", "turnover_value", "nav_change"}:
            fields.setdefault(fact.field, fact)
    close = _number(fields["close_price"].value) if "close_price" in fields else None
    change = _number(fields["change"].value) if "change" in fields else None
    turnover = _number(fields["turnover_value"].value) if "turnover_value" in fields else None
    period = None
    if "close_price" in fields and fields["close_price"].period:
        try:
            period = date.fromisoformat(str(fields["close_price"].period))
        except ValueError:
            period = None
    return {"latest_close": close, "change_pct": change, "turnover_value": turnover, "trade_date": period}


async def _load_history(provider: Any, name: str, semaphore: asyncio.Semaphore) -> list[Any]:
    async with semaphore:
        return await asyncio.wait_for(
            provider.get_price_history(name, "指数", limit=HISTORY_LIMIT), timeout=22)


async def fetch_index_board(provider: Any, *, limit: int = HISTORY_LIMIT) -> IndexBoardResponse:
    """一次读取六个主要指数；单个指数失败只影响它自己的卡片。"""

    now = datetime.now(timezone.utc)
    if provider is None:
        return IndexBoardResponse(
            fetched_at=now, status="unavailable",
            notes=["问财数据源尚未配置，无法读取指数行情与历史点位。"],
            items=[IndexQuoteItem(name=name, code=code, status="unavailable",
                                  message="数据源尚未配置") for name, code in INDEXES],
        )
    names = "、".join(name for name, _ in INDEXES)
    # 失败原因要如实回传给页面：额度用完、密钥无效和“真的没有数据”不是一回事。
    quote_error: str | None = None
    try:
        quotes = await asyncio.wait_for(provider.query(
            f"{names}的指数简称、指数代码、最新价、涨跌幅、成交额", limit=len(INDEXES)), timeout=26)
    except (RuntimeError, TimeoutError, ValueError) as exc:
        quotes = []
        quote_error = _public_error(exc)
    semaphore = asyncio.Semaphore(4)
    results = await asyncio.gather(
        *(_load_history(provider, name, semaphore) for name, _ in INDEXES),
        return_exceptions=True,
    )
    series: dict[str, Any] = {}
    history_errors = {}
    for (name, _code), result in zip(INDEXES, results):
        if isinstance(result, BaseException) or not result:
            if isinstance(result, BaseException):
                history_errors[name] = _public_error(result)
            continue
        cleaned = close_series([(getattr(fact, "period", None), getattr(fact, "value", None))
                                for fact in result])
        if len(cleaned) >= 2:
            series[name] = cleaned
    benchmark = series.get(BENCHMARK)
    items: list[IndexQuoteItem] = []
    notes: list[str] = []
    for name, code in INDEXES:
        quote = _quote_item(quotes, name, code)
        cleaned = series.get(name)
        analysis = None
        if cleaned is not None:
            analysis = analyze_trend(
                cleaned.points,
                benchmark=benchmark.points if benchmark is not None and name != BENCHMARK else None,
                benchmark_name=BENCHMARK if benchmark is not None and name != BENCHMARK else None,
            )
        available = cleaned is not None and len(cleaned) >= 2
        latest = quote["latest_close"] if quote["latest_close"] is not None else (
            analysis.latest_close if analysis else None)
        has_trend = analysis is not None and analysis.available
        item = IndexQuoteItem(
            name=name, code=code,
            status="ok" if latest is not None and has_trend else "empty" if latest is not None or has_trend else "unavailable",
            latest_close=latest,
            change_pct=quote["change_pct"],
            turnover_value=quote["turnover_value"],
            trade_date=quote["trade_date"] or (analysis.latest_date if analysis else None),
            ma5=analysis.ma5 if analysis else None,
            three_day_pct=analysis.three_day.change_pct if analysis and analysis.three_day.available else None,
            three_day_height=analysis.three_day.height if analysis and analysis.three_day.available else None,
            verdict=analysis.verdict if analysis else "数据不足",
            score=analysis.score if analysis else None,
            confidence=analysis.confidence if analysis else 0.0,
            analysis=analysis,
            points=[round(value, 4) for _, value in cleaned.points] if cleaned else [],
            dates=[day for day, _ in cleaned.points] if cleaned else [],
            message=None if available else (history_errors.get(name) or "问财本次未返回带日期的历史点位。"),
        )
        items.append(item)
        if not available:
            notes.append(f"{name}的历史点位暂未取得，未给出走势判断。" + history_errors.get(name,""))
    if quote_error:
        notes.append(f"本次指数行情未取得：{quote_error}")
    ok = sum(item.status == "ok" for item in items)
    status = "ok" if ok == len(items) else "partial" if ok or quotes or series else "unavailable"
    if series and not quotes:
        notes.append("本次只取得历史点位，最新涨跌幅缺失，按点位日期显示。")
    if benchmark is None:
        notes.append("沪深300历史点位不足，未计算相对基准强弱。")
    return IndexBoardResponse(fetched_at=now, status=status, items=items, notes=notes)


class IndexBoardCache:
    """60 秒有界缓存与同一请求合并；不缓存账户或投资建议。"""

    def __init__(self, ttl: int = 60, max_entries: int = 4) -> None:
        self.ttl, self.max_entries = ttl, max_entries
        self._entries: dict[int, tuple[float, asyncio.Task]] = {}

    async def get(self, provider: Any, *, refresh: bool = False) -> IndexBoardResponse:
        if provider is None:
            return await fetch_index_board(None)
        key = id(provider)
        loop = asyncio.get_running_loop()
        now = loop.time()
        entry = self._entries.get(key)
        if entry is not None and entry[1].get_loop() is not loop:
            # 事件循环已更换（进程内重建应用），旧任务结果不可复用。
            self._entries.pop(key, None)
            entry = None
        if entry is None or refresh or now - entry[0] >= self.ttl:
            while len(self._entries) >= self.max_entries:
                self._entries.pop(next(iter(self._entries)))
            task = loop.create_task(fetch_index_board(provider))
            self._entries[key] = (now, task)
            entry = (now, task)
        try:
            return await asyncio.shield(entry[1])
        except asyncio.CancelledError:
            raise
        except Exception:
            self._entries.pop(key, None)
            return IndexBoardResponse(
                fetched_at=datetime.now(timezone.utc), status="unavailable",
                notes=["指数看板读取失败，请稍后刷新。"],
                items=[IndexQuoteItem(name=name, code=code, status="unavailable")
                       for name, code in INDEXES],
            )

    async def aclose(self) -> None:
        tasks = [task for _, task in self._entries.values()]
        self._entries.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
