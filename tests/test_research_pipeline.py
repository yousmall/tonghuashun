"""自动取数闭环测试：意图路由、并行数据补齐、派生血缘和 API 回传。"""

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import backend.app.main as main_module
from backend.app.models import FactRecord, Intent, OrchestrationRequest
from backend.app.services import AutomatedResearchPipeline
from backend.app.services import research as research_module


class FakeIwencaiProvider:
    """只返回确定性样本，不访问网络；调用记录用于验证最小能力路由。"""

    source_id = "IWENCAI_TEST"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._serial = 0

    def _fact(self, suffix: str, field: str, value: object, *, entity: str = "示例科技") -> FactRecord:
        # 真实供应商每次返回的 fact_id 都是新的（iwencai.py 用 uuid4），这里同样
        # 保证唯一，否则不同持仓的事实会因为 ID 相同被误当成同一条而合并。
        self._serial += 1
        return FactRecord(
            fact_id=f"LIVE-{suffix}-{self._serial}",
            entity=entity,
            field=field,
            value=value,
            snapshot_time=datetime.now(timezone.utc),
            source_id=self.source_id,
            quality=0.9,
            period="2026Q2" if field in {"roe", "revenue_growth", "pe_ttm", "pb"} else None,
        )

    async def get_quote(self, target: str) -> list[FactRecord]:
        self.calls.append("quote")
        return [
            self._fact("PRICE", "close_price", 25.6),
            self._fact("CHANGE", "change", 0.02),
        ]

    async def get_financial_metrics(self, target: str) -> list[FactRecord]:
        self.calls.append("financial")
        return [
            self._fact("ROE", "roe", 0.16),
            self._fact("GROWTH", "revenue_growth", 0.12),
            self._fact("PE", "pe_ttm", 20),
            self._fact("PB", "pb", 2),
        ]

    async def get_event_data(self, target: str) -> list[FactRecord]:
        self.calls.append("event")
        return [self._fact("EVENT", "event_title", "测试事件")]

    async def get_institutional_research(self, target: str) -> list[FactRecord]:
        self.calls.append("institutional_research")
        return [self._fact("RATING", "rating", "增持")]

    async def get_macro_data(self, target: str) -> list[FactRecord]:
        # 个股研究也计划 market 节点，因此这里必须提供完整五个宏观维度。
        # 实体名与个股不同，避免把宏观分项误当成个股事实。
        self.calls.append("macro")
        return [
            self._fact(f"MACRO-{field}", field, 55, entity="A股市场")
            for field in ("growth_score", "inflation_score", "liquidity_score",
                          "policy_score", "risk_appetite_score")
        ]

    async def get_industry_rank(self, target: str) -> list[FactRecord]:
        # 同理，industry 节点需要完整五个行业维度；行业估值分与个股估值分
        # 必须是不同实体，否则 industry 的分项会被当成个股估值。
        self.calls.append("industry")
        return [
            self._fact(f"IND-{field}", field, 58, entity="示例行业")
            for field in ("prosperity_score", "valuation_score", "capital_flow_score",
                          "crowding_score", "policy_score")
        ]


def request_for_security(*, auto_fetch: bool = True) -> OrchestrationRequest:
    return OrchestrationRequest(
        query="请研究示例科技这只个股",
        profile={"user_id": "u-auto", "risk_level": "R3", "confirmed": True},
        auto_fetch=auto_fetch,
    )


def test_pipeline_routes_live_data_and_preserves_derivation_lineage() -> None:
    provider = FakeIwencaiProvider()
    pipeline = AutomatedResearchPipeline(provider)

    prepared, audit = asyncio.run(
        pipeline.prepare(request_for_security(), Intent.SECURITY_RESEARCH)
    )

    assert audit.mode == "live"
    assert audit.provider == provider.source_id
    # 个股研究同时计划 market/industry/security，取数必须覆盖这三个节点的输入维度。
    assert set(provider.calls) == {
        "macro", "industry", "quote", "financial", "event", "institutional_research",
    }
    assert audit.fetched_fact_count == 18
    derived = [fact for fact in prepared.facts if fact.source_id == "DERIVED_RULE_V1"]
    assert {fact.field for fact in derived} == {
        "fundamental_score",
        "valuation_score",
        "technical_score",
    }
    # 派生只作用于被研究的个股实体，不得把宏观/行业快照也算成它的派生结果。
    assert {fact.entity for fact in derived} == {"示例科技"}
    assert all(fact.derived_from for fact in derived)
    assert all(parent.startswith("LIVE-") for fact in derived for parent in fact.derived_from)


