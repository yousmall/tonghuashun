"""登录会话租约与应用级阻塞任务线程池。"""

from __future__ import annotations

import hashlib
import os
import secrets
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Callable, Generic, ParamSpec, TypeVar


P = ParamSpec("P")
R = TypeVar("R")


class SessionCapacityExceeded(RuntimeError):
    """全部登录会话槽均已占用。"""


class SessionLeaseExpired(RuntimeError):
    """会话已退出、被回收或因空闲超时失效。"""


@dataclass(slots=True)
class SessionLease:
    """一个登录会话占用的逻辑线程槽。"""

    user_id: int
    last_activity: float
    beacon_digest: str
    active_requests: int = 0


class SessionThreadPool(Generic[R]):
    """以登录会话租约约束容量，并复用固定数量的工作线程。"""

    def __init__(
        self,
        max_workers: int = 100,
        idle_timeout_seconds: float = 600,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers 必须大于 0")
        if idle_timeout_seconds <= 0:
            raise ValueError("idle_timeout_seconds 必须大于 0")
        self.max_workers = max_workers
        self.idle_timeout_seconds = idle_timeout_seconds
        self._clock = clock
        self._lock = Lock()
        self._leases: dict[str, SessionLease] = {}
        self._beacon_sessions: dict[str, str] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="wence-session",
        )

    @classmethod
    def from_env(cls) -> "SessionThreadPool[object]":
        """按环境变量创建线程池，默认支持 100 个并发登录会话。"""

        return cls(
            max_workers=int(os.getenv("WENCE_SESSION_MAX_WORKERS", "100")),
            idle_timeout_seconds=float(os.getenv("WENCE_SESSION_IDLE_SECONDS", "600")),
        )

    def allocate(self, user_id: int) -> tuple[str, str]:
        """分配会话槽，并返回会话标识和最小权限的浏览器回收凭据。"""

        now = self._clock()
        with self._lock:
            self._reap_expired_locked(now)
            if len(self._leases) >= self.max_workers:
                raise SessionCapacityExceeded(
                    f"当前在线会话已达到 {self.max_workers} 个，请稍后重试"
                )
            session_id = secrets.token_urlsafe(24)
            beacon_token = secrets.token_urlsafe(32)
            beacon_digest = self._digest_beacon(beacon_token)
            self._leases[session_id] = SessionLease(
                user_id=user_id,
                last_activity=now,
                beacon_digest=beacon_digest,
            )
            self._beacon_sessions[beacon_digest] = session_id
            return session_id, beacon_token

    def acquire(self, session_id: str) -> int:
        """校验会话、记录请求开始，并返回该租约绑定的用户 ID。"""

        now = self._clock()
        with self._lock:
            self._reap_expired_locked(now)
            lease = self._leases.get(session_id)
            if lease is None:
                raise SessionLeaseExpired("登录已失效或闲置超过 10 分钟，请重新登录")
            lease.last_activity = now
            lease.active_requests += 1
            return lease.user_id

    def finish(self, session_id: str) -> None:
        """记录请求结束；执行中的请求不会被空闲清理器误回收。"""

        now = self._clock()
        with self._lock:
            lease = self._leases.get(session_id)
            if lease is None:
                return
            lease.active_requests = max(0, lease.active_requests - 1)
            lease.last_activity = now

    def touch(self, session_id: str) -> int:
        """刷新浏览器活动时间，不占用工作线程。"""

        now = self._clock()
        with self._lock:
            self._reap_expired_locked(now)
            lease = self._leases.get(session_id)
            if lease is None:
                raise SessionLeaseExpired("登录已失效或闲置超过 10 分钟，请重新登录")
            lease.last_activity = now
            return lease.user_id

    def is_active(self, session_id: str, *, user_id: int | None = None) -> bool:
        """只检查租约是否有效，不刷新空闲时间。"""

        with self._lock:
            self._reap_expired_locked(self._clock())
            lease = self._leases.get(session_id)
            return lease is not None and (user_id is None or lease.user_id == user_id)

    def release(self, session_id: str, *, user_id: int | None = None) -> bool:
        """退出登录或关闭页面时立即释放会话槽。"""

        with self._lock:
            lease = self._leases.get(session_id)
            if lease is None or (user_id is not None and lease.user_id != user_id):
                return False
            self._remove_lease_locked(session_id)
            return True

    def release_beacon(self, beacon_token: str) -> bool:
        """用最小权限回收凭据释放会话槽。"""

        with self._lock:
            session_id = self._beacon_sessions.get(self._digest_beacon(beacon_token))
            if session_id is None:
                return False
            self._remove_lease_locked(session_id)
            return True

    def submit(
        self,
        session_id: str,
        function: Callable[P, R],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> Future[R]:
        """把已认证会话的阻塞任务提交到工作线程池。"""

        with self._lock:
            lease = self._leases.get(session_id)
            if lease is None or lease.active_requests < 1:
                raise SessionLeaseExpired("登录已失效，请重新登录")
        return self._executor.submit(function, *args, **kwargs)

    def reap_expired(self) -> int:
        """回收所有空闲达到超时时间且没有在途请求的会话槽。"""

        with self._lock:
            return self._reap_expired_locked(self._clock())

    def snapshot(self) -> dict[str, int | float]:
        """返回线程池容量和当前租约数量，供 readiness 与 metrics 观测。"""

        with self._lock:
            self._reap_expired_locked(self._clock())
            active_sessions = len(self._leases)
            active_requests = sum(lease.active_requests for lease in self._leases.values())
        return {
            "max_workers": self.max_workers,
            "active_sessions": active_sessions,
            "active_requests": active_requests,
            "available_slots": self.max_workers - active_sessions,
            "idle_timeout_seconds": self.idle_timeout_seconds,
        }

    def shutdown(self) -> None:
        """等待已提交任务完成并关闭工作线程，供测试或进程托管方清理。"""

        self._executor.shutdown(wait=True, cancel_futures=False)

    def _reap_expired_locked(self, now: float) -> int:
        expired = [
            session_id
            for session_id, lease in self._leases.items()
            if lease.active_requests == 0
            and now - lease.last_activity >= self.idle_timeout_seconds
        ]
        for session_id in expired:
            self._remove_lease_locked(session_id)
        return len(expired)

    def _remove_lease_locked(self, session_id: str) -> None:
        lease = self._leases.pop(session_id)
        self._beacon_sessions.pop(lease.beacon_digest, None)

    @staticmethod
    def _digest_beacon(beacon_token: str) -> str:
        return hashlib.sha256(beacon_token.encode("utf-8")).hexdigest()
