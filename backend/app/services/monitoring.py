"""轻量运行指标，用于容量验证和可用性观测。

指标只保存在当前进程，适合演示和探针；生产部署应由 Prometheus/OpenTelemetry
等外部系统汇总多个实例并计算长期 SLA。
"""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timezone
from time import monotonic
from time import perf_counter
from typing import Any


class ServiceMetrics:
    def __init__(self, sample_size: int = 10_000) -> None:
        self.started_at = datetime.now(timezone.utc)
        self._started_monotonic = monotonic()
        self.request_count = 0
        self.failure_count = 0
        self.latencies_ms: deque[float] = deque(maxlen=sample_size)
        self.analysis_latencies_ms: deque[float] = deque(maxlen=sample_size)
        self.analysis_count = 0
        self.analysis_failure_count = 0
        self.analysis_within_target_count = 0
        self.analysis_outcomes: dict[str, int] = {}

    def record_analysis(self, elapsed_ms: float, outcome: str) -> None:
        """仅统计完整分析；REVIEW/BLOCK 与异常分开，不用健康检查稀释延迟。"""
        self.analysis_count += 1
        self.analysis_latencies_ms.append(elapsed_ms)
        self.analysis_outcomes[outcome] = self.analysis_outcomes.get(outcome, 0) + 1
        if outcome == "ERROR":
            self.analysis_failure_count += 1
        elif elapsed_ms <= 3_000:
            self.analysis_within_target_count += 1

    def record(self, status_code: int, elapsed_ms: float) -> None:
        self.request_count += 1
        if status_code >= 500:
            self.failure_count += 1
        self.latencies_ms.append(elapsed_ms)

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, math.ceil(percentile * len(ordered)) - 1))
        return round(ordered[index], 2)

    def snapshot(self) -> dict[str, Any]:
        values = list(self.latencies_ms)
        successful = self.request_count - self.failure_count
        return {
            "started_at": self.started_at.isoformat(),
            "uptime_seconds": round(monotonic() - self._started_monotonic, 2),
            "request_count": self.request_count,
            "failure_count": self.failure_count,
            "observed_success_rate": round(successful / self.request_count, 6) if self.request_count else None,
            "latency_sample_count": len(values),
            "p50_latency_ms": self._percentile(values, 0.50),
            "p95_latency_ms": self._percentile(values, 0.95),
            "p99_latency_ms": self._percentile(values, 0.99),
            "availability_target": 0.999,
            "latency_target_ms": 3_000,
            "scope": "single_process_observation_not_production_sla",
            "analysis": {
                "count": self.analysis_count,
                "failure_count": self.analysis_failure_count,
                "outcomes": dict(self.analysis_outcomes),
                "within_3_seconds_count": self.analysis_within_target_count,
                "latency_sample_count": len(self.analysis_latencies_ms),
                "p50_latency_ms": self._percentile(list(self.analysis_latencies_ms), 0.50),
                "p95_latency_ms": self._percentile(list(self.analysis_latencies_ms), 0.95),
                "p99_latency_ms": self._percentile(list(self.analysis_latencies_ms), 0.99),
                "max_latency_ms": round(max(self.analysis_latencies_ms), 2) if self.analysis_latencies_ms else None,
                "scope": "analysis_handler_including_history_save_excluding_auth_and_transport",
            },
        }


class RequestMetricsMiddleware:
    """在最后一块响应正文完成后记录延迟；流式建连不等于完成。"""

    def __init__(self, app, metrics: ServiceMetrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = perf_counter()
        status = 500
        completed = False

        async def observed_send(message):
            nonlocal status, completed
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                completed = True

        try:
            await self.app(scope, receive, observed_send)
        finally:
            self.metrics.record(status if completed else 500, (perf_counter() - started) * 1_000)
