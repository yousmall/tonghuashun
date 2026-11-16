"""Real-model/provider HTTP probe with isolated accounts and two API workers.

Reports actual completion, overload and P95 separately. Never treats HTTP 200,
streaming progress, or synthetic concurrency as a completed research result.
"""
from __future__ import annotations
import argparse
import asyncio
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def percentile(values, p):
    return sorted(values)[max(0, math.ceil(len(values) * p) - 1)] if values else None


async def probe(base, users, rounds, duration, offline=False, mode="deep", output=None, loaded_data=False):
    import httpx
    from backend.app.risk_questionnaire import QUESTIONS, QUESTIONNAIRE_VERSION
    async with httpx.AsyncClient(timeout=1900, trust_env=False, limits=httpx.Limits(max_connections=max(120, users+5))) as client:
        ready = (await client.get(base + "/api/v1/readiness")).json()
        if not ready["third_party_llm_configured"] or not ready["iwencai_skillhub_configured"]:
            return {"readiness": ready, "status": "blocked_external_configuration"}
        accounts = []
        loaded_facts = []
        preparation_seconds = 0
        preparation_attempts = []
        if loaded_data:
            preparation_started = time.perf_counter()
            for attempt in range(3):
                fetched = await client.post(base + "/api/v1/data/fetch", json={"kind":"quote","target":"600519","filters":{}})
                fetched.raise_for_status()
                body = fetched.json()
                loaded_facts = body.get("facts", [])
                preparation_attempts.append({"status":body.get("status"),"fact_count":len(loaded_facts)})
                if loaded_facts: break
                await asyncio.sleep(.5)
            preparation_seconds = round(time.perf_counter()-preparation_started,4)
            if not loaded_facts:
                raise RuntimeError("Real preloaded quote unavailable")
        run = uuid4().hex[:8]
        for index in range(users):
            response = await client.post(base + "/api/v1/auth/register", json={
                "username": f"capacity_{run}_{index}", "password": "Isolated-capacity-test-2026!"})
            response.raise_for_status()
            headers = {"Authorization": "Bearer " + response.json()["access_token"]}
            assessment = await client.post(base + "/api/v1/profile/assess", headers=headers, json={
                "questionnaire_version": QUESTIONNAIRE_VERSION,
                "risk_answers": dict(zip((q.id for q in QUESTIONS), "ACABDCCACCBBCCBCCDBCADAECDC", strict=True))})
            assessment.raise_for_status()
            confirm = await client.post(base + "/api/v1/profile/confirm", headers=headers, json={"profile": assessment.json()["profile"]})
            confirm.raise_for_status()
            accounts.append((headers, confirm.json()))
        samples = []
        async def request(account, phase, index):
            headers, profile = account
            started = time.perf_counter()
            response = await client.post(base + "/api/v1/portfolio/analyze", headers=headers, json={
                "query": "请研究贵州茅台600519的最新财报、估值及风险，核对宏观和白酒行业，保留资料缺项。",
                "research_direction": "个股研究", "research_mode": mode, "auto_fetch": not loaded_data, "facts": loaded_facts, "profile": profile})
            row = {"phase": phase, "user_index": index, "http_status": response.status_code,
                   "seconds": round(time.perf_counter() - started, 4)}
            if response.status_code == 200:
                advice = response.json()
                ids = {f["fact_id"] for f in advice["facts"]}
                cited = set(advice["evidence"])
                for agent in advice["agent_results"]:
                    cited.update(agent["facts_used"])
                row.update(status=advice["compliance"]["status"],
                    research_returned=bool(advice["agent_results"]),
                    response_scope=advice.get("response_scope"),
                    all_plan_nodes_completed=bool(advice["task_plan"]["nodes"]) and all(n["status"] == "completed" for n in advice["task_plan"]["nodes"]),
                    complete_audited_advice=advice["compliance"]["status"] == "PASS" and advice.get("cross_validation", {}).get("status") == "PASS" and bool(advice.get("evidence")) and bool(advice["agent_results"]) and all(n["status"] == "completed" for n in advice["task_plan"]["nodes"]) and all((a.get("details") or {}).get("engine") == "third_party_llm" for a in advice["agent_results"]),
                    model_completed=sum(call["status"] == "completed" and not call.get("cache_hit") for call in advice["model_calls"]),
                    specialist_llm_completed=sum((agent.get("details") or {}).get("engine") == "third_party_llm" for agent in advice["agent_results"]),
                    model_cache_hits=sum(bool(call.get("cache_hit")) for call in advice["model_calls"]),
                    model_cost=advice.get("model_cost"), timings_ms=advice["timings_ms"],
                    missing_fields=advice["data_acquisition"]["missing_fields_by_agent"],
                    capability_errors=advice["data_acquisition"].get("capability_errors"),
                    citations_exist=cited <= ids, citation_count=len(cited))
            samples.append(row)
            if output:
                with output.with_suffix(".jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(json.dumps({k: row[k] for k in ("phase", "user_index", "http_status", "seconds")}), flush=True)
        await request(accounts[0], "cold", 0)
        await request(accounts[0], "warm", 0)
        deadline = time.perf_counter() + duration
        load_started = time.perf_counter()
        round_index = 0
        while round_index < rounds or (duration and time.perf_counter() < deadline):
            await asyncio.gather(*(request(account, f"load_{round_index}", index) for index, account in enumerate(accounts)))
            round_index += 1
            if duration and time.perf_counter() < deadline:
                await asyncio.sleep(.5)
        load_window_seconds = round(time.perf_counter()-load_started,3)
        # Verify histories after the consultation burst, not as an additional
        # simultaneous 200-request history workload while answers are still running.
        history_gate = asyncio.Semaphore(8)
        async def verify_history(index, account):
            async with history_gate:
                headers, _ = account
                rows = [r for r in samples if r["user_index"] == index and r["phase"].startswith("load_")]
                row = rows[-1] if rows else None
                if row is None or row["http_status"] != 200: return
                for attempt in range(50):
                    history = await client.get(base+"/api/v1/history", headers=headers)
                    history_rows = history.json() if history.status_code == 200 else []
                    if history_rows: break
                    await asyncio.sleep(.1)
                row["history_http_status"] = history.status_code
                if not history_rows:
                    row["history_citations_complete"] = False
                    return
                row["history_id"] = history_rows[0]["id"]
                detail = await client.get(base+"/api/v1/history/"+row["history_id"],headers=headers)
                row["history_detail_http_status"] = detail.status_code
                latest = next((m["payload"] for m in reversed(detail.json().get("messages",[])) if m["role"]=="assistant"),{}) or {}
                from backend.app.services.history import used_fact_ids_of
                row["history_citations_complete"] = used_fact_ids_of(latest) <= {f["fact_id"] for f in latest.get("facts",[])}
        await asyncio.gather(*(verify_history(i,a) for i,a in enumerate(accounts)))
        load_rows = [r for r in samples if r["phase"].startswith("load_")]
        history_ids = [r["history_id"] for r in load_rows if r.get("history_id")]
        cross_account_status = None
        if users > 1 and load_rows[0].get("history_id"):
            owner = load_rows[0]["user_index"]
            other = accounts[(owner+1)%users][0]
            cross_account_status = (await client.get(base+"/api/v1/history/"+load_rows[0]["history_id"],headers=other)).status_code
        completed = [row for row in samples if row.get("research_returned")]
        latencies = [row["seconds"] for row in completed]
        return {"status": "measured", "readiness": ready, "users": users, "rounds": round_index,
            "research_mode": mode, "deep_three_second_requirement": False,
            "real_quote_preloaded": loaded_data and not offline,
            "preloaded_fact_count": len(loaded_facts),
            "data_preparation_seconds": preparation_seconds, "data_preparation_attempts": preparation_attempts,
            "history_verification_after_all_responses": True,
            "all_load_histories_complete": all(r.get("history_citations_complete") for r in load_rows),
            "distinct_load_histories": len(set(history_ids)), "cross_account_history_status":cross_account_status,
            "completed_audited_advice": sum(bool(row.get("complete_audited_advice")) for row in samples),
            "load_http_successes": sum(row["http_status"] == 200 for row in samples if row["phase"].startswith("load_")),
            "load_audited_successes": sum(bool(row.get("complete_audited_advice")) for row in samples if row["phase"].startswith("load_")),
            "external_services": ("synthetic_transport_over_real_HTTP" if offline else
                "real_iwencai_snapshot_local_rules_no_model" if mode == "lite" else "real_model_and_iwencai"),
            "target_window_seconds": duration, "measured_load_window_seconds": load_window_seconds, "samples": samples,
            "http_429": sum(row["http_status"] == 429 for row in samples),
            "completed_research": len(completed), "http_200_without_research": sum(row["http_status"] == 200 and not row.get("research_returned") for row in samples),
            "completed_p50_seconds": percentile(latencies, .5), "completed_p95_seconds": percentile(latencies, .95),
            "complete_response_3s_met": bool(latencies) and all(t <= 3 for t in latencies),
            "real_production_capacity_verified": False,
            "limitations": ["Same-host two-worker isolated API, not production deployment.",
                "Capture time does not establish intraday exchange freshness.",
                "HTTP overloads and clarifications excluded from completed-research latency."]}


def run(args):
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    work = ROOT / ".tmp_flow" / ("capacity_" + uuid4().hex[:8])
    work.mkdir(parents=True)
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", args.port)) == 0:
            raise RuntimeError("Probe port already occupied")
    url = "sqlite+pysqlite:///" + (work / "accounts.sqlite").as_posix()
    secret = "isolated-capacity-only-not-production-secret-2026"
    from backend.app.database import Database
    admin_engine = None
    schema_name = None
    if args.database == "mysql":
        from sqlalchemy import text
        source = Database.from_env()
        if not source.configured or not source.engine.url.drivername.startswith("mysql"):
            raise RuntimeError("Configured MySQL is required for this probe")
        admin_engine = source.engine
        schema_name = "wence_qa_capacity_" + uuid4().hex[:12]
        with admin_engine.begin() as connection:
            connection.execute(text(f"CREATE DATABASE `{schema_name}` CHARACTER SET utf8mb4"))
        url = admin_engine.url.set(database=schema_name).render_as_string(hide_password=False)
    process = log = None
    try:
        database = Database(url, secret)
        database.initialize()
        database.engine.dispose()
        env = dict(os.environ, MYSQL_URL=url, WENCE_AUTH_SECRET=secret,
            WENCE_RESEARCH_CACHE_PATH=str(work / "public.sqlite"),
            WENCE_SESSION_STORE_PATH=str(work / "sessions.sqlite"), PYTHONIOENCODING="utf-8")
        if args.offline:
            env.update(DEEPSEEK_API_KEY="", IWENCAI_API_KEY="")
        log = (work / "api.log").open("w", encoding="utf-8")
        module = "scripts.capacity_fixture:app" if args.offline else "backend.app.main:app"
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", module, "--host", "127.0.0.1",
            "--port", str(args.port), "--workers", "2", "--log-level", "warning"], cwd=ROOT, env=env,
            stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        base = f"http://127.0.0.1:{args.port}"
        import httpx
        for _ in range(100):
            try:
                if httpx.get(base + "/api/v1/health", timeout=1, trust_env=False).status_code == 200:
                    break
            except httpx.RequestError:
                pass
            time.sleep(.2)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        result = asyncio.run(probe(base, args.users, args.rounds, args.duration, args.offline, args.mode, args.output, args.loaded_data))
        result["generated_at"] = datetime.now(timezone.utc).isoformat()
        result["database"] = args.database
        result["isolated_database"] = True
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in result.items() if key not in {"samples", "readiness"}}, ensure_ascii=False))
    finally:
        if process is not None and os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
        elif process is not None:
            process.terminate()
        if process is not None:
            process.wait(timeout=10)
        if log is not None:
            log.close()
        if admin_engine is not None:
            # Only the fresh schema created above; never the configured user database.
            if not schema_name or not schema_name.startswith("wence_qa_capacity_") or len(schema_name) != 30:
                raise RuntimeError("Refusing unexpected test schema cleanup")
            from sqlalchemy import text
            with admin_engine.begin() as connection:
                connection.execute(text(f"DROP DATABASE `{schema_name}`"))
            admin_engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users", type=int, default=4)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--duration", type=float, default=0, help="Repeat until this many seconds; reports cannot imply a longer window")
    parser.add_argument("--port", type=int, default=8017)
    parser.add_argument("--offline", action="store_true", help="Explicit synthetic upstream transports; never a production capacity claim")
    parser.add_argument("--mode", choices=("deep", "fast", "lite"), default="deep")
    parser.add_argument("--loaded-data", action="store_true", help="Fetch a real quote once, then consult on the loaded snapshot; disclose preparation separately")
    parser.add_argument("--database", choices=("sqlite","mysql"), default="sqlite", help="MySQL creates and cleans up a unique QA schema; never uses the existing user schema")
    parser.add_argument("--output", type=Path, default=ROOT / "deliverables/research_capacity_20261002.json")
    args = parser.parse_args()
    if args.users < 1 or args.rounds < 1 or args.duration < 0:
        parser.error("users and rounds must be positive; duration must be nonnegative")
    run(args)
