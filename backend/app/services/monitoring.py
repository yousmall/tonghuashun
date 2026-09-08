"""轻量运行指标，用于容量验证和可用性观测。

指标只保存在当前进程，适合演示和探针；生产部署应由 Prometheus/OpenTelemetry
等外部系统汇总多个实例并计算长期 SLA。
"""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timezone
from time import monotonic
from typing import Any


class ServiceMetrics:
    def __init__(self, sample_size: int = 10_000) -> None:
        self.started_at = datetime.now(timezone.utc)
        self._started_monotonic = monotonic()
        self.request_count = 0
        self.failure_count = 0
        self.latencies_ms: deque[float] = deque(maxlen=sample_size)

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
        }
