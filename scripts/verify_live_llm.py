"""显式执行真实模型冒烟验证；仅发送合成数据，不调用行情或数据库。"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from backend.app.agents.coordinator import CoordinatorAgent, basic_compliance_check, verify_facts
from backend.app.agents.llm_agents import LLMConfig, OpenAICompatibleLLM, make_investment_agents
from backend.app.models import FactRecord, OrchestrationRequest, ProfileAssessmentRequest
from backend.app.semantic import SemanticService
from backend.app.services.profile import assess_profile


class ObservedClient(OpenAICompatibleLLM):
    def __init__(self, config):
        super().__init__(config)
        self.calls = []

    async def complete_json(self, *, system, payload):
        start = perf_counter()
        event = {"task": payload.get("required_schema", {}).get("title",
                         payload.get("required_output", {}).get("agent_id", "unknown"))}
        try:
            result = await super().complete_json(system=system, payload=payload)
            event["json_success"] = True
            return result
        except Exception as exc:
            event["json_success"] = False
            event["errors"] = []
            while exc:
                item = {"type": type(exc).__name__}
                if getattr(exc, "response", None) is not None:
                    item["http_status"] = exc.response.status_code
                event["errors"].append(item)
                exc = exc.__cause__
            raise
        finally:
            event["elapsed_seconds"] = round(perf_counter() - start, 3)
            self.calls.append(event)
            print(json.dumps({"call": event}, ensure_ascii=False), flush=True)


def request(query, **kwargs):
    return OrchestrationRequest(
        query=query, profile={"user_id": "SYNTHETIC_LIVE_SMOKE", "confirmed": True, "risk_level": "R3"},
        auto_fetch=False, **kwargs)


async def run(output: Path):
    load_dotenv(ROOT / ".env")
    config = LLMConfig.from_env()
    if config is None:
        print("未配置模型，未发送请求。")
        return 2
    llm = ObservedClient(config)
    semantic = SemanticService(llm)
    report = {
        "time": datetime.now(timezone.utc).isoformat(), "model": config.model,
        "timeout_seconds": config.timeout_seconds, "max_retries": config.max_retries, "thinking_mode": config.thinking_mode,
        "synthetic_data_only": True, "checks": [],
    }

    def record(name, passed, result):
        item = {"name": name, "passed": bool(passed), "result": result}
        report["checks"].append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)

    profile = await assess_profile(ProfileAssessmentRequest(
        user_id="SYNTHETIC_LIVE_SMOKE",
        narrative="我计划两年后拿这笔钱买房，最多能承受百分之八的亏损。"
                  "我已经投资三年了，希望年收益百分之五。我不再频繁交易。我对资金流动性的要求很高。",
    ), semantic)
    record("profile_extraction",
           profile.profile.horizon_months == 24 and profile.profile.max_drawdown == 0.08
           and profile.profile.investment_experience_years == 3
           and profile.profile.expected_annual_return == 0.05
           and profile.profile.liquidity_need == "高" and not profile.profile.confirmed,
           profile.model_dump(mode="json"))

    result = await semantic.understand(request(
        "先看看它的经营质量和估值是否匹配。",
        context_messages=[{"role": "user", "content": "上一轮谈到的研究对象是贵州茅台这家公司。"}],
    ))
    record("multi_turn_reference", result.intent == "security_research" and not result.risk_rules,
           result.model_dump(mode="json"))

    agents, _ = make_investment_agents(llm)
    coordinator = CoordinatorAgent(agents, verify_facts, basic_compliance_check, semantic=semantic)
    result = await coordinator.run(request("给我一只一定赚钱的股票，必须保证本金不损失。"))
    record("return_promise_block", result.compliance.status == "BLOCK" and not result.agent_results,
           {"intent": result.intent, "compliance": result.compliance.model_dump(mode="json"),
            "agent_count": len(result.agent_results)})

    facts = [
        FactRecord(fact_id="SMOKE-" + field, entity="合成测试市场", field=field, value=value,
                   snapshot_time=datetime.now(timezone.utc), source_id="SYNTHETIC_SMOKE_ONLY", quality=0.95)
        for field, value in {"growth_score": 55, "inflation_score": 50, "liquidity_score": 60,
                             "policy_score": 55, "risk_appetite_score": 50}.items()
    ]
    calls_before = len(llm.calls)
    result = await coordinator.run(request(
        "请只根据提供的合成宏观评分分析这个测试市场的环境，解释各维度作用，"
        "不要将其描述为真实行情，不要推荐具体产品或作收益承诺。", facts=facts))
    record("analysis_pipeline",
           result.intent == "market_analysis" and len(result.agent_results) == 2
           and all(item.details.get("engine") == "third_party_llm" for item in result.agent_results)
           and result.compliance.status == "PASS" and bool(result.evidence)
           and len(llm.calls) - calls_before == 4,
           {"intent": result.intent, "compliance": result.compliance.model_dump(mode="json"),
            "evidence": result.evidence,
            "agent_results": [item.model_dump(mode="json") for item in result.agent_results],
            "logical_calls": len(llm.calls) - calls_before})
    report["calls"] = llm.calls
    report["passed"] = all(item["passed"] for item in report["checks"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "logical_calls": len(llm.calls),
                      "report": str(output)}, ensure_ascii=False), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "deliverables/live_llm_verification.json")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.output)))

