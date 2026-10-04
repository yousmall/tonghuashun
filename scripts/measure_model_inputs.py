"""Capture the actual payload builders without calling an external model."""
import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.agents.llm_agents import HybridInvestmentAgent
from backend.app.agents.rule_agents import make_rule_agents
from backend.app.models import FactRecord, OrchestrationRequest, UserProfile
from backend.app.risk_questionnaire import QUESTIONS, evaluate_answers
from backend.app.semantic import SemanticService


class Recorder:
    def __init__(self, *, baseline=False):
        self.calls = []
        self.config = SimpleNamespace(model="record-only")
        self.baseline = baseline

    async def complete_json(self, *, system, payload):
        if self.baseline:
            system = system.replace("profile 中省略的字段表示未提供，禁止推测补全。", "")
        self.calls.append((system, payload))
        purpose = payload.get("required_schema", {}).get("title")
        if purpose == "RequestUnderstanding":
            return dict(intent="security_research", confidence=.9, risk_rules=[], reason="研究个股")
        if purpose == "OutputReview":
            return dict(confidence=.9, risk_rules=[], conflicting_agents=[], reason="证据需要复核")
        if purpose == "StockEvidenceReview":
            return dict(confidence=.9, assessments=[], matches=[])
        if purpose == "ProfileExtraction":
            return dict(confidence=.9, patch={}, evidence={})
        return dict(agent_id=payload["required_output"]["agent_id"], status="completed",
                    opinion="仅根据已提供材料研究，注意风险。", score=50, confidence=.8,
                    facts_used=[f["fact_id"] for f in payload["authorized_facts"][:1]])


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--baseline", action="store_true", help="Reproduce the pre-compression serialization locally")
    args = parser.parse_args()
    if args.baseline:
        import backend.app.semantic as semantic_module
        import backend.app.agents.llm_agents as agent_module
        def original_profile(value, *, exclude=None):
            excluded = {"user_id"} | (exclude or set())
            if exclude:
                excluded.add("risk_answers")
            return value.model_dump(mode="json", exclude=excluded)
        semantic_module.compact_model_schema = lambda schema: schema
        semantic_module.profile_for_model = agent_module.profile_for_model = original_profile
        semantic_module.fact_for_model = agent_module.fact_for_model = lambda value: value.model_dump(mode="json")
        agent_module.baseline_for_model = lambda value: value.model_dump(mode="json")
    answers = dict(zip((q.id for q in QUESTIONS), "ACABDC CACCB BCCBC CDB CADAECDC".replace(" ", ""), strict=True))
    profile = UserProfile(**evaluate_answers(answers), confirmed=True, constraints=["不使用杠杆"],
                          holding_history=[{"entity": "示例公司", "weight": .3}],
                          trading_analysis={"warnings": ["持仓集中"], "trade_count": 30})
    fields = ("growth_score", "inflation_score", "liquidity_score", "policy_score", "risk_appetite_score",
              "prosperity_score", "valuation_score", "capital_flow_score", "crowding_score",
              "fundamental_score", "technical_score", "fund_score", "fee_rate", "tracking_error",
              "fund_risk_level", "weight", "close_price", "roe", "pe_ttm")
    facts = [FactRecord(fact_id=f"F-{i}", entity=entity, field=field, value=("R2" if field == "fund_risk_level" else 50),
                        snapshot_time=datetime(2026, 10, 4, tzinfo=timezone.utc), source_id="DEMO_SNAPSHOT", quality=.9)
             for i, (entity, field) in enumerate((e, f) for e in ("A股市场", "示例行业", "示例公司") for f in fields)]
    req = OrchestrationRequest(query="个股研究：研究示例公司及其持仓集中风险", profile=profile, facts=facts,
                               research_direction="个股研究", context_messages=[{"role": "user", "content": "不使用杠杆"}])
    recorder = Recorder(baseline=args.baseline)
    semantic = SemanticService(recorder)
    await semantic.understand(req)
    results = [await HybridInvestmentAgent(aid, handler, recorder).run(req) for aid, handler in make_rule_agents().items()]
    await semantic.review(req, results)
    await semantic.assess_stock_evidence(req, facts, [], [], candidate_entity="示例公司")
    await semantic.extract_profile("投资期限三年，不使用杠杆")
    try:
        import tiktoken
        encoding = tiktoken.get_encoding("cl100k_base")
    except Exception:
        encoding = None
    rows = []
    for system, payload in recorder.calls:
        content = system + json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))
        rows.append({"purpose": payload.get("required_output", {}).get("agent_id") or payload["required_schema"]["title"],
                     "input_chars": len(content), "cl100k_tokens_estimate": len(encoding.encode(content)) if encoding else None,
                     "fact_count": len(payload.get("authorized_facts", []))})
    report = {"synthetic_input": True, "external_model_calls": 0, "baseline": args.baseline,
              "tokenizer": "cl100k_base estimate, not DeepSeek billed usage",
              "calls": rows, "total_chars": sum(r["input_chars"] for r in rows),
              "total_tokens_estimate": sum(r["cl100k_tokens_estimate"] for r in rows) if encoding else None}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
