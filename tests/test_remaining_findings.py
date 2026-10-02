"""Adversarial checks for screenshot findings; live load is a separate probe."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from backend.app.agents.llm_agents import LLMConfig, OpenAICompatibleLLM
from backend.app.agents.rule_agents import StockAgent
from backend.app.data_provider.iwencai import IwencaiSkillHubProvider
from backend.app.models import FactRecord, Intent, OrchestrationRequest
from backend.app.services.model_telemetry import analysis_telemetry, summarize_costs
from backend.app.services.research import AutomatedResearchPipeline, derive_scoring_facts
from backend.app.services.research_cache import ResearchFactCache
from backend.app.services.research_recovery import recover_research
from backend.app.services.public_fact_store import SqlitePublicFactStore
from backend.app.session_pool import SessionThreadPool, SessionCapacityExceeded, SessionLeaseExpired
from backend.app.shared_sessions import SharedSessionStore
from backend.app.services.research_admission import ResearchAdmission
from backend.app.services.derived_score_cache import DerivedScoreCache
from backend.app.services.provider_errors import ProviderCallError


def fact(field, value, **kwargs):
    return FactRecord(fact_id=kwargs.pop("fact_id", field), field=field, value=value, entity="对象甲",
        snapshot_time=kwargs.pop("snapshot_time", datetime.now(timezone.utc)), source_id="EXPLICIT_TEST",
        quality=.9, **kwargs)


def test_raw_dimensions_units_and_lineage():
    roots = [fact("m2_growth", "10%"), fact("market_advancing_ratio", .6, unit="ratio"),
        fact("industry_revenue_growth", "5%"), fact("industry_turnover_percentile", 80, unit="percent"),
        fact("capital_flow", 20, unit="CNY", period="20261002"), fact("turnover_value", 100, unit="CNY", period="20261002")]
    scores = {f.field: f for f in derive_scoring_facts(roots, now=datetime.now(timezone.utc))}
    assert {k: v.value for k, v in scores.items()} == {"liquidity_score": 60, "risk_appetite_score": 60,
        "prosperity_score": 60, "crowding_score": 20, "capital_flow_score": 60}
    assert all(score.derivation_rule and set(score.derived_from) <= {f.fact_id for f in roots} for score in scores.values())
    assert not derive_scoring_facts([fact("capital_flow", 20, unit="CNY", period="a"),
                                    fact("turnover_value", 100, unit="万元", period="a")], now=datetime.now(timezone.utc))
    assert not derive_scoring_facts([fact("m2_growth", 10)], now=datetime.now(timezone.utc))


def test_qualitative_rubric_requires_complete_linked_evidence():
    doc = fact("announcement", "明确生效的政策及审计披露", source_url="https://example.org/notice", period="2026")
    assessments = [fact("policy_assessment", {"rubric": "POLICY_V1", "complete": True,
        "policy": {"label": "supportive", "evidence_id": doc.fact_id}}),
        fact("event_assessment", {"rubric": "EVENT_V1", "complete": True,
        "event": {"label": "adverse", "evidence_id": doc.fact_id}}),
        fact("governance_assessment", {"rubric": "GOVERNANCE_V1", "complete": True,
            **{name: {"label": label, "evidence_id": doc.fact_id} for name, label in
            [("audit_opinion", "qualified"), ("regulatory_status", "penalty"), ("disclosure_status", "delayed")]}})]
    results = derive_scoring_facts([doc, *assessments], now=datetime.now(timezone.utc))
    assert {f.field: f.value for f in results} == {"policy_score": 75, "event_score": 25, "governance_score": 16.67}
    for result in results:
        assert doc.fact_id in result.derived_from
    assert not derive_scoring_facts([doc] * 100, now=datetime.now(timezone.utc))
    assert not derive_scoring_facts(assessments, now=datetime.now(timezone.utc))
    malformed = fact("policy_assessment", {"rubric": "POLICY_V1", "complete": True,
        "policy": {"label": [], "evidence_id": {}}})
    assert not derive_scoring_facts([doc, malformed], now=datetime.now(timezone.utc))


@pytest.mark.asyncio
async def test_duplicate_scores_do_not_complete_security_dimensions():
    scores = [fact("fundamental_score", 60, fact_id=str(i)) for i in range(20)]
    request = OrchestrationRequest(query="研究对象甲", profile={"confirmed": True}, facts=scores)
    result = await StockAgent().run(request)
    assert result.status.value == "degraded"
    assert len(result.facts_used) == 1
    assert set(result.details["missing_fields"]) == {"valuation_score", "technical_score", "event_score", "governance_score"}


@pytest.mark.asyncio
async def test_401_has_safe_audit_and_no_repair_retry():
    attempts = []
    def handler(request):
        attempts.append(request)
        return httpx.Response(401, json={"error": "DO_NOT_LEAK_PROVIDER_BODY"})
    provider = IwencaiSkillHubProvider("DO_NOT_LEAK_KEY", transport=httpx.MockTransport(handler))
    pipeline = AutomatedResearchPipeline(provider)
    request = OrchestrationRequest(query="研究600519", profile={"confirmed": True, "risk_level": "R3"})
    prepared, audit = await pipeline.prepare(request, Intent.SECURITY_RESEARCH, target="600519")
    count = len(attempts)
    await recover_research(pipeline, prepared, Intent.SECURITY_RESEARCH, None, audit, target="600519")
    assert len(attempts) == count
    assert all(error["code"] == "AUTHENTICATION_REJECTED" and not error["retryable"] for error in audit.capability_errors.values())
    assert "DO_NOT_LEAK" not in audit.model_dump_json()
    await provider.aclose()


@pytest.mark.asyncio
async def test_exact_model_cache_is_account_scoped_and_preserves_cost():
    attempts = 0
    async def handler(request):
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(.03)
        return httpx.Response(200, json={"usage": {"prompt_tokens": 100, "completion_tokens": 20},
            "choices": [{"message": {"content": '{"answer":"ok"}'}, "finish_reason": "stop"}]})
    llm = OpenAICompatibleLLM(LLMConfig("https://example.org", "test", "test",
        input_price_per_million=1, output_price_per_million=2), transport=httpx.MockTransport(handler))
    async def call(scope, payload=None):
        collector = {"calls": [], "cache_scope": scope, "mode": "fast"}
        token = analysis_telemetry.set(collector)
        try:
            result = await llm.complete_json(system="s", payload=payload or {"query": "same"})
            return result, collector["calls"][0]
        finally:
            analysis_telemetry.reset(token)
    calls = await asyncio.gather(*[call("owner") for _ in range(100)])
    assert attempts == 1
    assert sum(item[1]["cache_hit"] for item in calls) == 99
    assert summarize_costs([item[1] for item in calls])["estimated_total"] == .00014
    await call("other")
    await call("owner", {"query": "changed"})
    assert attempts == 3
    assert summarize_costs([{"estimated_cost_usd": None}])["estimated_total"] is None
    await llm.aclose()


def test_four_real_processes_share_one_public_fetch(tmp_path):
    cache_path, counter = tmp_path / "public.sqlite", tmp_path / "fetches.txt"
    SqlitePublicFactStore(str(cache_path))  # initialize before concurrent opens
    worker = '''import asyncio, sys
from datetime import datetime, timezone
from pathlib import Path
from backend.app.models import FactRecord
from backend.app.services.research_cache import ResearchFactCache
async def run():
    cache=ResearchFactCache(shared_path=sys.argv[1])
    async def load():
        with Path(sys.argv[2]).open("a") as f: f.write("fetch\\n")
        await asyncio.sleep(.3)
        return [FactRecord(fact_id="PUBLIC",entity="e",field="close_price",value=10,snapshot_time=datetime.now(timezone.utc),source_id="TEST",quality=.9)]
    facts, hit=await cache.get("public-query",load,lambda: datetime.now(timezone.utc))
    print(facts[0].fact_id)
    await cache.aclose()
asyncio.run(run())'''
    processes = [subprocess.Popen([sys.executable, "-c", worker, str(cache_path), str(counter)],
        cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) for _ in range(4)]
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr.decode()
            assert b"PUBLIC" in stdout
        assert counter.read_text().splitlines() == ["fetch"]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()


@pytest.mark.asyncio
async def test_shared_cache_expires_and_force_refreshes(tmp_path):
    cache = ResearchFactCache(shared_path=str(tmp_path / "cache.sqlite"))
    now = datetime.now(timezone.utc)
    calls = 0
    async def load():
        nonlocal calls
        calls += 1
        return [fact("close_price", calls, snapshot_time=now)]
    assert (await cache.get("k", load, lambda: now))[0][0].value == 1
    other = ResearchFactCache(shared_path=str(tmp_path / "cache.sqlite"))
    assert (await other.get("k", load, lambda: now))[1]
    assert (await other.get("k", load, lambda: now, force_refresh=True))[0][0].value == 2
    now += timedelta(seconds=61)
    assert (await cache.get("k", load, lambda: now))[0][0].value == 3
    await cache.aclose()
    await other.aclose()


def test_shared_session_revocation_and_global_capacity(tmp_path):
    path = str(tmp_path / "sessions.sqlite")
    first = SessionThreadPool(max_workers=1, shared_path=path)
    second = SessionThreadPool(max_workers=1, shared_path=path)
    try:
        sid, beacon = first.allocate(7)
        assert second.acquire(sid) == 7
        assert first.snapshot()["active_requests"] == 1
        with pytest.raises(SessionCapacityExceeded):
            second.allocate(8)
        assert not first.release(sid, user_id=8)
        assert second.release_beacon(beacon)
        assert not first.is_active(sid)
        with pytest.raises(SessionLeaseExpired):
            second.submit(sid, lambda: True)
        second.finish(sid)
        assert first.snapshot()["active_sessions"] == 0
    finally:
        first.shutdown()
        second.shutdown()


def test_crashed_worker_reservation_expires(tmp_path):
    now = [0]
    store = SharedSessionStore(str(tmp_path / "sessions.sqlite"), 1, 10,
        clock=lambda: now[0], request_seconds=20)
    assert store.allocate("sid", 7, "digest")
    assert store.acquire("sid")[0] == 7
    now[0] = 11
    assert store.reap() == 0  # an active, bounded request protects the lease
    now[0] = 21
    assert store.reap() == 1  # a crashed worker cannot reserve capacity forever


@pytest.mark.asyncio
async def test_research_admission_rejects_overload_and_recovers():
    admission = ResearchAdmission(limit=1, queue_seconds=.02)
    first = await admission.acquire()
    with pytest.raises(TimeoutError):
        await admission.acquire()
    first.release()
    second = await admission.acquire()
    second.release()


@pytest.mark.asyncio
async def test_permission_rejection_coalesces_across_shared_caches(tmp_path):
    path = str(tmp_path / "cache.sqlite")
    first, second = ResearchFactCache(shared_path=path), ResearchFactCache(shared_path=path)
    calls = 0
    async def load():
        nonlocal calls
        calls += 1
        await asyncio.sleep(.02)
        raise ProviderCallError("safe error", code="AUTHENTICATION_REJECTED", status_code=401)
    results = await asyncio.gather(first.get("same-authorized-query", load, lambda: datetime.now(timezone.utc)),
        second.get("same-authorized-query", load, lambda: datetime.now(timezone.utc)), return_exceptions=True)
    assert calls == 1
    assert all(isinstance(result, ProviderCallError) for result in results)
    assert not first.entries and not second.entries
    with pytest.raises(ProviderCallError):
        await second.get("changed-credential-query", load, lambda: datetime.now(timezone.utc))
    assert calls == 2
    await first.aclose()
    await second.aclose()


@pytest.mark.asyncio
async def test_skill_credentials_follow_fixed_skill_and_partition_cache():
    seen = []
    def handler(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"data": [{"股票代码": "600519", "最新价": 10}]})
    provider = IwencaiSkillHubProvider("default-test", skill_api_keys={"hithink-finance-query": "finance-test"}, transport=httpx.MockTransport(handler))
    await provider.get_financial_metrics("600519")
    await provider.get_quote("600519")
    assert seen == ["Bearer finance-test", "Bearer default-test"]
    other = IwencaiSkillHubProvider("default-test", skill_api_keys={"hithink-finance-query": "other-test"})
    assert provider.cache_namespace != other.cache_namespace
    assert "finance-test" not in provider.cache_namespace
    await provider.aclose()
    await other.aclose()


def test_derived_memo_preserves_time_and_invalidates_changed_inputs():
    now = datetime.now(timezone.utc)
    cache = DerivedScoreCache()
    roots = [fact("roe", 10, unit="percent", snapshot_time=now)]
    token = analysis_telemetry.set({"cache_scope": "owner"})
    try:
        initial = cache.derive(roots, now, derive_scoring_facts)[0]
        repeated = cache.derive(roots, now + timedelta(seconds=5), derive_scoring_facts)[0]
        assert repeated.snapshot_time == initial.snapshot_time
        unrelated = fact("news", "unrelated", snapshot_time=now)
        assert cache.derive([*roots, unrelated], now + timedelta(seconds=6), derive_scoring_facts)[0].snapshot_time == initial.snapshot_time
        changed = [fact("roe", 20, unit="percent", snapshot_time=now)]
        assert cache.derive(changed, now + timedelta(seconds=7), derive_scoring_facts)[0].value != initial.value
        assert not cache.derive(roots, now + timedelta(days=91), derive_scoring_facts)
    finally:
        analysis_telemetry.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload, result", [
    ({"required_schema": {"title": "OutputReview"}}, {"confidence": "invalid"}),
    ({"required_output": {"agent_id": "security"}, "rule_baseline": {"opinion": "baseline"},
      "authorized_facts": [{"fact_id": "allowed"}]},
     {"opinion": "invalid reference", "confidence": .8, "facts_used": ["unknown"]}),
])
async def test_invalid_schema_or_reference_is_not_model_cached(payload, result):
    attempts = 0
    async def handler(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": result}}]})
    llm = OpenAICompatibleLLM(LLMConfig("https://example.org", "test", "test"), transport=httpx.MockTransport(handler))
    token = analysis_telemetry.set({"calls": [], "cache_scope": "owner"})
    try:
        for _ in range(2):
            await llm.complete_json(system="s", payload=payload)
        assert attempts == 2
        assert not llm._responses.entries
    finally:
        analysis_telemetry.reset(token)
        await llm.aclose()
