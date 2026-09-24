"""自动化真实数据闭环：意图路由、并行取数、事实合并与可审计派生。"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import NAMESPACE_URL, uuid5

from backend.app.models import (
    DataAcquisitionResult,
    FactRecord,
    Intent,
    OrchestrationRequest,
    ResearchCapability,
)
from backend.app.fact_taxonomy import fact_is_current, fact_max_age_seconds


@dataclass(frozen=True)
class DataCall:
    """一次受限的数据能力调用；label 用于审计，不包含密钥或请求头。"""

    label: str
    method: str
    args: tuple[Any, ...]
    # 复用键：同一能力 + 同一研究目标视为"同一次取数"。它在服务端由语义层抽取的
    # 研究对象生成，与发给数据源的查询文本无关，因此换一种问法仍能识别出"这份
    # 资料已经取过"。
    key: str


# 只带查询摘要、没有任何业务字段的调用不算"已经取到资料"：这类返回不是结果，
# 而是数据源的一次空手而归，下一轮必须重试。
NON_SUBSTANTIVE_FIELDS = frozenset({"provider_response"})

# 语义模型只能选择业务能力枚举；具体方法名由服务端固定映射，绝不接受模型
# 输出的方法名、URL 或任意参数。这样具备工具选择能力，同时保留只读安全边界。
CAPABILITY_METHODS: dict[ResearchCapability, str] = {
    ResearchCapability.QUOTE: "get_quote",
    ResearchCapability.FINANCIAL: "get_financial_metrics",
    ResearchCapability.BASIC_INFO: "get_basic_info",
    ResearchCapability.COMPANY_OPERATIONS: "get_company_operations",
    ResearchCapability.SHAREHOLDER_EQUITY: "get_shareholder_equity",
    ResearchCapability.EVENT: "get_event_data",
    ResearchCapability.MACRO: "get_macro_data",
    ResearchCapability.INSTITUTIONAL_RESEARCH: "get_institutional_research",
    ResearchCapability.NEWS: "get_news",
    ResearchCapability.RESEARCH_REPORT: "get_research_reports",
    ResearchCapability.ANNOUNCEMENT: "get_announcements",
    ResearchCapability.STOCK_SCREEN: "screen_stocks",
    ResearchCapability.SECTOR_SCREEN: "screen_sectors",
    ResearchCapability.FUND: "get_fund_candidates",
    ResearchCapability.INDUSTRY: "get_industry_rank",
    ResearchCapability.CONVERTIBLE: "get_convertible_bond",
}


def _target_key(target: str | None, query: str) -> str:
    """生成复用键里的研究对象标识。

    优先使用语义层抽取的对象；抽取不到时退化为整句查询。退化意味着"换一种问法
    就会重新取数"——这是刻意的保守行为：没有可靠的对象标识时，不能假定上一轮的
    资料仍然适用于新问题。
    """

    return re.sub(r"\s+", "", (target or query or "").strip()).casefold()


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
        *,
        target: str | None = None,
        data_requirements: list[ResearchCapability] | None = None,
    ) -> tuple[OrchestrationRequest, DataAcquisitionResult]:
        """返回补齐事实后的请求以及不泄露底层异常的取数审计摘要。

        ``target`` 是语义层抽取的研究对象，仅用于判断"同一份资料本轮是否已经取过"。
        真正发给数据源的仍是用户原话：问财接受自然语言查询，改写它反而会改变结果。
        """

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

        calls = self._calls_for(request, intent, target, data_requirements)
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

        # 追问时最容易浪费的一段：同一目标的资料通常几分钟前才取过。这里先判断
        # 哪些能力可以直接沿用，只对真正缺资料的能力发起外部调用。
        reusable, pending = self._split_reusable(calls, base_facts, self.now())
        reused = [call.label for call in reusable]
        results = await asyncio.gather(
            *(self._execute(call) for call in pending),
            return_exceptions=True,
        )
        fetched: list[FactRecord] = []
        successful: list[str] = []
        empty: list[str] = []
        failed: list[str] = []
        for call, result in zip(pending, results, strict=True):
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
        elif reused:
            # 全部计划内的能力都沿用了既有资料：这不是"取数失败"，不能按不可用上报，
            # 否则界面会告诉用户"未取得最新数据"，把一次正常复用说成故障。
            mode = "reused"
            message = (
                "本次沿用仍在有效期内的已授权资料，未重复调用外部数据源。"
                if not (failed or empty)
                # 有沿用也有没取到的（例如数据源熔断）：不能只说"没重复调用"，
                # 那会让一次残缺的取数看起来完全正常。
                else "部分资料沿用了有效期内的已有资料，另有能力本次未能取得。"
            )
        else:
            mode = "unavailable"
            message = "外部数据未返回可用事实，分析已按现有事实安全降级。"

        prepared = request.model_copy(update={"facts": all_facts})
        return prepared, DataAcquisitionResult(
            mode=mode,
            provider=getattr(self.provider, "source_id", type(self.provider).__name__),
            requested_capabilities=requested,
            successful_capabilities=successful,
            reused_capabilities=reused,
            empty_capabilities=empty,
            failed_capabilities=failed,
            supplied_fact_count=len(supplied),
            fetched_fact_count=len(fetched),
            derived_fact_count=len(portfolio_facts) + len(derived),
            message=message,
        )

    async def _execute(self, call: DataCall) -> list[FactRecord]:
        method = getattr(self.provider, call.method)
        facts = await method(*call.args)
        # 打上来源调用键：下一轮针对同一目标再提问时，据此判断能否直接沿用。
        return [fact.model_copy(update={"produced_by": call.key}) for fact in facts]

    def _split_reusable(
        self,
        calls: list[DataCall],
        facts: list[FactRecord],
        now: datetime,
    ) -> tuple[list[DataCall], list[DataCall]]:
        """把计划拆成"沿用已有资料"与"需要真正取数"两组。

        判据是"这次能力上一轮取回的事实，是否**全部**仍在各自时效内"。它刻意不维护
        "哪些字段才算够用"的清单，原因有二：

        * 数据源每次返回的字段并不稳定（同一次宏观查询，有时给 pmi、有时不给），
          按字段清单判断会让一次缺字段就永远无法复用；
        * 逐字段判断需要人工维护映射，一旦与实际返回不符，就会出现"少取了资料却
          没人发现"的隐性损失，而这里恰恰是最不该出错的地方。

        只沿用整批仍有效的事实，等价于"不重取也能拿到同样这批数据"，因此跳过取数
        永远不会让结论比不跳过时更差。
        """

        reusable: list[DataCall] = []
        pending: list[DataCall] = []
        for call in calls:
            if self._covers(call, facts, now):
                reusable.append(call)
            else:
                pending.append(call)
        return reusable, pending

    @staticmethod
    def _covers(call: DataCall, facts: list[FactRecord], now: datetime) -> bool:
        """这次能力上一轮取回的事实是否仍然整批可用。

        供应商每次返回的 ``fact_id`` 都是新的，同一字段会随着多轮追问不断累积历史
        版本。因此判据取**每个字段的最新一版**：只要最新一版仍然有效，就说明这次
        能力该拿的字段当前都拿得到；旧版本过期不应永久作废复用。反过来，某个字段
        只剩过期版本时，仍按"需要重取"处理。
        """

        latest: dict[str, FactRecord] = {}
        for fact in facts:
            if fact.produced_by != call.key:
                continue
            field = fact.field.casefold()
            current = latest.get(field)
            if current is None or (fact.snapshot_time, fact.quality) > (current.snapshot_time, current.quality):
                latest[field] = fact
        if not any(field not in NON_SUBSTANTIVE_FIELDS for field in latest):
            return False
        return all(fact_is_current(fact, now) for fact in latest.values())

    @staticmethod
    def _research_scope(request: OrchestrationRequest, intent: Intent, target: str | None) -> str:
        """本次取数的研究对象标识，用作复用键。

        组合诊断的"研究对象"就是持仓本身，而持仓在本地是确定的，不必依赖模型抽取：
        用持仓集合做键，既不会随问法变化，也能在用户真的改了持仓时自动重新取数。
        其余情况优先用语义层抽取的对象；抽取不到时退化为整句查询，也就是"换一种
        问法就重新取数"——没有可靠的对象标识时，这是唯一安全的退路。
        """

        if intent is Intent.PORTFOLIO_REVIEW:
            entities = sorted(
                _holding_entity(holding) for holding in request.portfolio if isinstance(holding, dict)
            )
            held = [entity for entity in entities if entity]
            if held:
                return f"portfolio:{','.join(held)}"
        return _target_key(target, request.query)

    def _calls_for(
        self,
        request: OrchestrationRequest,
        intent: Intent,
        target: str | None = None,
        data_requirements: list[ResearchCapability] | None = None,
    ) -> list[DataCall]:
        query_text = request.query
        research_target = self._research_scope(request, intent, target)
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
            # args 仍然是用户原话（问财接受自然语言查询）；key 才用抽取出的研究对象，
            # 这样"换一种问法问同一只票"能被识别为同一次取数。
            DataCall(label=name, method=method, args=(query_text,), key=f"{method}@{research_target}")
            for name, method in routes[intent]
        ]
        if intent is Intent.FUND_SCREENING:
            filters = {
                "query": request.query,
                "risk_level": request.profile.risk_level or "未指定",
            }
            calls.append(DataCall(label="fund", method="get_fund_candidates", args=(filters,),
                                  key=f"get_fund_candidates@{research_target}"))
        if intent is Intent.PORTFOLIO_REVIEW:
            for index, holding in enumerate(request.portfolio[: self.max_portfolio_entities], start=1):
                if not isinstance(holding, dict):
                    continue
                entity = _holding_entity(holding)
                if not entity:
                    continue
                # 持仓的取数目标就是持仓名，跨轮天然稳定。
                holding_key = _target_key(entity, entity)
                calls.extend(
                    (
                        DataCall(label=f"quote:{index}:{entity}", method="get_quote", args=(entity,),
                                 key=f"get_quote@{holding_key}"),
                        DataCall(label=f"financial:{index}:{entity}", method="get_financial_metrics",
                                 args=(entity,), key=f"get_financial_metrics@{holding_key}"),
                    )
                )

        # 固定意图路由保证专业节点的基础证据不缩水；语义模型只负责补充本轮问题
        # 明确需要的能力（例如公告、主营构成或股东变化）。后端按方法去重，随后
        # _split_reusable 会只执行从未取过或已经过期的调用。
        planned_methods = {call.method for call in calls}
        for raw_capability in data_requirements or []:
            try:
                capability = ResearchCapability(raw_capability)
            except ValueError:
                # 正常 API 路径已由 Pydantic 拦截；这里同时保护直接 Python 调用。
                continue
            method = CAPABILITY_METHODS[capability]
            if method in planned_methods:
                continue
            calls.append(
                self._capability_call(
                    capability,
                    request,
                    research_target=research_target,
                )
            )
            planned_methods.add(method)
        return calls

    @staticmethod
    def _capability_call(
        capability: ResearchCapability,
        request: OrchestrationRequest,
        *,
        research_target: str,
    ) -> DataCall:
        """把模型选择的白名单能力转换成后端控制的只读调用。"""

        method = CAPABILITY_METHODS[capability]
        if capability is ResearchCapability.FUND:
            filters = {
                "query": request.query,
                "risk_level": request.profile.risk_level or "未指定",
            }
            args: tuple[Any, ...] = (filters,)
        else:
            args = (request.query,)
        return DataCall(
            label=capability.value,
            method=method,
            args=args,
            key=f"{method}@{research_target}",
        )

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
