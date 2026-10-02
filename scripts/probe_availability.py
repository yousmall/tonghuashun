"""持续采样应用健康/就绪接口并保存证据；采样成功率不等同于完整系统 SLA。"""
import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import httpx

ROOT = Path(__file__).resolve().parents[1]


async def run(args):
    started = perf_counter()
    samples = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # 每次独立运行覆盖摘要；原始采样按次追加，异常只保留类型。
    journal = args.output.with_suffix(".jsonl")
    async with httpx.AsyncClient(timeout=5) as client:
        while True:
            now = perf_counter()
            sample = {"time": datetime.now(timezone.utc).isoformat()}
            try:
                health, ready = await asyncio.gather(client.get(args.url.rstrip("/") + "/api/v1/health"),
                                                     client.get(args.url.rstrip("/") + "/api/v1/readiness"))
                body = ready.json()
                sample.update(health_status=health.status_code, readiness_status=ready.status_code,
                              mode=body.get("mode"), mysql_ready=body.get("mysql_ready"))
                sample["success"] = (health.status_code == 200 and health.json().get("status") == "ok"
                                      and ready.status_code == 200 and body.get("status") == "ready"
                                      and (not body.get("mysql_configured") or body.get("mysql_ready") is True))
            except (httpx.HTTPError, ValueError) as exc:
                sample.update(success=False, error_type=type(exc).__name__)
            sample["elapsed_ms"] = round((perf_counter() - now) * 1000, 2)
            samples.append(sample)
            with journal.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(sample, ensure_ascii=False) + "\n")
            remaining = args.duration - (perf_counter() - started)
            if remaining <= 0:
                break
            await asyncio.sleep(min(args.interval, remaining))
    report = {"started_at": samples[0]["time"], "ended_at": samples[-1]["time"],
              "duration_seconds": round(perf_counter() - started, 2), "interval_seconds": args.interval,
              "samples": len(samples), "failures": sum(not sample["success"] for sample in samples),
              "observed_probe_success_rate": sum(sample["success"] for sample in samples) / len(samples),
              "availability_target": 0.999, "production_sla_verified": False,
              "scope": "application_health_readiness_samples_not_frontend_or_external_dependency_availability",
              "journal": str(journal)}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--output", type=Path, default=ROOT / "deliverables/requirements_availability.json")
    args = parser.parse_args()
    if args.duration <= 0 or args.interval <= 0:
        parser.error("duration 和 interval 必须大于0")
    asyncio.run(run(args))
