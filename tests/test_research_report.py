"""Evidence safety, API delivery, history and UI/export contracts for the dossier."""
from datetime import datetime, timedelta, timezone
import json
import pytest
from streamlit.testing.v1 import AppTest

from backend.app.models import (AdvicePackage, ComplianceResult, CrossValidationResult,
                                CrossValidationIssue, FactRecord, Intent, TaskPlan)
from backend.app.services.research_report import build_research_report, attach_research_report
from backend.app.services.history import summarise_advice
from frontend.research_report import report_sections
from frontend.result_views import answer_export_payload

NOW = datetime.now(timezone.utc)


def fact(fid, field="change", value=2, **kwargs):
    return FactRecord(fact_id=fid, entity=kwargs.pop("entity", "测试公司"), field=field,
                      value=value, unit=kwargs.pop("unit", "percent"), period=kwargs.pop("period", NOW.date().isoformat()),
                      source_id=kwargs.pop("source_id", "SYNTHETIC_TEST_ONLY"),
                      quality=kwargs.pop("quality", .95), snapshot_time=kwargs.pop("snapshot_time", datetime.now(timezone.utc)), **kwargs)


def advice(rows, **kwargs):
    return AdvicePackage(trace_id="test", intent=Intent.SECURITY_RESEARCH,
                         task_plan=TaskPlan(trace_id="test", intent=Intent.SECURITY_RESEARCH),
                         compliance=ComplianceResult(status="REVIEW"), conclusion="有限结论", confidence=.4,
                         facts=rows, evidence=[row.fact_id for row in rows],
                         cross_validation=CrossValidationResult(status="PASS"), research_mode="instant", **kwargs)


def items(report):
    return [item for section in report.sections for item in section.evidence]


def test_dossier_preserves_entities_periods_and_opposing_evidence():
    rows = [fact("up"), fact("profit", "net_profit_growth", -10),
            fact("other", entity="另一公司", value=-3),
            fact("old", period=(NOW.date()-timedelta(days=1)).isoformat(), value=-1)]
    report = build_research_report(advice(rows), now=NOW)
    assert {section.entity for section in report.sections} == {"测试公司", "另一公司"}
    points = items(report)
    assert len(points) == 4
    assert {point.stance for point in points} == {"favorable", "caution"}
    assert {fid for point in points for fid in point.fact_ids} == {row.fact_id for row in rows}
    assert all(section.change_conditions and section.limitations for section in report.sections)
    assert "模型辩论" in report.limitations[0]


def test_rule_scores_and_undated_values_are_neutral():
    rows = [fact("undated", period=None),
            fact("score", "valuation_score", 0, unit=None),
            fact("flow", "capital_flow", 10, unit=None)]
    report = build_research_report(advice(rows), now=NOW)
    assert all(point.stance == "neutral" for point in items(report))
    assert any("统计日期" in point.interpretation for point in items(report))
    assert any("规则映射" in point.interpretation for point in items(report))


def test_conflicts_and_derived_descendants_cannot_reappear():
    rows = [fact("a"), fact("b", value=9),
            fact("score", "technical_score", 70, unit=None, source_id="DERIVED_TEST", derived_from=["a"])]
    data = advice(rows)
    data.cross_validation = CrossValidationResult(status="REVIEW", issues=[
        CrossValidationIssue(code="SOURCE_VALUE_CONFLICT", severity="warning", message="冲突", fact_ids=["a", "b"])])
    report = build_research_report(data, now=NOW)
    assert report.status == "needs_review" and not items(report)
    assert "撤下" in report.limitations[0]


@pytest.mark.parametrize("updates", [
    {"quality": .1}, {"snapshot_time": NOW-timedelta(days=999)},
    {"derived_from": ["missing"], "source_id": "DERIVED_TEST"},
    {"period": "2099-01-01"}, {"observation_date": NOW.date()+timedelta(days=3)},
])
def test_invalid_or_future_evidence_is_excluded(updates):
    row = fact("bad", **updates)
    report = build_research_report(advice([row]), now=NOW)
    assert not items(report)


def test_blocked_education_and_uncited_facts_never_generate_signals():
    data = advice([fact("one")])
    data.evidence = []
    assert build_research_report(data) is None
    data.evidence = ["one"]
    data.compliance.status = "BLOCK"
    assert build_research_report(data) is None
    data.compliance.status = "REVIEW"
    data.response_scope = "education"
    assert build_research_report(data) is None


def test_cyclic_lineage_is_excluded_without_recursion():
    rows = [fact("a", derived_from=["b"]), fact("b", derived_from=["a"])]
    assert not items(build_research_report(advice(rows), now=NOW))


