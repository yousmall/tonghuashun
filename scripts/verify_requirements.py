"""隔离验收：合成事实/语义、100 个真实租约和 SQLite 保存；不调用外部服务。"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from backend.app import main
from backend.app.agents.coordinator import CoordinatorAgent, basic_compliance_check, verify_facts
from backend.app.agents.rule_agents import make_rule_agents
from backend.app.auth import hash_password
from backend.app.database import Database
from backend.app.models import FactRecord, OrchestrationRequest
from backend.app.semantic import SemanticService
from backend.app.services.monitoring import ServiceMetrics
from backend.app.services.research import AutomatedResearchPipeline
from backend.app.session_pool import SessionThreadPool


class AcceptanceLLM:
    """显式固定语义夹具，用于验证编排；不是第三方模型测试。"""
    async def complete_json(self, *, system, payload):
        if payload["required_schema"]["title"] == "OutputReview":
            return {"confidence": 0.95, "risk_rules": [], "conflicting_agents": [], "reason": "合成验收复核"}
        query = payload["query"]
        cases = {"市场演示": "market_analysis", "行业演示": "industry_analysis",
                 "个股演示": "security_research", "基金演示": "fund_screening"}
        return {"intent": cases.get(query, "portfolio_review"), "confidence": 0.95,
                "risk_rules": ["NO_RETURN_PROMISE"] if query == "保证收益演示" else [],
                "reason": "固定合成语义，仅供功能验收"}


def sample_request(query="组合演示"):
    values = {"growth_score": 55, "inflation_score": 50, "liquidity_score": 60,
              "policy_score": 55, "risk_appetite_score": 50, "prosperity_score": 55,
              "valuation_score": 55, "capital_flow_score": 55, "crowding_score": 45,
              "fundamental_score": 60, "technical_score": 55, "event_score": 55,
              "governance_score": 60, "fund_risk_level": "R2", "fund_score": 65,
              "fee_rate": 0.005, "tracking_error": 0.01,
              "weight": 0.15}
    return OrchestrationRequest(query=query, auto_fetch=False,
        profile={"risk_level": "R3", "confirmed": True, "horizon_months": 24,
                 "max_drawdown": 0.15, "liquidity_need": "中"},
        facts=[FactRecord(fact_id="DEMO-" + field, entity="合成验收ETF", field=field, value=value,
            snapshot_time=datetime.now(timezone.utc), source_id="SYNTHETIC_ACCEPTANCE_ONLY", quality=0.95)
            for field, value in values.items()])


async def run(output: Path, users: int):
    coordinator = CoordinatorAgent(make_rule_agents(), verify_facts, basic_compliance_check,
                                   semantic=SemanticService(AcceptanceLLM()))
    samples = []
    for title in ("市场演示", "行业演示", "个股演示", "基金演示", "组合演示", "保证收益演示"):
        started = perf_counter()
        request = sample_request(title)
        advice = await coordinator.run(request)
        samples.append({"name": title, "advice": advice.model_copy(update={
            "facts": request.facts,
            "timings_ms": {"coordinator_only": round((perf_counter() - started) * 1000, 2)},
        }).model_dump(mode="json")})
    stale = sample_request()
    stale.facts = [fact.model_copy(update={"snapshot_time": datetime.now(timezone.utc) - timedelta(days=100)}) for fact in stale.facts]
    samples.append({"name": "过期证据演示", "advice": (await coordinator.run(stale)).model_copy(update={"facts": stale.facts}).model_dump(mode="json")})
    conflict = sample_request()
    conflict.facts.append(conflict.facts[-1].model_copy(update={"fact_id": "DEMO-CONFLICT", "value": 0.7}))
    samples.append({"name": "同项冲突演示", "advice": (await coordinator.run(conflict)).model_copy(update={"facts": conflict.facts}).model_dump(mode="json")})

    originals = {key: getattr(main, key) for key in ("database", "session_thread_pool", "coordinator", "research_pipeline", "service_metrics", "llm_enabled")}
    pool = SessionThreadPool(max_workers=max(100, users))
    with TemporaryDirectory(prefix="wence-acceptance-") as directory:
        database = Database("sqlite+pysqlite:///" + (Path(directory) / "acceptance.sqlite").as_posix(),
                            "isolated-acceptance-secret-not-production-20261001")
        database.initialize()
        try:
            main.database, main.session_thread_pool = database, pool
            main.coordinator, main.research_pipeline = coordinator, AutomatedResearchPipeline(None)
            main.service_metrics, main.llm_enabled = ServiceMetrics(), False
            headers = []
            password_hash = hash_password("SyntheticAcceptancePassword")
            for index in range(users):
                user = database.create_user(f"acceptance-{index:03d}", password_hash)
                profile = sample_request().profile.model_dump(mode="json")
                database.save_profile(int(user["id"]), profile, 1)
                headers.append({"Authorization": "Bearer " + main.auth_response_for(user).access_token})
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://acceptance") as client:
                start = asyncio.Event()
                async def one(index):
                    await start.wait()
                    request = sample_request()
                    request.conversation_id = f"acceptance-conversation-{index}"
                    started = perf_counter()
                    response = await client.post("/api/v1/portfolio/analyze", json=request.model_dump(mode="json"), headers=headers[index])
                    return response.status_code, round((perf_counter() - started) * 1000, 2), response.json().get("compliance", {}).get("status")
                tasks = [asyncio.create_task(one(index)) for index in range(users)]
                started = perf_counter()
                start.set()
                results = await asyncio.gather(*tasks)
                wall_ms = round((perf_counter() - started) * 1000, 2)
            latencies = [result[1] for result in results]
            histories = [database.list_conversations(index + 1) for index in range(users)]
            load = {"users": users, "http_200_count": sum(r[0] == 200 for r in results),
                    "business_outcomes": {status: sum(r[2] == status for r in results) for status in {r[2] for r in results}},
                    "wall_ms": wall_ms, "p50_ms": ServiceMetrics._percentile(latencies, 0.50),
                    "p95_ms": ServiceMetrics._percentile(latencies, 0.95), "max_ms": max(latencies),
                    "all_within_3_seconds": max(latencies) <= 3000,
                    "history_isolation_passed": all(len(rows) == 1 and rows[0]["id"] == f"acceptance-conversation-{index}" for index, rows in enumerate(histories)),
                    "metrics": main.service_metrics.snapshot()["analysis"],
                    "scope": "isolated_ASGI_synthetic_semantics_rule_agents_SQLite_auth_profile_history_not_production"}
        finally:
            pool.shutdown()
            database.engine.dispose()
            for key, value in originals.items():
                setattr(main, key, value)
    report = {"generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
              "synthetic_data_only": True, "external_calls": 0, "load": load, "samples": samples,
              "limitations": ["合成语义与合成事实，不证明真实模型质量或行情准确性。",
                              "ASGI 无公网传输；SQLite 不是生产 MySQL；不证明真实环境 100 用户吞吐。",
                              "3秒以完整结果为准；99.9%可用性须长期生产观测。"]}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(output), "load": {key: value for key, value in load.items() if key != "metrics"}}, ensure_ascii=False))
    return 0 if load["http_200_count"] == users and load["history_isolation_passed"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "deliverables/requirements_acceptance.json")
    parser.add_argument("--users", type=int, choices=range(1, 201), default=100)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.output, args.users)))
