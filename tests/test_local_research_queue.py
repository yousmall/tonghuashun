"""Short local bursts queue without admitting unbounded active work."""
import asyncio

import pytest

from backend.app import main
from backend.app.services.research_admission import ResearchAdmission


@pytest.mark.asyncio
async def test_local_burst_drains_without_overload_and_preserves_active_limit():
    admission = ResearchAdmission(limit=16, queue_seconds=.75)
    active = peak = 0
    async def work():
        nonlocal active, peak
        slot = await admission.acquire()
        try:
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.015)
        finally:
            active -= 1
            slot.release()
    await asyncio.wait_for(asyncio.gather(*(work() for _ in range(100))), timeout=2)
    assert peak == 16 and active == 0
    assert main.local_research_admission is not main.research_admission


@pytest.mark.asyncio
async def test_queue_timeout_and_cancellation_do_not_leak_slots():
    admission = ResearchAdmission(limit=1, queue_seconds=.03)
    first = await admission.acquire()
    with pytest.raises(TimeoutError):
        await admission.acquire()
    queued = asyncio.create_task(admission.acquire())
    await asyncio.sleep(0)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    first.release()
    recovered = await admission.acquire()
    recovered.release()
