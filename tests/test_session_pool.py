"""登录会话线程池的容量、超时与回收测试。"""

from __future__ import annotations

import pytest

from backend.app.session_pool import (
    SessionCapacityExceeded,
    SessionLeaseExpired,
    SessionThreadPool,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_pool_accepts_exactly_one_hundred_login_sessions() -> None:
    pool = SessionThreadPool(max_workers=100, idle_timeout_seconds=600)
    allocated = [pool.allocate(user_id) for user_id in range(100)]

    assert pool.snapshot()["active_sessions"] == 100
    assert pool.snapshot()["available_slots"] == 0
    with pytest.raises(SessionCapacityExceeded):
        pool.allocate(101)

    session_id, _ = allocated[0]
    assert pool.release(session_id, user_id=0) is True
    replacement_id, _ = pool.allocate(101)
    assert pool.is_active(replacement_id, user_id=101) is True


def test_idle_session_expires_but_in_flight_request_is_preserved() -> None:
    clock = FakeClock()
    pool = SessionThreadPool(max_workers=2, idle_timeout_seconds=600, clock=clock)
    session_id, _ = pool.allocate(1)

    assert pool.acquire(session_id) == 1
    clock.advance(601)
    assert pool.reap_expired() == 0
    pool.finish(session_id)
    clock.advance(600)

    assert pool.reap_expired() == 1
    with pytest.raises(SessionLeaseExpired):
        pool.acquire(session_id)


def test_beacon_credential_can_only_release_its_session() -> None:
    clock = FakeClock()
    pool = SessionThreadPool(max_workers=1, idle_timeout_seconds=600, clock=clock)
    session_id, beacon_token = pool.allocate(7)

    clock.advance(300)
    assert pool.release_beacon("invalid") is False
    assert pool.release_beacon(beacon_token) is True
    assert pool.is_active(session_id) is False
    assert pool.release_beacon(beacon_token) is False
