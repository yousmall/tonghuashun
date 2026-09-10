"""自动化真实数据闭环：意图路由、并行取数、事实合并与可审计派生。"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import NAMESPACE_URL, uuid5

from backend.app.models import DataAcquisitionResult, FactRecord, Intent, OrchestrationRequest
from backend.app.fact_taxonomy import fact_max_age_seconds


@dataclass(frozen=True)
class DataCall:
    """一次受限的数据能力调用；label 用于审计，不包含密钥或请求头。"""

    label: str
    method: str
    args: tuple[Any, ...]


class AutomatedResearchPipeline:
    """把无事实/部分事实请求补齐为协调器可直接消费的事实包。"""

    def __init__(
        self,
        provider: Any | None,
        *,
        now: Callable[[], datetime] | None = None,
        max_portfolio_entities: int = 4,
    ) -> None:
        self.provider = provider
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.max_portfolio_entities = max_portfolio_entities

    async def prepare(
        self,
        request: OrchestrationRequest,
        intent: Intent,
    ) -> tuple[OrchestrationRequest, DataAcquisitionResult]:
        """返回补齐事实后的请求以及不泄露底层异常的取数审计摘要。"""

        supplied = [fact.model_copy(deep=True) for fact in request.facts]
        portfolio_facts = self._portfolio_facts(request)
        base_facts = _merge_by_fact_id([*supplied, *portfolio_facts])

        if not request.auto_fetch:
            derived = derive_scoring_facts(base_facts, now=self.now())
            prepared = request.model_copy(update={"facts": _merge_by_fact_id([*base_facts, *derived])})
            return prepared, DataAcquisitionResult(
                mode="provided",
                supplied_fact_count=len(supplied),
                derived_fact_count=len(portfolio_facts) + len(derived),
                message="自动取数已由调用方关闭，仅使用显式事实和持仓快照。",
            )

        if not request.profile.confirmed or intent in {Intent.UNKNOWN, Intent.EDUCATION}:
            prepared = request.model_copy(update={"facts": base_facts})
            return prepared, DataAcquisitionResult(
                mode="not_required",
                supplied_fact_count=len(supplied),
                derived_fact_count=len(portfolio_facts),
                message="当前请求尚未通过画像/意图闸门，未调用外部数据源。",
            )

        calls = self._calls_for(request, intent)
        requested = [call.label for call in calls]
        if self.provider is None:
            derived = derive_scoring_facts(base_facts, now=self.now())
            prepared = request.model_copy(update={"facts": _merge_by_fact_id([*base_facts, *derived])})
            return prepared, DataAcquisitionResult(
                mode="unavailable",
                requested_capabilities=requested,
                supplied_fact_count=len(supplied),
                derived_fact_count=len(portfolio_facts) + len(derived),
                message="未配置问财只读密钥，已仅使用调用方事实和持仓快照。",
            )

        results = await asyncio.gather(
            *(self._execute(call) for call in calls),
            return_exceptions=True,
        )
        fetched: list[FactRecord] = []
        successful: list[str] = []
        empty: list[str] = []
        failed: list[str] = []
        for call, result in zip(calls, results, strict=True):
            if isinstance(result, BaseException):
                failed.append(call.label)
            elif result:
                successful.append(call.label)
                fetched.extend(result)
            else:
                empty.append(call.label)

        source_facts = _merge_by_fact_id([*base_facts, *fetched])
        derived = derive_scoring_facts(source_facts, now=self.now())
        all_facts = _merge_by_fact_id([*source_facts, *derived])
        if fetched:
            mode = "mixed" if base_facts else "live"
            message = "真实数据已自动获取并进入事实核验、专业分析和合规流程。"
        else:
            mode = "unavailable"
            message = "外部数据未返回可用事实，分析已按现有事实安全降级。"

        prepared = request.model_copy(update={"facts": all_facts})
        return prepared, DataAcquisitionResult(
            mode=mode,
            provider=getattr(self.provider, "source_id", type(self.provider).__name__),
            requested_capabilities=requested,
            successful_capabilities=successful,
            empty_capabilities=empty,
            failed_capabilities=failed,
            supplied_fact_count=len(supplied),
            fetched_fact_count=len(fetched),
            derived_fact_count=len(portfolio_facts) + len(derived),
            message=message,
        )

    async def _execute(self, call: DataCall) -> list[FactRecord]:
        method = getattr(self.provider, call.method)
        return await method(*call.args)

    def _calls_for(self, request: OrchestrationRequest, intent: Intent) -> list[DataCall]:
        target = request.query
        routes: dict[Intent, tuple[tuple[str, str], ...]] = {
            Intent.MARKET_ANALYSIS: (
                ("macro", "get_macro_data"),
                ("industry", "get_industry_rank"),
                ("news", "get_news"),
            ),
            Intent.INDUSTRY_ANALYSIS: (
                ("industry", "get_industry_rank"),
                ("news", "get_news"),
            ),
            Intent.SECURITY_RESEARCH: (
                # 个股研究同时计划 market/industry/security 三个专业节点，因此必须把
                # 宏观与行业数据一并取回；否则这两个节点必然因缺字段降级，只留下
                # 个股一个观点，还会被一致性检查误判为"跨智能体分歧"。
                ("macro", "get_macro_data"),
                ("industry", "get_industry_rank"),
                ("quote", "get_quote"),
                ("financial", "get_financial_metrics"),
                ("event", "get_event_data"),
                ("institutional_research", "get_institutional_research"),
            ),
            Intent.CONVERTIBLE_BOND_ANALYSIS: (
                ("convertible", "get_convertible_bond"),
                ("news", "get_news"),
            ),
            Intent.FUND_SCREENING: (),
            Intent.PORTFOLIO_REVIEW: (
                ("macro", "get_macro_data"),
                ("industry", "get_industry_rank"),
            ),
            Intent.EDUCATION: (),
            Intent.UNKNOWN: (),
        }
        calls = [
            DataCall(label=name, method=method, args=(target,))
            for name, method in routes[intent]
        ]
        if intent is Intent.FUND_SCREENING:
            filters = {
                "query": request.query,
                "risk_level": request.profile.risk_level or "未指定",
            }
            calls.append(DataCall(label="fund", method="get_fund_candidates", args=(filters,)))
        if intent is Intent.PORTFOLIO_REVIEW:
            for index, holding in enumerate(request.portfolio[: self.max_portfolio_entities], start=1):
                if not isinstance(holding, dict):
                    continue
                entity = _holding_entity(holding)
                if not entity:
                    continue
                calls.extend(
                    (
                        DataCall(label=f"quote:{index}:{entity}", method="get_quote", args=(entity,)),
                        DataCall(label=f"financial:{index}:{entity}", method="get_financial_metrics", args=(entity,)),
                    )
                )
        return calls

    def _portfolio_facts(self, request: OrchestrationRequest) -> list[FactRecord]:
        facts: list[FactRecord] = []
        snapshot_time = self.now()
        existing = {
            (fact.entity.casefold(), fact.field.casefold())
            for fact in request.facts
            if fact.field.casefold() in {"weight", "portfolio_weight"}
        }
        for index, holding in enumerate(request.portfolio, start=1):
            if not isinstance(holding, dict):
                continue
            entity = _holding_entity(holding)
            weight = _number(holding.get("weight"))
            if not entity or weight is None or not 0 <= weight <= 1:
                continue
            if (entity.casefold(), "weight") in existing:
                continue
            stable_id = uuid5(NAMESPACE_URL, f"portfolio:{request.profile.user_id}:{index}:{entity}:{weight}")
            facts.append(
                FactRecord(
                    fact_id=f"PORTFOLIO-{stable_id.hex[:16].upper()}",
                    entity=entity,
                    field="weight",
                    value=weight,
                    snapshot_time=snapshot_time,
                    source_id="USER_PORTFOLIO_SNAPSHOT",
                    quality=1.0,
                )
            )
        return facts


def derive_scoring_facts(facts: list[FactRecord], *, now: datetime) -> list[FactRecord]:
    """用公开、固定公式派生规则智能体评分；缺输入就不生成，不填中性默认值。"""

    grouped: dict[str, list[FactRecord]] = defaultdict(list)
    for fact in facts:
        grouped[fact.entity].append(fact)
    derived: list[FactRecord] = []
    for entity, entity_facts in grouped.items():
        by_field: dict[str, list[FactRecord]] = defaultdict(list)
        for fact in entity_facts:
            by_field[fact.field.casefold()].append(fact)
        existing = set(by_field)

        fundamental_inputs: list[tuple[FactRecord, float]] = []
        for field, multiplier in (("roe", 1.5), ("revenue_growth", 1.0)):
            source = _latest_numeric(by_field.get(field, []), now)
            if source:
                fundamental_inputs.append((source[0], _clamp(50 + _as_percent(source[1]) * multiplier)))
        _append_score(derived, existing, entity, "fundamental_score", fundamental_inputs, now)

        valuation_inputs: list[tuple[FactRecord, float]] = []
        pe = _latest_numeric(by_field.get("pe_ttm", []), now)
        if pe and pe[1] > 0:
            valuation_inputs.append((pe[0], _clamp(100 - pe[1] * 2)))
        pb = _latest_numeric(by_field.get("pb", []), now)
        if pb and pb[1] > 0:
            valuation_inputs.append((pb[0], _clamp(100 - pb[1] * 12)))
        _append_score(derived, existing, entity, "valuation_score", valuation_inputs, now)

        change = _latest_numeric(by_field.get("change", []), now)
        if change:
            _append_score(
                derived,
                existing,
                entity,
                "technical_score",
                [(change[0], _clamp(50 + _as_percent(change[1]) * 3))],
                now,
            )

        pmi = _latest_numeric(by_field.get("pmi", []), now)
        if pmi:
            _append_score(
                derived,
                existing,
                entity,
                "growth_score",
                [(pmi[0], _clamp(50 + (pmi[1] - 50) * 5))],
                now,
            )
        inflation_inputs: list[tuple[FactRecord, float]] = []
        for field in ("cpi", "ppi"):
            source = _latest_numeric(by_field.get(field, []), now)
            if source:
                value = _as_percent(source[1])
                inflation_inputs.append((source[0], _clamp(100 - abs(value - 2) * 12)))
        _append_score(derived, existing, entity, "inflation_score", inflation_inputs, now)

        rate = _latest_numeric(by_field.get("interest_rate", []), now)
        if rate:
            _append_score(
                derived,
                existing,
                entity,
                "liquidity_score",
                [(rate[0], _clamp(75 - _as_percent(rate[1]) * 6))],
                now,
            )

        fund_inputs: list[tuple[FactRecord, float]] = []
        fee = _latest_numeric(by_field.get("fee_rate", []), now)
        if fee:
            fund_inputs.append((fee[0], _clamp(100 - _as_percent(fee[1]) * 20)))
        tracking = _latest_numeric(by_field.get("tracking_error", []), now)
        if tracking:
            fund_inputs.append((tracking[0], _clamp(100 - _as_percent(tracking[1]) * 20)))
        _append_score(derived, existing, entity, "fund_score", fund_inputs, now)
    return derived


def _append_score(
    output: list[FactRecord],
    existing: set[str],
    entity: str,
    field: str,
    inputs: list[tuple[FactRecord, float]],
    now: datetime,
) -> None:
    if field in existing or not inputs:
        return
    parents = sorted({fact.fact_id for fact, _ in inputs})
    value = round(sum(score for _, score in inputs) / len(inputs), 2)
    stable_id = uuid5(NAMESPACE_URL, f"derived:v1:{entity}:{field}:{'|'.join(parents)}")
    output.append(
        FactRecord(
            fact_id=f"DERIVED-{stable_id.hex[:16].upper()}",
            entity=entity,
            field=field,
            value=value,
            snapshot_time=now,
            source_id="DERIVED_RULE_V1",
            quality=round(min(fact.quality for fact, _ in inputs) * 0.9, 2),
            period=_common_period([fact for fact, _ in inputs]),
            derived_from=parents,
        )
    )
    existing.add(field)


def _latest_numeric(facts: list[FactRecord], now: datetime) -> tuple[FactRecord, float] | None:
    candidates = [
        (fact, _number(fact.value))
        for fact in facts
        if (
            now - timedelta(seconds=fact_max_age_seconds(fact))
            <= fact.snapshot_time
            <= now + timedelta(minutes=5)
            and fact.quality >= 0.4
            and fact.source_id.strip()
        )
    ]
    usable = [(fact, value) for fact, value in candidates if value is not None]
    if not usable:
        return None
    return max(usable, key=lambda item: item[0].snapshot_time)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", value)
        if match:
            try:
                return float(match.group(0).replace(",", ""))
            except ValueError:
                return None
    return None


def _as_percent(value: float) -> float:
    return value * 100 if -1 < value < 1 else value


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def _holding_entity(holding: dict[str, Any]) -> str:
    for key in ("symbol", "code", "name", "entity"):
        value = holding.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _merge_by_fact_id(facts: list[FactRecord]) -> list[FactRecord]:
    merged: dict[str, FactRecord] = {}
    for fact in facts:
        current = merged.get(fact.fact_id)
        if current is None or (fact.quality, fact.snapshot_time) > (current.quality, current.snapshot_time):
            merged[fact.fact_id] = fact
    return list(merged.values())


def _common_period(facts: list[FactRecord]) -> str | None:
    periods = {fact.period for fact in facts if fact.period}
    return periods.pop() if len(periods) == 1 else None
