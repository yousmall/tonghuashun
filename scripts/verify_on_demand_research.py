"""只读问财探测；--full 使用合成画像测试真实自动补取，只输出安全摘要。"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone
from time import perf_counter

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from backend.app.data_provider import IwencaiSkillHubProvider
from backend.app.services.provider_errors import failure_summary


async def probe(data_provider, target, timeout):
    if data_provider is None:
        return {"provider_configured": False}
    results = await asyncio.gather(*(asyncio.wait_for(getattr(data_provider, method)(target), timeout=timeout)
                                    for method in ("get_quote", "get_institutional_research")), return_exceptions=True)
    report = {"provider_configured": True, "target": target, "probe_timeout_seconds": timeout, "capabilities": {}}
    for name, result in zip(("quote", "institutional_research"), results, strict=True):
        if isinstance(result, BaseException):
            report["capabilities"][name] = {"status": "failed", **failure_summary(result)}
        else:
            wanted = "close_price" if name == "quote" else "target_price"
            facts = [f for f in result if f.field == wanted]
            report["capabilities"][name] = {"status": "ok" if facts else "empty", "fact_count": len(facts),
                "with_currency": sum(f.unit in {"CNY", "HKD", "USD"} for f in facts),
                "with_source_date": sum(f.observation_date is not None for f in facts),
                "with_original_link": sum(bool(f.source_url) for f in facts)}
            report["capabilities"][name]["returned_fields"] = dict(Counter(f.field for f in result))
    return report


async def full_probe(provider, target, timeout, *, data_only=False):
    from backend.app.agents.coordinator import CoordinatorAgent, basic_compliance_check, verify_facts
    from backend.app.agents.llm_agents import LLMConfig, OpenAICompatibleLLM, make_investment_agents
    from backend.app.agents.rule_agents import make_rule_agents
    from backend.app.models import OrchestrationRequest
    from backend.app.semantic import SemanticService, RequestUnderstanding
    from backend.app.models import Intent
    from backend.app.services.research import AutomatedResearchPipeline
    from backend.app.services.research_recovery import recover_research
    from backend.app.services.model_telemetry import analysis_telemetry

    config = LLMConfig.from_env()
    if provider is None or config is None and not data_only:
        return {"status": "not_configured", "provider_configured": provider is not None,
                "model_configured": config is not None}
    llm = OpenAICompatibleLLM(config) if not data_only else None
    agents = make_rule_agents() if data_only else make_investment_agents(llm)[0]
    coordinator = CoordinatorAgent(agents, verify_facts, basic_compliance_check, semantic=SemanticService(llm))
    pipeline = AutomatedResearchPipeline(provider, call_timeout_seconds=timeout)
    request = OrchestrationRequest(query=f"请研究{target}，分析风险及机构目标价依据",
        research_direction="个股研究", profile={"user_id": "SYNTHETIC_ON_DEMAND_SMOKE", "confirmed": True,
            "risk_level": "R3", "horizon_months": 24, "max_drawdown": .20, "liquidity_need": "中"})
    attempts = []
    async def observe(event):
        attempts.append({key: event[key] for key in ("skill_id", "status_code", "status", "fact_count")})
    provider.call_observer = observe
    def progress(stage):
        print(json.dumps({"stage": stage}, ensure_ascii=False), flush=True)
    started = perf_counter()
    telemetry = {"calls": [], "model_fact_ids": set()}
    telemetry_token = analysis_telemetry.set(telemetry)
    try:
        progress("理解问题")
        understanding = (RequestUnderstanding(intent=Intent.SECURITY_RESEARCH, confidence=1, risk_rules=[],
            reason="仅取数诊断固定研究范围，不代替真实语义判断", target=target) if data_only
            else await coordinator.understand_request(request))
        if understanding.risk_rules or understanding.intent.value != "security_research":
            return {"status": "dispatch_rejected", "intent": understanding.intent.value,
                    "risk_rules": understanding.risk_rules, "synthetic_profile": True,
                    "model_call_statuses": dict(Counter(c.get("status") for c in telemetry["calls"]))}
        progress("查找资料")
        prepared, audit = await pipeline.prepare(request, understanding.intent, target=understanding.target,
            data_requirements=understanding.data_requirements, force_refresh=True)
        prepared, audit = await recover_research(pipeline, prepared, understanding.intent, None, audit,
            target=understanding.target, data_requirements=understanding.data_requirements,
            semantic=coordinator.semantic, progress_sink=progress)
        advice = None
        if not data_only:
            advice = await coordinator.run(prepared, understanding=understanding, progress_sink=progress)
            recovered, audit = await recover_research(pipeline, prepared, understanding.intent, advice, audit,
                target=understanding.target, data_requirements=understanding.data_requirements,
                semantic=coordinator.semantic, progress_sink=progress)
            if recovered is not prepared:
                prepared = recovered
                advice = await coordinator.run(prepared, understanding=understanding, progress_sink=progress)
        return {"status": "data_only_completed" if data_only else "completed", "synthetic_profile": True,
            "semantic_used": not data_only, "intent": understanding.intent.value,
            "elapsed_seconds": round(perf_counter() - started, 3), "fact_count": len(prepared.facts),
            "field_counts": dict(Counter(f.field for f in prepared.facts)),
            "successful_capabilities": audit.successful_capabilities, "empty_capabilities": audit.empty_capabilities,
            "capability_errors": audit.capability_errors, "recovery_rounds": audit.recovery_rounds,
            "capability_evidence": audit.capability_evidence,
            "recovery_evidence": audit.recovery_evidence,
            "recovery_deferred_capabilities": audit.recovery_deferred_capabilities,
            "recovery_gap_metrics": audit.recovery_gap_metrics,
            "recovery_successful_capabilities": audit.recovery_successful_capabilities,
            "recovery_empty_capabilities": audit.recovery_empty_capabilities, "recovery_errors": audit.recovery_errors,
            "missing_fields_before": audit.recovery_missing_fields_before,
            "missing_fields_after": audit.missing_fields_by_agent, "assessment_status": audit.assessment_status,
            "assessment_targets": audit.assessment_targets, "gateway_attempts": attempts,
            "model_call_statuses": dict(Counter(c.get("status") for c in telemetry["calls"])),
            "model_call_diagnostics": [{k: c.get(k) for k in
                ("purpose", "status", "input_chars", "input_fact_count", "attempts")} for c in telemetry["calls"]],
            "model_input_budget": config.max_input_chars if config else None,
            "document_entity_counts": dict(Counter(f.entity for f in prepared.facts
                if f.field in {"news", "announcement", "research_report", "event"})),
            "eligible_document_count": sum(bool(f.source_url and f.period) for f in prepared.facts
                if f.field in {"news", "news_summary", "announcement", "announcement_summary", "research_report", "event"}),
            "compliance_status": advice.compliance.status.value if advice else None,
            "agent_statuses": {r.agent_id: r.status.value for r in advice.agent_results} if advice else {},
            "return_expectation_status": advice.return_expectation.status if advice else None,
            "return_scenario_count": len(advice.return_expectation.scenarios) if advice else None,
            "allocation_count": len(advice.allocation) if advice else None,
            "cross_validation_codes": sorted({i.code for i in advice.cross_validation.issues}) if advice else []}
    finally:
        analysis_telemetry.reset(telemetry_token)
        provider.call_observer = None
        await pipeline.cache.aclose()
        if llm is not None:
            await llm.aclose()


async def main(args):
    provider = IwencaiSkillHubProvider.from_env()
    try:
        run = full_probe(provider, args.target, args.timeout, data_only=args.data_only) if args.full or args.data_only else probe(provider, args.target, args.timeout)
        result = await asyncio.wait_for(run, timeout=180 if args.full or args.data_only else args.timeout + 5)
        return {"time": datetime.now(timezone.utc).isoformat(), **result}
    except Exception as error:
        return {"status": "failed", **failure_summary(error)}
    finally:
        if provider is not None:
            await provider.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("target")
    parser.add_argument("--timeout", type=float, default=15)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--full", action="store_true")
    mode.add_argument("--data-only", action="store_true", help="只验证真实取数与补取，固定研究范围，不调用模型或生成建议")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 0 < args.timeout <= 60:
        parser.error("timeout must be > 0 and <= 60 seconds")
    report = asyncio.run(main(args))
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