def test_history_export_and_render_preserve_report_without_fake_debate():
    data = attach_research_report(advice([fact("a"), fact("b", "net_profit_growth", -3)]))
    payload = data.model_dump(mode="json")
    assert summarise_advice(payload)["research_report"] == payload["research_report"]
    exported = answer_export_payload(payload, [])
    text = "\n".join(text for _, values in exported["sections"] for text in values)
    assert "支持因素" in text and "不利因素" in text and "重新评估条件" in text
    script = ("import json\nfrom frontend.research_report import render_research_report\n"
              + "data = json.loads(" + repr(json.dumps(payload)) + ")\n"
              + "render_research_report(data, key='test')\n")
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    visible = "\n".join(element.value for element in app.markdown)
    assert "研究证据对照" in visible and "不利因素" in visible


def test_report_is_bounded_and_discloses_omission():
    rows = [fact(str(i), entity=f"公司{i}") for i in range(25)]
    report = build_research_report(advice(rows), now=NOW)
    assert len(report.sections) == 16
    assert any("部分标的" in text for text in report.limitations)


@pytest.mark.asyncio
async def test_api_instant_delivers_dossier_without_any_model_or_network(monkeypatch):
    import httpx
    from backend.app import main
    from backend.app.models import OrchestrationRequest
    async def forbidden(*args, **kwargs):
        pytest.fail("instant dossier must not acquire data or invoke models")
    monkeypatch.setattr(main.coordinator, "run", forbidden)
    monkeypatch.setattr(main.research_pipeline, "prepare", forbidden)
    req = OrchestrationRequest(query="请分析测试公司股票", research_direction="个股研究", research_mode="instant",
                               profile={"confirmed": True, "risk_level": "R3"}, facts=[fact("q")])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.post("/api/v1/portfolio/analyze", json=req.model_dump(mode="json"))
    assert response.status_code == 200
    body = response.json()
    assert body["research_report"]["sections"] and body["model_calls"] == []
    assert any("q" in item["fact_ids"] for section in body["research_report"]["sections"]
               for item in section["evidence"])


def test_blocking_a_previous_report_also_suppresses_ui_and_export():
    data = attach_research_report(advice([fact("q")])).model_dump(mode="json")
    data["compliance"]["status"] = "BLOCK"
    assert not report_sections(data)
    assert all("支持因素" not in text for _, values in answer_export_payload(data, [])["sections"] for text in values)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["deep", "fast"])
async def test_research_prompt_separates_conclusion_from_evidence_without_extra_model_calls(mode):
    from types import SimpleNamespace
    from backend.app.agents.coordinator import CoordinatorAgent, verify_facts
    from backend.app.agents.llm_agents import HybridInvestmentAgent
    from backend.app.models import AgentResult, ComplianceStatus, CrossValidationResult, OrchestrationRequest
    evidence = fact("q")
    seen = []
    baseline = AgentResult(agent_id="security", status="completed", opinion="有限依据",
                           confidence=.4, facts_used=["q"], score=50)
    async def rule(request):
        return baseline.model_copy(deep=True)
    class Model:
        config = SimpleNamespace(model="test-model")
        async def complete_json(self, *, system, payload):
            seen.append((system, payload))
            return {
                **baseline.model_dump(mode="json"),
                "conclusion": "核心判断：短期表现偏强。支持证据：本期涨幅为2%。"
                              "判断限制：样本区间较短。投资评价：更像局部走强，而非普遍回升。",
                "opinion": "这一变化支持短期偏强的判断。",
            }
    request = OrchestrationRequest(query="研究测试公司", facts=[evidence],
                                   profile={"confirmed": True, "risk_level": "R3"}, research_mode=mode)
    result = await HybridInvestmentAgent("security", rule, Model()).run(request)
    assert len(seen) == 1 and result.facts_used == ["q"]
    instruction, payload = seen[0]
    opinion = payload["required_output"]["opinion"]
    assert "不列用于推导判断的指标、数值或来源" in payload["required_output"]["conclusion"]
    assert "直接评价并说明其实际意义" in payload["required_output"]["conclusion"]
    assert "关键数据、数据所属期、支持和反面证据及其如何影响判断写入 opinion" in instruction
    assert "判断限制、置信依据和缺项一律放在 opinion 或 confidence_reasons" in instruction
    assert "不以‘无法判断’开头" in instruction
    assert "不把缺少证据写成相反结论" in instruction and "无相关证据时" in instruction
    if mode == "deep":
        assert "不超过220字" in opinion and "证据少就写短" in opinion
        assert "独立贡献" in instruction and "不把样本表现外推到全市场" in instruction
        assert "数据所属期" in instruction and "只写足以影响判断的反面证据" in instruction
        assert "150字" not in instruction
    else:
        assert "80字" in instruction
        assert "220字" not in opinion
        assert "details" not in payload["required_output"]
    summary = CoordinatorAgent._summarize([result], ComplianceStatus.PASS,
                                          CrossValidationResult(status=ComplianceStatus.PASS))
    assert summary == "个股：短期表现偏强。\n\n更像局部走强，而非普遍回升。"
    assert "涨幅为2%" in result.opinion and "样本区间较短" in result.opinion
    rejected, = await verify_facts([result], [])
    assert rejected.conclusion is None and "2%" not in rejected.opinion and "样本区间较短" not in rejected.opinion
