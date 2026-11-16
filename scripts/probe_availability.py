"""持续采样应用健康/就绪接口并保存证据；采样成功率不等同于完整系统 SLA。"""
import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter

import httpx

ROOT = Path(__file__).resolve().parents[1]


async def run(args):
    started = perf_counter()
    sample_count = failure_count = 0
    started_at = datetime.now(timezone.utc).isoformat()
    run_id = started_at
    previous_sample = started
    largest_gap_seconds = 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # 每次独立运行覆盖摘要；原始采样按次追加，异常只保留类型。
    journal = args.output.with_suffix(".jsonl")
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        while True:
            now = perf_counter()
            largest_gap_seconds = max(largest_gap_seconds, now-previous_sample)
            previous_sample = now
            sample = {"time": datetime.now(timezone.utc).isoformat(), "run_id": run_id}
            try:
                health, ready = await asyncio.gather(client.get(args.url.rstrip("/") + "/api/v1/health"),
                                                     client.get(args.url.rstrip("/") + "/api/v1/readyz"))
                body = ready.json()
                sample.update(health_status=health.status_code, readiness_status=ready.status_code,
                              database_reachable=body.get("database_reachable"))
                sample["success"] = (health.status_code == 200 and health.json().get("status") == "ok"
                                      and ready.status_code == 200 and body.get("status") == "ready"
                                      and body.get("database_reachable") is True)
                if args.frontend_url:
                    front = await client.get(args.frontend_url.rstrip("/") + "/_stcore/health")
                    sample["frontend_status"] = front.status_code
                    sample["success"] = sample["success"] and front.status_code == 200 and front.text.strip() == "ok"
            except (httpx.HTTPError, ValueError) as exc:
                sample.update(success=False, error_type=type(exc).__name__)
            sample["elapsed_ms"] = round((perf_counter() - now) * 1000, 2)
            sample_count += 1
            failure_count += not sample["success"]
            with journal.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(sample, ensure_ascii=False) + "\n")
            report = {"run_id":run_id,"started_at":started_at,"last_sample_at":sample["time"],
                "expected_end_at":(datetime.fromisoformat(started_at)+timedelta(seconds=args.duration)).isoformat(),
                "elapsed_seconds":round(perf_counter()-started,2),"target_duration_seconds":args.duration,
                "interval_seconds":args.interval,"samples":sample_count,"failures":failure_count,
                "observed_probe_success_rate":(sample_count-failure_count)/sample_count,
                "availability_target":.999,"production_sla_verified":False,
                "observation_complete":perf_counter()-started >= args.duration,
                "frontend_included":bool(args.frontend_url),
                "largest_sampling_gap_seconds":round(largest_gap_seconds,2),
                "sampling_gaps_detected":largest_gap_seconds > max(30,args.interval*3),
                "scope":"sampled_API_live_database_and_optional_frontend_not_real_model_data_consultation_SLA",
                "journal":str(journal),"last_sample":sample}
            temporary = args.output.with_suffix(".tmp")
            temporary.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
            temporary.replace(args.output)
            remaining = args.duration - (perf_counter() - started)
            if remaining <= 0:
                break
            await asyncio.sleep(min(args.interval, remaining))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--frontend-url", default="")
    parser.add_argument("--output", type=Path, default=ROOT / "deliverables/requirements_availability.json")
    args = parser.parse_args()
    if args.duration <= 0 or args.interval <= 0:
        parser.error("duration 和 interval 必须大于0")
    asyncio.run(run(args))
