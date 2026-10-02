"""Isolated HTTP verification with the configured real model and iWenCai.

Only the diagnostic harness is instrumented; no provider/model result is mocked.
The second scenario removes two fields from real first-scenario evidence to
exercise a reproducible technical-evidence gap. Production accounts and databases
are never read or modified. Reports exclude secrets, headers and model prompts.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
WORK = ROOT / ".tmp_flow/recovery_verification_20261002"
QUERY = ("个股研究：请研究贵州茅台600519的最新财报、估值和提价对业绩的影响。"
         "同时核对宏观流动性和白酒行业景气，资料缺失时自动补取，保留未验证的不确定性。")


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def recheck_saved(output):
    """Re-evaluate report predicates from preserved actual observations only."""
    report = json.loads(output.read_text(encoding="utf-8"))
    report.pop("market_closed_day", None)
    report["intraday_freshness_verified"] = False
    for scenario in report["scenarios"]:
        for check in report["checks"]:
            if check["name"] == scenario["name"] + ":real_model_success":
                check["passed"] = any(call.get("status") == "completed" for call in scenario["model_calls"])
            if check["name"] == scenario["name"] + ":real_HTTP_or_valid_public_cache":
                audit = scenario["data_acquisition"]
                check["passed"] = bool(scenario["observations"]["http_attempts"]
                    or audit["cached_capabilities"] or audit.get("recovery_cached_capabilities"))
    report["verification_passed"] = all(check["passed"] for check in report["checks"])
    report["predicate_correction"] = "Model telemetry uses status=completed, not a success boolean; no observations changed."
    report["limitations"] = [
        "问财为单一授权来源；不是独立多来源确认。", "测试未覆盖盘中实时变化；抓取时点不等于交易时点。",
        "第二场景仅删除真实证据中的两个字段构造缺项，没有注入虚构行情或评分。",
        "成功取得资料不代表所有评分维度齐备；最终 REVIEW 按真实结果保留。",
    ]
    write_json(output, report)
    print(json.dumps({"verification_passed": report["verification_passed"],
                      "failed_checks": [check for check in report["checks"] if not check["passed"]]}, ensure_ascii=False))
    return 0 if report["verification_passed"] else 1


def serve(port):
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    WORK.mkdir(parents=True, exist_ok=True)
    # These override only this fresh child process before application creation.
    os.environ["MYSQL_URL"] = "sqlite+pysqlite:///" + (WORK / "isolated.sqlite").as_posix()
    os.environ["WENCE_AUTH_SECRET"] = "local-verification-only-not-production-20261002"
    from backend.app import main
    from backend.app.services.evidence_coverage import missing_fields_by_agent
    import uvicorn

    observations = {"http_attempts": [], "capability_calls": [], "analysis_runs": []}
    run_count = 0
    if main.data_provider:
        async def observe(event):
            observations["http_attempts"].append({
                key: value for key, value in event.items() if key in {
                    "endpoint", "skill_id", "attempt", "status", "status_code", "fact_count",
                    "error_type", "duration_ms", "created_at",
                }
            })
        main.data_provider.call_observer = observe
    execute = main.research_pipeline._execute
    async def observed_execute(call):
        started = time.perf_counter()
        event = {"label": call.label, "method": call.method,
                 "phase": "recovery" if main.research_pipeline._cache_key(call)
                 in main.research_pipeline.cache.refreshing else "initial"}
        try:
            result = await execute(call)
            event.update(status="ok" if result else "empty", fact_count=len(result),
                         fields=sorted({fact.field for fact in result}),
                         source_ids=sorted({fact.source_id for fact in result}),
                         periods=sorted({fact.period for fact in result if fact.period}),
                         source_link_count=sum(bool(fact.source_url) for fact in result))
            return result
        except BaseException as exc:
            event.update(status="failed", error_type=type(exc).__name__)
            raise
        finally:
            event["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
            observations["capability_calls"].append(event)
    main.research_pipeline._execute = observed_execute
    run = main.coordinator.run
    async def observed_run(request, **kwargs):
        nonlocal run_count
        run_count += 1
        started = time.perf_counter()
        result = await run(request, **kwargs)
        observations["analysis_runs"].append({
            "run": run_count, "fact_count": len(request.facts),
            "missing_fields_by_agent": missing_fields_by_agent(request.facts, result.intent),
            "issue_codes": [issue.code for issue in result.cross_validation.issues],
            "compliance": result.compliance.status.value,
            "cross_validation": result.cross_validation.status.value,
            "agents": [{"agent_id": agent.agent_id, "status": agent.status.value,
                        "engine": agent.details.get("engine"), "score": agent.score,
                        "citation_count": len(agent.facts_used)} for agent in result.agent_results],
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        })
        return result
    main.coordinator.run = observed_run

    @main.app.get("/__verification/observations")
    async def observed():
        return observations

    @main.app.post("/__verification/reset")
    async def reset():
        nonlocal run_count
        run_count = 0
        for values in observations.values():
            values.clear()
        return {"status": "ok"}
    uvicorn.run(main.app, host="127.0.0.1", port=port, log_level="warning")


def verify(port, output):
    import httpx
    WORK.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{port}"
    # Never kill an existing listener: require a free port before launching.
    import socket
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(f"Verification port {port} is already occupied")
    report = {"generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
              "scope": "isolated_loopback_HTTP_real_model_real_iwencai_no_production_accounts",
              "intraday_freshness_verified": False,
              "capture_time_is_not_exchange_timestamp": True,
              "synthetic_market_values": False, "checks": [], "scenarios": []}
    child_log = (WORK / "server.log").open("w", encoding="utf-8")
    child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--serve", "--port", str(port)],
                             cwd=ROOT, stdout=child_log, stderr=subprocess.STDOUT,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        with httpx.Client(timeout=240, trust_env=False) as client:
            for _ in range(80):
                try:
                    if client.get(base + "/api/v1/health", timeout=1).status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                if child.poll() is not None:
                    raise RuntimeError("Isolated server exited before readiness")
                time.sleep(.25)
            health = client.get(base + "/api/v1/health")
            ready = client.get(base + "/api/v1/readiness")
            report["health"] = health.json()
            report["readiness"] = ready.json()
            report["checks"].extend([
                {"name": "HTTP_health", "passed": health.status_code == 200},
                {"name": "live_provider_configured", "passed": bool(ready.json().get("iwencai_skillhub_configured"))},
                {"name": "live_model_configured", "passed": bool(ready.json().get("third_party_llm_configured"))},
                {"name": "protected_history_requires_login", "passed": client.get(base + "/api/v1/history").status_code == 401},
                {"name": "protected_admin_requires_login", "passed": client.get(base + "/api/v1/admin/users").status_code == 401},
            ])
            payload = {"query": QUERY, "research_direction": "个股研究", "research_mode": "fast",
                       "profile": {"user_id": "ISOLATED_LIVE_RECOVERY_TEST", "confirmed": True, "risk_level": "R3"},
                       "auto_fetch": True, "facts": []}
            first_advice = None
            for scenario in ("natural_missing_evidence", "controlled_technical_gap_from_real_evidence", "warm_repeat"):
                if scenario.startswith("controlled"):
                    if not first_advice or not first_advice.get("facts"):
                        report["scenarios"].append({"name": scenario, "status": "skipped_no_real_evidence"})
                        break
                    payload["facts"] = [fact for fact in first_advice["facts"]
                                        if fact["field"] not in {"change", "technical_score"}]
                elif scenario == "warm_repeat":
                    payload["facts"] = []
                client.post(base + "/__verification/reset").raise_for_status()
                started = time.perf_counter()
                response = client.post(base + "/api/v1/portfolio/analyze", json=payload)
                elapsed = round(time.perf_counter() - started, 3)
                response.raise_for_status()
                advice = response.json()
                observations = client.get(base + "/__verification/observations").json()
                if first_advice is None:
                    first_advice = advice
                audit = advice["data_acquisition"]
                ids = {fact["fact_id"] for fact in advice["facts"]}
                cited = set(advice["evidence"])
                for agent in advice["agent_results"]:
                    cited.update(agent["facts_used"])
                snapshots = observations["analysis_runs"]
                gaps_before = audit.get("recovery_missing_fields_before") or (
                    snapshots[0]["missing_fields_by_agent"] if snapshots else {})
                gaps_after = audit["missing_fields_by_agent"]
                count_before = sum(len(fields) for fields in gaps_before.values())
                count_after = sum(len(fields) for fields in gaps_after.values())
                if audit["recovery_successful_capabilities"]:
                    effect = "gap_reduced" if count_after < count_before else "facts_added_gaps_remain"
                else:
                    effect = "no_usable_recovery_facts" if audit["recovery_rounds"] else "recovery_not_triggered"
                item = {"name": scenario, "http_status": response.status_code, "elapsed_seconds": elapsed,
                        "effect": effect, "data_acquisition": audit, "observations": observations,
                        "gaps_before": gaps_before, "gaps_after": gaps_after,
                        "gap_count_before": count_before, "gap_count_after": count_after,
                        "technical_gap_recovered": (scenario.startswith("controlled")
                            and "technical_score" in gaps_before.get("security", [])
                            and "technical_score" not in gaps_after.get("security", [])
                            and any(fact["field"] == "technical_score" for fact in advice["facts"])),
                        "fact_count": len(advice["facts"]), "citation_count": len(cited),
                        "citations_exist": cited <= ids, "final_status": advice["compliance"]["status"],
                        "final_issue_codes": [issue["code"] for issue in advice["cross_validation"]["issues"]],
                        "timings_ms": advice["timings_ms"], "model_calls": advice["model_calls"]}
                report["scenarios"].append(item)
                write_json(output.with_name(output.stem + "_" + scenario + "_advice.json"), advice)
                report["checks"].extend([
                    {"name": scenario + ":bounded_round", "passed": audit["recovery_rounds"] <= 1},
                    {"name": scenario + ":citation_integrity", "passed": cited <= ids},
                    {"name": scenario + ":full_reanalysis", "passed": not audit["recovery_reanalyzed"] or len(snapshots) == 2},
                    {"name": scenario + ":known_gaps_analyzed_once", "passed":
                     audit.get("recovery_phase") != "before_analysis" or len(snapshots) == 1},
                    {"name": scenario + ":real_HTTP_or_valid_public_cache", "passed":
                     bool(observations["http_attempts"]) or bool(audit["cached_capabilities"])
                     or bool(audit.get("recovery_cached_capabilities"))},
                    {"name": scenario + ":real_model_success", "passed": any(call.get("status") == "completed" for call in advice["model_calls"])},
                ])
                if gaps_after or advice["cross_validation"]["status"] != "PASS":
                    report["checks"].append({"name": scenario + ":unresolved_evidence_stays_review",
                                             "passed": advice["compliance"]["status"] != "PASS"})
                print(json.dumps({"scenario": scenario, "effect": effect, "seconds": elapsed,
                                  "recovery_success": audit["recovery_successful_capabilities"],
                                  "recovery_empty": audit["recovery_empty_capabilities"],
                                  "gaps": [count_before, count_after], "status": item["final_status"],
                                  "technical_gap_recovered": item["technical_gap_recovered"]}, ensure_ascii=False), flush=True)
                write_json(output, report)
            report["verification_passed"] = all(check["passed"] for check in report["checks"])
            report["all_required_dimensions_recovered"] = bool(report["scenarios"]) and all(
                scenario.get("gap_count_after", 1) == 0 for scenario in report["scenarios"])
            report["limitations"] = [
                "问财为单一授权来源；不是独立多来源确认。", "测试未覆盖盘中实时变化；抓取时点不等于交易时点。",
                "第二场景仅删除真实证据中的两个字段构造缺项，没有注入虚构行情或评分。",
                "成功取得资料不代表所有评分维度齐备；最终 REVIEW 按真实结果保留。",
            ]
    finally:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)
        child_log.close()
        write_json(output, report)
    print(json.dumps({"report": str(output), "verification_passed": report.get("verification_passed"),
                      "all_required_dimensions_recovered": report.get("all_required_dimensions_recovered")}, ensure_ascii=False))
    return 0 if report.get("verification_passed") else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--recheck-saved", action="store_true", help="Recheck existing real observations without more external requests")
    parser.add_argument("--port", type=int, default=8013)
    parser.add_argument("--output", type=Path, default=ROOT / "deliverables/recovery_live_20261002.json")
    args = parser.parse_args()
    if args.serve:
        serve(args.port)
    elif args.recheck_saved:
        raise SystemExit(recheck_saved(args.output))
    else:
        raise SystemExit(verify(args.port, args.output))
