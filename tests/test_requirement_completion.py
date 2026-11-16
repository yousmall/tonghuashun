"""Queue, true source timestamps and live dependency failure/recovery."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.app.data_provider.iwencai import _source_timestamp
from backend.app.models import FactRecord
from backend.app.services.data_freshness import freshness_observation
from backend.app.services.dependency_health import DatabaseHealthProbe
from backend.app.services.research_admission import ResearchAdmission


@pytest.mark.asyncio
async def test_hundred_deep_requests_queue_without_dropping_or_exceeding_active_budget():
    gate = ResearchAdmission(limit=2, queue_seconds=10, max_pending=100)
    active = peak = 0
    async def work():
        nonlocal active, peak
        slot = await gate.acquire()
        try:
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.002)
        finally:
            active -= 1
            slot.release()
    await asyncio.gather(*(work() for _ in range(100)))
    assert peak == 2 and active == gate.pending == 0


@pytest.mark.asyncio
async def test_full_queue_rejects_excess_and_cancellation_frees_waiting_capacity():
    gate = ResearchAdmission(limit=1, queue_seconds=20, max_pending=1)
    slot = await gate.acquire()
    queued = asyncio.create_task(gate.acquire())
    await asyncio.sleep(0)
    with pytest.raises(TimeoutError):
        await gate.acquire()
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert gate.pending == 0
    slot.release()
    (await gate.acquire()).release()


def test_source_timestamp_requires_intraday_precision_and_uses_china_timezone():
    assert _source_timestamp({"行情时间": "2026-11-16"}) is None
    assert _source_timestamp({"日期": "2026-11-16", "时间": "15:00:00"}) is None
    assert _source_timestamp({"行情时间": "2026-11-16 15:00:01"}) == datetime(2026, 11, 16, 7, 0, 1, tzinfo=timezone.utc)
    assert _source_timestamp({"quote_time": "2026-11-16T07:00:01Z"}) == datetime(2026, 11, 16, 7, 0, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("age,status,verified", [(None,"source_timestamp_unavailable",False),(2,"within_target",True),(8,"delayed",False),(-60,"clock_error",False)])
def test_capture_time_cannot_prove_source_freshness(age, status, verified):
    now = datetime.now(timezone.utc)
    fact = FactRecord(fact_id="q", entity="test", field="close_price", value=1, quality=.9,
                      snapshot_time=now, source_id="test", source_timestamp=now-timedelta(seconds=age) if age is not None else None)
    result = freshness_observation(fact, now)
    assert result["status"] == status and result["seconds_level_verified"] is verified
    with pytest.raises(ValidationError):
        FactRecord(**{**fact.model_dump(), "source_timestamp": datetime(2026,11,16)})


@pytest.mark.asyncio
async def test_database_probe_recovers_and_unconfigured_database_is_unready(monkeypatch):
    gate = DatabaseHealthProbe(ttl=0)
    db = SimpleNamespace(configured=True, initialization_error=None)
    monkeypatch.setattr(gate, "_ping", lambda _: False)
    assert not await gate.check(db)
    monkeypatch.setattr(gate, "_ping", lambda _: True)
    assert await gate.check(db)
    db.configured = False
    assert not await gate.check(db)


@pytest.mark.asyncio
async def test_dependency_readiness_fails_closed(monkeypatch):
    from backend.app import main
    from fastapi import Response
    async def check(_): return False
    monkeypatch.setattr(main.database_health_probe, "check", check)
    response = Response()
    body = await main.dependency_readiness(response)
    assert response.status_code == 503 and body["status"] == "unavailable"


@pytest.mark.asyncio
async def test_probe_timeout_coalesces_threads_and_can_recover(monkeypatch):
    from threading import Event
    released = Event()
    calls = []
    def ping(_):
        calls.append(1)
        released.wait(1)
        return True
    gate = DatabaseHealthProbe(ttl=0, timeout=.01)
    monkeypatch.setattr(gate, "_ping", ping)
    db = SimpleNamespace(configured=True, initialization_error=None)
    try:
        assert not await gate.check(db)
        assert not await gate.check(db)
        assert len(calls) == 1
    finally:
        released.set()
        await gate.task
    assert await gate.check(db)


@pytest.mark.asyncio
async def test_source_timestamp_survives_adapter_history_and_display():
    from backend.app.data_provider import IwencaiSkillHubProvider
    from frontend.presentation import source_trace_rows
    provider = IwencaiSkillHubProvider("synthetic-no-network")
    facts = provider._normalize({"data":[{"股票代码":"600519", "名称":"测试", "最新价":1,
        "行情时间":"2026-11-16 15:00:01"}]}, entity_hint="600519")
    quote = next(f for f in facts if f.field == "close_price")
    restored = FactRecord.model_validate_json(quote.model_dump_json())
    assert restored.source_timestamp == datetime(2026,11,16,7,0,1,tzinfo=timezone.utc)
    rows = source_trace_rows({"facts":[restored.model_dump(mode="json")],"evidence":[quote.fact_id]})
    assert "15:00:01" in rows[0]["上游行情时间（北京时间）"]
    await provider.aclose()
