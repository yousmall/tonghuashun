"""Behavioral regressions for the six findings from the full-project audit."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from backend.app.models import AgentResult, FactRecord, Intent, OrchestrationRequest, UserProfile, ResearchCapability
from backend.app.agents.coordinator import cross_validate_results
from backend.app.agents.llm_agents import HybridInvestmentAgent, LLMConfig, OpenAICompatibleLLM
from backend.app.agents.rule_agents import MacroAgent
from backend.app.database import Database, MessageRow, ResearchEvidenceRow
from backend.app.data_provider.iwencai import IwencaiSkillHubProvider
from backend.app.services.history import cited_facts_of, summarise_advice, used_fact_ids_of
from backend.app.services.research import AutomatedResearchPipeline, derive_scoring_facts
from backend.app.services.research_cache import ResearchFactCache
from backend.app.services.evidence_coverage import missing_fields_by_agent
from backend.app.services.model_input import slice_facts_for_agent
from backend.app.services.model_telemetry import analysis_telemetry


def fact(field="change", value="0.5%", *, fact_id="f", unit=None, entity="公司甲", **kwargs):
    return FactRecord(fact_id=fact_id, field=field, value=value, unit=unit, entity=entity,
                      snapshot_time=kwargs.pop("snapshot_time", datetime.now(timezone.utc)),
                      source_id="TEST", quality=.9, **kwargs)


@pytest.mark.parametrize("points", [.5, -.5, 1, -1, 0, 12])
def test_percentage_representations_produce_identical_scores(points):
    variants = [fact(value=f"{points}%"), fact(value=points, unit="percent"),
                fact(value=points / 100, unit="ratio")]
    scores = [derive_scoring_facts([item], now=item.snapshot_time)[0].value for item in variants]
    assert scores == [max(0, min(100, 50 + points * 3))] * 3
    assert variants[0].value == f"{points}%"


@pytest.mark.parametrize("value,unit", [(.5, None), (.005, None), (.5, "unknown"),
                                         ("0.5%", "ratio"), (True, "percent"),
                                         (float("inf"), "percent"), ("1% 注入 100", None)])
def test_unknown_or_conflicting_percentage_unit_never_silently_scores(value, unit):
    item = fact(value=value, unit=unit)
    assert item.normalized_value is None
    assert derive_scoring_facts([item], now=item.snapshot_time) == []


@pytest.mark.parametrize("field,points,score_field,expected", [
    ("roe", .5, "fundamental_score", 50.75),
    ("revenue_growth", -1, "fundamental_score", 49),
    ("fee_rate", .5, "fund_score", 90),
    ("tracking_error", 1, "fund_score", 80),
    ("interest_rate", 1, "liquidity_score", 69),
    ("cpi", 1, "inflation_score", 88),
])
def test_percentage_rules_for_financial_fund_and_macro_fields(field, points, score_field, expected):
    for value, unit in [(points, "percent"), (points / 100, "ratio"), (f"{points}%", None)]:
        item = fact(field, value, unit=unit)
        scores = derive_scoring_facts([item], now=item.snapshot_time)
        assert next(score.value for score in scores if score.field == score_field) == expected


@pytest.mark.asyncio
async def test_provider_declares_percentage_point_units_and_preserves_macro_index_units():
    provider = IwencaiSkillHubProvider("test-secret", base_url="https://test.example")
    rates = provider._normalize({"data": [{"股票代码": "600001", "涨跌幅": .5, "净资产收益率": .5}]}, entity_hint="600001")
    assert all(item.unit == "percent" and item.normalized_value == .5 for item in rates)
    macro = provider._normalize({"data": [{"指标名称": "居民消费价格指数", "指标单位": "指数", "宏观@值[20260901]": 101}]}, entity_hint="宏观")
    assert macro[0].unit == "指数" and macro[0].normalized_value is None
    yoy = provider._normalize({"data": [{"指标名称": "CPI同比", "宏观@值[20260901]": .5}]}, entity_hint="宏观")
    assert yoy[0].field == "cpi" and yoy[0].normalized_value == .5
    assert "CPI同比" in yoy[0].source_field
    await provider.aclose()


@pytest.mark.asyncio
async def test_explicit_china_macro_scope_combines_indicators_but_preserves_other_regions():
    def handler(request):
        return httpx.Response(200, json={"data": [
            {"名称": "制造业PMI", "指标名称": "制造业PMI", "宏观@值[20260831]": 50.1},
            {"名称": "CPI:当月同比", "指标名称": "CPI同比", "宏观@值[20260831]": .8},
            {"名称": "PPI:当月同比", "指标名称": "PPI同比", "宏观@值[20260831]": 3.8},
            {"名称": "美国CPI", "指标名称": "CPI同比", "宏观@值[20260831]": 2.5},
            {"国家": "美国", "指标名称": "CPI同比", "宏观@值[20260831]": 2.4},
        ]})
    provider = IwencaiSkillHubProvider("test-secret", base_url="https://test.example", transport=httpx.MockTransport(handler))
    facts = await provider.get_macro_data("中国最新宏观经济")
    china = [item for item in facts if item.entity == "中国宏观经济"]
    assert {item.field for item in china} == {"pmi", "cpi", "ppi"}
    scores = derive_scoring_facts(facts, now=datetime.now(timezone.utc))
    assert {item.field for item in scores if item.entity == "中国宏观经济"} == {"growth_score", "inflation_score"}
    inflation = next(item for item in scores if item.entity == "中国宏观经济" and item.field == "inflation_score")
    assert set(inflation.derived_from) == {item.fact_id for item in china if item.field in {"cpi", "ppi"}}
    assert all(item.unit == "percent" and item.normalized_value == item.value for item in china if item.field in {"cpi", "ppi"})
    assert all(item.source_field and item.period for item in china)
    assert any(item.entity == "美国CPI" for item in facts)
    assert any(item.entity == "美国:CPI同比" for item in facts)
    custom = await provider.get_macro_data("中国和美国宏观比较")
    assert not any(item.entity == "中国宏观经济" for item in custom)
    await provider.aclose()


@pytest.mark.asyncio
async def test_query_permission_errors_do_not_disable_healthy_provider_capabilities():
    calls = []
    def handler(request):
        query = json.loads(request.content)["query"]
        calls.append(query)
        if "拒绝" in query:
            return httpx.Response(401, json={"error": "not authorized"})
        if "故障" in query:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"data": [{"股票代码": "600001", "最新价": 10}]})
    provider = IwencaiSkillHubProvider("test-secret", base_url="https://test.example", max_retries=0,
                                      transport=httpx.MockTransport(handler))
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await provider.query("拒绝")
    assert provider._circuit_open_until is None
    assert (await provider.get_quote("600001"))[0].value == 10
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await provider.query("故障")
    with pytest.raises(RuntimeError, match="熔断"):
        await provider.get_quote("600001")
    assert len(calls) == 7  # An actual outage still opens the bounded breaker.
    await provider.aclose()


def test_normalized_units_do_not_raise_false_source_conflicts_and_conflicts_are_replayable():
    now = datetime.now(timezone.utc)
    first = fact(value="0.5%", snapshot_time=now, source_field="涨跌幅")
    second = fact(value=.005, unit="ratio", fact_id="other", snapshot_time=now, source_field="normalized change")
    result = AgentResult(agent_id="security", status="completed", opinion="测试", confidence=.8, facts_used=["f"])
    assert not cross_validate_results([result], [first, second]).issues
    changed = FactRecord.model_validate({**second.model_dump(), "value": .01})
    issue = cross_validate_results([result], [first, changed]).issues[0]
    assert issue.code == "INTERNAL_VALUE_CONFLICT"
    assert issue.evidence_details[1]["normalized_value"] == 1
    assert {detail["source_field"] for detail in issue.evidence_details} == {"涨跌幅", "normalized change"}


def test_more_than_sixty_citations_and_multilevel_lineage_roundtrip_in_separate_table():
    db = Database("sqlite+pysqlite:///:memory:", "test-secret-longer-than-thirty-two-characters")
    db.initialize()
    owner = int(db.create_user("evidence-owner", "hash")["id"])
    other = int(db.create_user("evidence-other", "hash")["id"])
    facts = [fact("news", f"资料{i}", fact_id=f"F{i}").model_dump(mode="json") for i in range(70)]
    facts += [fact("fundamental_score", 60, fact_id="D1", derived_from=["F68", "F69"]).model_dump(mode="json"),
              fact("fundamental_score", 60, fact_id="D2", derived_from=["D1"]).model_dump(mode="json")]
    advice = {"facts": facts, "evidence": [f"F{i}" for i in range(68)] + ["D2"],
              "agent_results": [{"agent_id": "security", "facts_used": ["D2"]}]}
    summary = summarise_advice(advice, used_fact_ids_of(advice))
    assert len(summary["facts"]) == 60
    chat = db.save_exchange(owner, None, "研究", {"facts": facts}, "结果", summary, cited_facts_of(advice))
    with db.session_factory() as session:
        rows = session.scalars(select(MessageRow).order_by(MessageRow.id)).all()
        assert rows[0].payload["facts"] == []
        assert len(rows[1].payload["facts"]) == 60
        assert len(session.get(ResearchEvidenceRow, rows[1].id).facts) == 72
    restored = db.get_conversation(owner, chat, limit=1)["messages"][0]["payload"]
    assert len(restored["facts"]) == 72
    assert used_fact_ids_of(restored) <= {item["fact_id"] for item in restored["facts"]}
    assert restored["evidence_restored"] and not restored["facts_truncated"]
    assert db.get_conversation(other, chat) is None
    assert db.list_conversations(owner)[0]["message_count"] == 2
    db.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("rule_score,model_score", [(60, 95), (60, None), (None, 95)])
async def test_rule_score_cannot_be_replaced_by_model_score(rule_score, model_score):
    async def baseline(request):
        return AgentResult(agent_id="market", status="completed", opinion="规则", score=rule_score,
                           confidence=.8, facts_used=["f"], risk_flags=["规则约束"])
    class LLM:
        config = SimpleNamespace(model="test")
        async def complete_json(self, **kwargs):
            return dict(opinion="模型", confidence=.9, score=model_score, facts_used=["f"], rule_score=99)
    request = OrchestrationRequest(query="市场", profile=UserProfile(confirmed=True), facts=[fact("growth_score", 60)])
    result = await HybridInvestmentAgent("market", baseline, LLM()).run(request)
    assert result.score == result.rule_score == rule_score
    assert result.model_score == model_score
    assert result.score_policy == "rule_only"
    assert "规则约束" in result.risk_flags
    assert cross_validate_results([result], request.facts).consensus_score == rule_score


@pytest.mark.asyncio
async def test_coverage_and_market_rules_never_combine_entities():
    fields = ["growth_score", "inflation_score", "liquidity_score", "policy_score", "risk_appetite_score"]
    facts = [fact(field, 60, fact_id=str(i), entity="甲" if i < 3 else "乙") for i, field in enumerate(fields)]
    gaps = missing_fields_by_agent(facts, Intent.MARKET_ANALYSIS)
    assert gaps["market"] == ["policy_score", "risk_appetite_score"]
    result = await MacroAgent().run(OrchestrationRequest(query="市场", profile=UserProfile(confirmed=True), facts=facts))
    assert result.status.value == "degraded" and result.score is None


def test_capability_templates_keep_macro_scope_and_explicit_security_code():
    pipeline = AutomatedResearchPipeline(None)
    request = OrchestrationRequest(query="请研究600001的最新财报", profile=UserProfile(confirmed=True))
    target = pipeline._validated_target(request, "600002")
    assert target == "600001"
    calls = pipeline._calls_for(request, Intent.SECURITY_RESEARCH, target)
    args = {call.label: call.args[0] for call in calls}
    assert args["macro"] == "中国最新宏观经济"
    assert args["quote"] == args["financial"] == "600001"
    assert args["industry"] == "600001所属行业"
    for intent in (Intent.INDUSTRY_ANALYSIS, Intent.FUND_SCREENING, Intent.CONVERTIBLE_BOND_ANALYSIS):
        assert "macro" in {call.label for call in pipeline._calls_for(request, intent)}
    assert pipeline._validated_target(request.model_copy(update={"query": "研究公司甲"}), "凭空的公司") is None


@pytest.mark.asyncio
async def test_cache_coalesces_requests_copies_values_and_expires_by_actual_field_age():
    current = datetime.now(timezone.utc)
    now = lambda: current
    cache = ResearchFactCache(max_entries=2)
    calls = 0
    async def load():
        nonlocal calls
        calls += 1
        await asyncio.sleep(.01)
        return [fact(snapshot_time=current)]
    left, right = await asyncio.gather(cache.get("quote", load, now), cache.get("quote", load, now))
    assert calls == 1 and not left[1] and right[1]
    left[0][0].value = "changed locally"
    assert (await cache.get("quote", load, now))[0][0].value == "0.5%"
    current += timedelta(seconds=61)
    await cache.get("quote", load, now)
    assert calls == 2
    async def empty():
        nonlocal calls
        calls += 1
        return []
    await cache.get("empty", empty, now)
    await cache.get("empty", empty, now)
    assert calls == 4 and "empty" not in cache.entries
    async def fail():
        raise RuntimeError("failed")
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await cache.get("failure", fail, now)
    assert not cache.pending


@pytest.mark.asyncio
async def test_cancelled_cache_waiter_does_not_cancel_other_users_source_request():
    cache = ResearchFactCache()
    gate = asyncio.Event()
    async def load():
        await gate.wait()
        return [fact()]
    now = lambda: datetime.now(timezone.utc)
    first = asyncio.create_task(cache.get("quote", load, now))
    await asyncio.sleep(0)
    second = asyncio.create_task(cache.get("quote", load, now))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    gate.set()
    result, shared = await second
    assert shared and result and not cache.pending


@pytest.mark.asyncio
async def test_forced_cache_refresh_has_bounded_cooldown_and_expires_with_fact():
    current = datetime.now(timezone.utc)
    cache = ResearchFactCache(max_entries=1)
    calls = 0
    async def load():
        nonlocal calls
        calls += 1
        return [fact(snapshot_time=current)]
    async def get(key="quote", **kwargs):
        return await cache.get(key, load, lambda: current, **kwargs)
    await get()
    await get(force_refresh=True, refresh_cooldown_seconds=30)
    assert calls == 2  # A regular cache hit is insufficient for initial repair.
    await get(force_refresh=True, refresh_cooldown_seconds=30)
    assert calls == 2
    current += timedelta(seconds=31)
    await get(force_refresh=True, refresh_cooldown_seconds=30)
    assert calls == 3
    current += timedelta(seconds=61)
    await get(force_refresh=True, refresh_cooldown_seconds=300)
    assert calls == 4  # Stale facts cannot be revived by a cooldown.
    await get("other", force_refresh=True)
    assert set(cache.refreshed_at) == {"other"}
    await cache.aclose()
    assert not cache.refreshed_at and not cache.refreshing


@pytest.mark.asyncio
async def test_refresh_during_initial_fetch_waits_for_a_distinct_fresh_read():
    cache = ResearchFactCache()
    gate = asyncio.Event()
    calls = 0
    async def load():
        nonlocal calls
        calls += 1
        number = calls
        if number == 1:
            await gate.wait()
        return [fact(value=f"{number}%")]
    now = lambda: datetime.now(timezone.utc)
    initial = asyncio.create_task(cache.get("quote", load, now))
    await asyncio.sleep(0)
    repairs = [asyncio.create_task(cache.get("quote", load, now, force_refresh=True,
                                            refresh_cooldown_seconds=30)) for _ in range(2)]
    await asyncio.sleep(0)
    gate.set()
    first, *fresh = await asyncio.gather(initial, *repairs)
    assert calls == 2 and first[0][0].value == "1%"
    assert all(result[0][0].value == "2%" for result in fresh)
    assert (await cache.get("quote", load, now))[0][0].value == "2%"


@pytest.mark.asyncio
async def test_model_telemetry_is_request_local_and_accounts_usage_without_prompt_contents():
    max_tokens = []
    async def handler(request):
        max_tokens.append(json.loads(request.content)["max_tokens"])
        await asyncio.sleep(.01)
        return httpx.Response(200, json={"usage": {"prompt_tokens": 100, "completion_tokens": 20},
                                        "choices": [{"message": {"content": "{}"}}]})
    client = OpenAICompatibleLLM(LLMConfig("https://test.example", "secret", "test", max_concurrency=1,
                                          input_price_per_million=1, output_price_per_million=2),
                                 transport=httpx.MockTransport(handler))
    async def run(mode):
        collector = {"calls": [], "mode": mode}
        token = analysis_telemetry.set(collector)
        try:
            await client.complete_json(system="private prompt", payload={"private": "never recorded"})
        finally:
            analysis_telemetry.reset(token)
        return collector["calls"]
    fast, deep = await asyncio.gather(run("fast"), run("deep"))
    await client.aclose()
    assert max_tokens == [1000, 2000]
    assert len(fast) == len(deep) == 1
    assert deep[0]["queue_ms"] > fast[0]["queue_ms"]
    assert fast[0]["estimated_cost_usd"] == .00014
    assert fast[0]["prompt_tokens"] == 100 and fast[0]["status"] == "completed"
    assert "private" not in json.dumps(fast)
    assert analysis_telemetry.get() is None


def test_role_slice_retains_full_rule_lineage_even_when_fast_budget_is_exceeded():
    parents = [fact("news", str(i), fact_id=f"P{i}") for i in range(65)]
    derived = fact("growth_score", 60, fact_id="D", derived_from=[item.fact_id for item in parents])
    extra = [fact("close_price", 20, fact_id="unrelated")]
    kept = slice_facts_for_agent(parents + [derived] + extra, "market", ["D"], mode="fast")
    assert {item.fact_id for item in kept} == {"D", *(item.fact_id for item in parents)}


def test_display_charts_and_comparison_share_the_same_explicit_percentage_units():
    from frontend.presentation import friendly_fact_rows
    from frontend.answer_report import chart_groups
    from frontend.comparison import comparison_matrix
    from frontend.research_board import format_fact
    a = fact("roe", .005, unit="ratio", fact_id="a", entity="甲", period="2026Q2").model_dump(mode="json")
    b = fact("roe", .5, unit="percent", fact_id="b", entity="乙", period="2026Q2").model_dump(mode="json")
    assert friendly_fact_rows([a, b])[0]["内容 / 数值"] == "0.5%"
    assert format_fact(a) == format_fact(b) == "0.50%"
    chart = chart_groups({"facts": [a, b], "evidence": ["a", "b"]})[0]
    assert [row["value"] for row in chart["rows"]] == [.5, .5]
    matrix = comparison_matrix([{"target": "甲", "facts": [a]}, {"target": "乙", "facts": [b]}])[0]
    assert matrix["甲"] == matrix["乙"] == "0.50"
    unknown = {**a, "unit": None}
    assert "单位待确认" in friendly_fact_rows([unknown])[0]["内容 / 数值"]
    assert chart_groups({"facts": [unknown, b], "evidence": ["a", "b"]}) == []


def test_fast_mode_reduces_parallel_model_inputs_without_removing_baseline_inputs():
    baseline = fact("fundamental_score", 60, fact_id="baseline")
    news = [fact("news", "新闻" * 100, fact_id=f"N{i}", entity="公司甲") for i in range(20)]
    fast = slice_facts_for_agent([baseline, *news], "security", ["baseline"], mode="fast")
    deep = slice_facts_for_agent([baseline, *news], "security", ["baseline"], mode="deep")
    assert len(fast) == 3 and len(deep) == 7
    assert fast[0].fact_id == deep[0].fact_id == "baseline"


@pytest.mark.asyncio
async def test_events_use_a_document_search_with_disclosure_dates_and_links():
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/comprehensive/search"
        assert body["channels"] == ["announcement"]
        assert "600001" in body["query"]
        return httpx.Response(200, json={"data": [{"标题": "业绩预告", "publish_date": "2026-09-30",
                                                   "url": "https://test.example/report"}]})
    provider = IwencaiSkillHubProvider("test-secret", base_url="https://test.example", transport=httpx.MockTransport(handler))
    facts = await provider.get_event_data("600001")
    await provider.aclose()
    assert any(item.field == "announcement" for item in facts)
    assert all(item.source_url == "https://test.example/report" and item.period.startswith("REC-") for item in facts)


@pytest.mark.asyncio
async def test_industry_median_is_not_a_company_quote_and_cannot_change_technical_score():
    provider = IwencaiSkillHubProvider("test-secret", base_url="https://test.example")
    facts = provider._normalize({"data": [{"股票简称": "公司甲", "最新涨跌幅": .5, "涨跌幅行业中值": 2}]}, entity_hint="公司甲")
    await provider.aclose()
    assert {item.field for item in facts} == {"change", "industry_median_change"}
    scores = derive_scoring_facts(facts, now=facts[0].snapshot_time)
    assert scores[0].value == 51.5
    result = AgentResult(agent_id="security", status="completed", opinion="测试", confidence=.8, facts_used=[facts[0].fact_id])
    assert cross_validate_results([result], facts).issues == []


@pytest.mark.asyncio
async def test_identical_disclosure_capabilities_share_one_call_and_keep_both_audit_labels():
    searches = 0
    def handler(request):
        nonlocal searches
        if request.url.path == "/v1/comprehensive/search":
            searches += 1
            return httpx.Response(200, json={"data": [{"标题": "公司公告", "publish_date": "2026-09-30"}]})
        return httpx.Response(200, json={"data": []})
    provider = IwencaiSkillHubProvider("test-secret", base_url="https://test.example", transport=httpx.MockTransport(handler))
    request = OrchestrationRequest(query="研究600001的公告", profile=UserProfile(confirmed=True))
    prepared, audit = await AutomatedResearchPipeline(provider).prepare(request, Intent.SECURITY_RESEARCH,
        target="600001", data_requirements=[ResearchCapability.ANNOUNCEMENT])
    await provider.aclose()
    assert searches == 1
    assert {"event", "announcement"} <= set(audit.successful_capabilities)
    assert "announcement" in audit.cached_capabilities
    assert audit.fetched_fact_count == len(prepared.facts)


@pytest.mark.asyncio
async def test_company_profit_margins_are_never_macro_interest_rates():
    provider = IwencaiSkillHubProvider("test-secret", base_url="https://test.example")
    facts = provider._normalize({"data": [{"股票简称": "公司甲", "毛利率": 95, "净利率": 80}]}, entity_hint="公司甲")
    await provider.aclose()
    assert {item.field for item in facts} == {"gross_margin", "net_margin"}
    assert all(item.normalized_value is not None for item in facts)
    assert derive_scoring_facts(facts, now=facts[0].snapshot_time) == []


def test_refreshed_source_recomputes_scores_instead_of_reusing_old_derived_values():
    source = fact(value="0.5%", fact_id="new-quote")
    old = fact("technical_score", 99, fact_id="old-score", derived_from=["old-quote"])
    old = old.model_copy(update={"source_id": "DERIVED_RULE_V1"})
    derived = derive_scoring_facts([source, old], now=source.snapshot_time)
    assert len(derived) == 1 and derived[0].value == 51.5
    assert derived[0].derived_from == ["new-quote"]