def test_pipeline_does_not_refresh_scores_from_stale_inputs() -> None:
    stale = FactRecord(
        fact_id="STALE-ROE",
        entity="示例科技",
        field="roe",
        value=0.2,
        snapshot_time=datetime.now(timezone.utc) - timedelta(days=100),
        source_id="OLD_REPORT",
        quality=0.9,
    )
    request = request_for_security(auto_fetch=False).model_copy(update={"facts": [stale]})

    prepared, audit = asyncio.run(
        AutomatedResearchPipeline(None).prepare(request, Intent.SECURITY_RESEARCH)
    )

    assert audit.mode == "provided"
    assert not any(fact.field == "fundamental_score" for fact in prepared.facts)


def test_pipeline_respects_explicit_auto_fetch_opt_out() -> None:
    provider = FakeIwencaiProvider()

    prepared, audit = asyncio.run(
        AutomatedResearchPipeline(provider).prepare(
            request_for_security(auto_fetch=False),
            Intent.SECURITY_RESEARCH,
        )
    )

    assert audit.mode == "provided"
    assert provider.calls == []
    assert prepared.facts == []


def test_analyze_endpoint_returns_fetched_and_derived_facts(monkeypatch) -> None:
    provider = FakeIwencaiProvider()
    monkeypatch.setattr(
        main_module,
        "research_pipeline",
        AutomatedResearchPipeline(provider),
    )
    client = TestClient(main_module.app)

    response = client.post(
        "/api/v1/portfolio/analyze",
        json=request_for_security().model_dump(mode="json"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["data_acquisition"]["mode"] == "live"
    assert body["data_acquisition"]["fetched_fact_count"] == 18
    assert any(fact["source_id"] == "IWENCAI_TEST" for fact in body["facts"])
    assert any(fact["source_id"] == "DERIVED_RULE_V1" for fact in body["facts"])
    assert set(body["evidence"]).issubset({fact["fact_id"] for fact in body["facts"]})
    # 来源调用键必须走通 API：浏览器要靠它把"这份资料是哪次取回的"带回下一轮，
    # 序列化时丢掉这个字段会让复用永远失效，而且不会报错。
    assert any(fact.get("produced_by") for fact in body["facts"])


# --------------------------------------------------------------------------- #
# 取数复用：同一研究对象的资料还整批有效时，不再重复调用外部数据源
# --------------------------------------------------------------------------- #
TARGET = "示例科技"


def follow_up(facts: list[FactRecord], *, query: str = "那它的负债情况呢") -> OrchestrationRequest:
    """模拟同一会话的第二轮：换了问法，但研究对象没变，且带回了上一轮的资料。"""

    return request_for_security().model_copy(update={"query": query, "facts": facts})


def fetch_once(provider: FakeIwencaiProvider) -> tuple[OrchestrationRequest, object]:
    pipeline = AutomatedResearchPipeline(provider)
    return asyncio.run(
        pipeline.prepare(request_for_security(), Intent.SECURITY_RESEARCH, target=TARGET)
    )


class SummaryOnlyProvider(FakeIwencaiProvider):
    """事件查询只带回一条查询摘要，没有任何业务字段。"""

    async def get_event_data(self, target: str) -> list[FactRecord]:
        self.calls.append("event")
        return [self._fact("SUMMARY", "provider_response", "示例科技近期事件")]


def test_follow_up_reuses_fresh_facts_instead_of_refetching() -> None:
    """资料还整批有效时，换一种问法问同一只票不该再取一遍。"""

    provider = FakeIwencaiProvider()
    first, _ = fetch_once(provider)
    provider.calls.clear()

    pipeline = AutomatedResearchPipeline(provider)
    prepared, audit = asyncio.run(
        pipeline.prepare(follow_up(first.facts), Intent.SECURITY_RESEARCH, target=TARGET)
    )

    assert provider.calls == []
    assert audit.mode == "reused"
    assert len(audit.reused_capabilities) == 6
    assert audit.fetched_fact_count == 0
    # 沿用的资料必须仍然出现在本次事实包里，否则等于把资料弄丢了。
    assert {fact.fact_id for fact in first.facts} <= {fact.fact_id for fact in prepared.facts}


def test_expired_facts_are_refetched_per_their_own_expiry() -> None:
    """行情 60 秒、财务 90 天：过期的那一类重取，没过期的照旧沿用。"""

    provider = FakeIwencaiProvider()
    first, _ = fetch_once(provider)
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    aged = [
        fact.model_copy(update={"snapshot_time": stale_time})
        if fact.field in {"close_price", "change"}
        else fact
        for fact in first.facts
    ]
    provider.calls.clear()

    pipeline = AutomatedResearchPipeline(provider)
    _, audit = asyncio.run(
        pipeline.prepare(follow_up(aged), Intent.SECURITY_RESEARCH, target=TARGET)
    )

    assert provider.calls == ["quote"]  # 5 分钟前的价格早已越过 60 秒窗口
    assert audit.reused_capabilities == [
        "macro", "industry", "financial", "event", "institutional_research",
    ]
    assert audit.mode == "mixed"


def test_empty_handed_call_is_retried_instead_of_reused() -> None:
    """只带回查询摘要、没有业务字段的调用不算"已经取到资料"，必须重试。"""

    provider = SummaryOnlyProvider()
    first, _ = fetch_once(provider)
    provider.calls.clear()

    pipeline = AutomatedResearchPipeline(provider)
    _, audit = asyncio.run(
        pipeline.prepare(follow_up(first.facts), Intent.SECURITY_RESEARCH, target=TARGET)
    )

    assert provider.calls == ["event"]
    assert "event" not in audit.reused_capabilities


def test_reuse_alongside_failures_is_not_reported_as_clean_reuse() -> None:
    """有沿用也有取不到时，审计文案不能只说"未重复调用"，那会掩盖残缺的取数。"""

    class FailingIndustryProvider(FakeIwencaiProvider):
        async def get_industry_rank(self, target: str) -> list[FactRecord]:
            self.calls.append("industry")
            raise RuntimeError("问财数据源熔断中，请稍后重试")

    provider = FailingIndustryProvider()
    first, first_audit = fetch_once(provider)
    assert first_audit.failed_capabilities == ["industry"]  # 首轮就没取到行业资料
    provider.calls.clear()

    pipeline = AutomatedResearchPipeline(provider)
    _, audit = asyncio.run(
        pipeline.prepare(follow_up(first.facts), Intent.SECURITY_RESEARCH, target=TARGET)
    )

    assert audit.failed_capabilities == ["industry"]
    assert audit.reused_capabilities == ["macro", "quote", "financial", "event", "institutional_research"]
    assert "未能取得" in (audit.message or "")


def test_facts_without_matching_provenance_do_not_enable_reuse() -> None:
    """字段名相同不代表同一次取数：没有来源调用标记就只能重新取。"""

    provider = FakeIwencaiProvider()
    first, _ = fetch_once(provider)
    unattributed = [fact.model_copy(update={"produced_by": None}) for fact in first.facts]
    provider.calls.clear()

    pipeline = AutomatedResearchPipeline(provider)
    _, audit = asyncio.run(
        pipeline.prepare(follow_up(unattributed), Intent.SECURITY_RESEARCH, target=TARGET)
    )

    assert provider.calls == [
        "macro", "industry", "quote", "financial", "event", "institutional_research",
    ]
    assert audit.reused_capabilities == []


def test_without_extracted_target_a_reworded_question_refetches() -> None:
    """抽不到研究对象时退回按原话匹配——换问法就重新取数，这是刻意的保守行为。"""

    provider = FakeIwencaiProvider()
    pipeline = AutomatedResearchPipeline(provider)
    first, _ = asyncio.run(
        pipeline.prepare(request_for_security(), Intent.SECURITY_RESEARCH)
    )
    provider.calls.clear()

    _, audit = asyncio.run(
        pipeline.prepare(follow_up(first.facts), Intent.SECURITY_RESEARCH)
    )

    assert provider.calls == [
        "macro", "industry", "quote", "financial", "event", "institutional_research",
    ]
    assert audit.reused_capabilities == []


def test_without_extracted_target_a_repeated_question_still_reuses() -> None:
    """没有研究对象时，原样再问一次仍然能沿用——退路不是"一律重取"。"""

    provider = FakeIwencaiProvider()
    pipeline = AutomatedResearchPipeline(provider)
    first, _ = asyncio.run(
        pipeline.prepare(request_for_security(), Intent.SECURITY_RESEARCH)
    )
    provider.calls.clear()

    _, audit = asyncio.run(
        pipeline.prepare(
            request_for_security().model_copy(update={"facts": first.facts}),
            Intent.SECURITY_RESEARCH,
        )
    )

    assert provider.calls == []
    assert audit.mode == "reused"


def test_portfolio_holdings_are_reused_across_turns() -> None:
    """组合诊断的研究对象是持仓本身：换了问法也应整批复用，不必依赖模型抽取目标。"""

    provider = FakeIwencaiProvider()
    pipeline = AutomatedResearchPipeline(provider)
    portfolio_request = OrchestrationRequest(
        query="请帮我看看这些持仓放在一起是否合适",
        profile={"user_id": "u-auto", "risk_level": "R3", "confirmed": True},
        portfolio=[{"name": "示例科技", "weight": 0.5}, {"name": "另一只基金", "weight": 0.3}],
    )
    first, first_audit = asyncio.run(
        pipeline.prepare(portfolio_request, Intent.PORTFOLIO_REVIEW)
    )
    assert len(first_audit.requested_capabilities) == 6  # macro + industry + 2 只 × 2 项
    provider.calls.clear()

    _, audit = asyncio.run(
        pipeline.prepare(
            portfolio_request.model_copy(update={"query": "要不要减仓？", "facts": first.facts}),
            Intent.PORTFOLIO_REVIEW,
        )
    )

    assert provider.calls == []
    assert audit.mode == "reused"
    assert len(audit.reused_capabilities) == 6


def test_changing_the_portfolio_invalidates_the_reuse_scope() -> None:
    """持仓变了就是新的研究对象，不能拿旧持仓的上下文资料继续用。"""

    provider = FakeIwencaiProvider()
    pipeline = AutomatedResearchPipeline(provider)
    portfolio_request = OrchestrationRequest(
        query="请帮我看看这些持仓放在一起是否合适",
        profile={"user_id": "u-auto", "risk_level": "R3", "confirmed": True},
        portfolio=[{"name": "示例科技", "weight": 0.5}],
    )
    first, _ = asyncio.run(pipeline.prepare(portfolio_request, Intent.PORTFOLIO_REVIEW))
    provider.calls.clear()

    _, audit = asyncio.run(
        pipeline.prepare(
            portfolio_request.model_copy(update={
                "facts": first.facts,
                "portfolio": [{"name": "示例科技", "weight": 0.5}, {"name": "另一只基金", "weight": 0.2}],
            }),
            Intent.PORTFOLIO_REVIEW,
        )
    )

    assert "macro" in provider.calls and "industry" in provider.calls
    assert "macro" not in audit.reused_capabilities
    # 没变的那只持仓仍可沿用，不必跟着一起重取。
    assert "quote:1:示例科技" in audit.reused_capabilities
