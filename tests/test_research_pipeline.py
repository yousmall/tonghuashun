"""自动取数闭环测试：意图路由、并行数据补齐、派生血缘和 API 回传。"""

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import backend.app.main as main_module
from backend.app.models import FactRecord, Intent, OrchestrationRequest
from backend.app.services import AutomatedResearchPipeline


class FakeIwencaiProvider:
    """只返回确定性样本，不访问网络；调用记录用于验证最小能力路由。"""

    source_id = "IWENCAI_TEST"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def _fact(self, suffix: str, field: str, value: object, *, entity: str = "示例科技") -> FactRecord:
        return FactRecord(
            fact_id=f"LIVE-{suffix}",
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
