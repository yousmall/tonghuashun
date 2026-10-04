"""收益/仓位展示的证据闸门、历史回放和真实 Streamlit 渲染。"""
from datetime import datetime, timezone

import pytest
from streamlit.testing.v1 import AppTest

from backend.app.agents.coordinator import CoordinatorAgent, basic_compliance_check, verify_facts
from backend.app.models import AgentResult, ComplianceResult, CrossValidationResult, FactRecord, Intent, OrchestrationRequest, TaskPlan, UserProfile
from backend.app.models.schemas import ReturnExpectation
from backend.app.services.history import summarise_advice
from backend.app.services.return_expectation import build_return_expectation
from frontend.investment_panel import allocation_rows, return_scenario_rows
from frontend.result_views import answer_export_payload


def facts():
    stamp = datetime.now(timezone.utc)
    return [FactRecord(fact_id=identity, entity="合成公司", entity_code="600001.SH", field=field,
                       value=value, unit="CNY", snapshot_time=stamp, period=stamp.date().isoformat(),
                       source_id="SYNTHETIC_TEST", source_url="https://example.com/report", quality=.95)
            for identity, field, value in [("price", "close_price", 100), ("target", "target_price", 120)]]


def profile():
    return UserProfile(confirmed=True, risk_level="R3", expected_annual_return=.08, horizon_months=24)


def result():
    return AgentResult(agent_id="portfolio", status="completed", opinion="合成诊断", confidence=.9,
                       facts_used=["price", "target"], details={"largest_weight": .2})


def advice():
    raw = facts()
    return {"trace_id": "explanation-test", "conclusion": "合成数据演示，请核对原文。",
            "compliance": {"status": "PASS"}, "evidence": ["price", "target"],
            "facts": [fact.model_dump(mode="json") for fact in raw], "agent_results": [result().model_dump(mode="json")],
            "return_expectation": build_return_expectation(raw, ["price", "target"], profile(), "PASS").model_dump(),
            "allocation": [{"asset_class": "权益类", "min_weight": .4, "max_weight": .6,
                            "basis": "已确认 R3 画像，合成示例", "evidence": ["price"]}]}


def test_target_scenario_is_reproducible_and_goal_is_separate():
    output = build_return_expectation(facts(), ["price", "target"], profile(), "PASS")
    assert output.status == "scenario"
    assert output.scenarios[0].price_return == pytest.approx(.2)
    assert output.scenarios[0].evidence == ["price", "target"]
    assert output.user_goal_annual == .08
    assert output.investment_horizon_months == 24
    assert "不作年化" in output.scenarios[0].horizon


@pytest.mark.parametrize("case", ["unreferenced", "missing_currency", "mixed_currency", "other_entity", "missing_code", "conflicting_price", "invalid_price", "missing_date", "future_period", "report_after_price"])
def test_missing_or_incompatible_evidence_never_produces_a_number(case):
    raw, references = facts(), ["price", "target"]
    if case == "unreferenced":
        references = ["price"]
    elif case == "missing_currency":
        raw[1].unit = None
    elif case == "mixed_currency":
        raw[1].unit = "USD"
    elif case == "other_entity":
        raw[1].entity_code = "000001.SZ"
    elif case == "missing_code":
        raw[1].entity_code = None
    elif case == "conflicting_price":
        raw.append(raw[0].model_copy(update={"fact_id": "conflict", "value": 110}))
        references.append("conflict")
    elif case == "invalid_price":
        raw[0].value = 0
    elif case == "missing_date":
        raw[1].period = None
    elif case == "future_period":
        raw[1].period = "2099-01-01"
    elif case == "report_after_price":
        raw[0].period = "2020-01-01"
    output = build_return_expectation(raw, references, profile(), "PASS")
    assert output.status == "unavailable" and not output.scenarios
    assert output.user_goal_annual == .08


def test_multiple_targets_are_kept_separate_and_downside_is_visible():
    raw = facts()
    raw.append(raw[1].model_copy(update={"fact_id": "bear-target", "value": 80, "source_id": "OTHER_TEST_SOURCE"}))
    output = build_return_expectation(raw, [fact.fact_id for fact in raw], profile(), "REVIEW")
    assert [scenario.price_return for scenario in output.scenarios] == pytest.approx([.2, -.2])
    assert "不能作为收益预测" in output.summary


@pytest.mark.parametrize("blocked,confirmed", [(True, True), (False, False)])
def test_blocked_or_unconfirmed_profile_hides_return_numbers(blocked, confirmed):
    user = profile().model_copy(update={"confirmed": confirmed})
    output = build_return_expectation(facts(), ["price", "target"], user, "BLOCK" if blocked else "PASS")
    assert output.status == "blocked" and not output.scenarios and output.user_goal_annual is None


def test_failed_or_unquantified_portfolio_does_not_get_default_r3_allocation():
    req = OrchestrationRequest(query="诊断", profile=profile(), facts=facts())
    assert CoordinatorAgent._allocation_summary([result()], req)
    req.profile.risk_level = None
    assert not CoordinatorAgent._allocation_summary([result()], req)
    req.profile.risk_level = "R3"
    for status in ["failed", "unknown"]:
        failed = result().model_copy(update={"status": status})
        assert not CoordinatorAgent._allocation_summary([failed], req)
    assert not CoordinatorAgent._allocation_summary([result().model_copy(update={"facts_used": []})], req)


def test_coordinator_and_history_keep_scenarios_and_references(semantic):
    req = OrchestrationRequest(query="诊断", profile=profile(), facts=facts())
    coordinator = CoordinatorAgent({}, verify_facts, basic_compliance_check, semantic=semantic)
    output = coordinator._aggregate(req, TaskPlan(trace_id="test", intent=Intent.PORTFOLIO_REVIEW),
                                    [result()], CrossValidationResult(status="PASS"), ComplianceResult(status="PASS"))
    assert output.return_expectation.status == "scenario"
    assert output.allocation[0]["evidence"] == output.evidence
    history = summarise_advice(advice())
    assert history["return_expectation"] == advice()["return_expectation"]
    assert return_scenario_rows(history)[0]["情景空间（%）"] == pytest.approx(20)
    blocked = coordinator._aggregate(req, output.task_plan, [result()], output.cross_validation, ComplianceResult(status="BLOCK"))
    assert blocked.return_expectation.status == "blocked" and not blocked.allocation


@pytest.mark.asyncio
async def test_complete_orchestration_rejects_expired_forecast_references(semantic):
    async def security(request):
        return result().model_copy(update={"agent_id": "security"})
    coordinator = CoordinatorAgent({"security": security}, verify_facts, basic_compliance_check, semantic=semantic)
    req = OrchestrationRequest(query="请研究示例科技这只个股", profile=profile(), auto_fetch=False, facts=facts())
    output = await coordinator.run(req)
    assert output.return_expectation.status == "scenario"
    from datetime import timedelta
    req.facts[1].snapshot_time -= timedelta(days=365)
    output = await coordinator.run(req)
    assert output.return_expectation.status == "unavailable" and "target" not in output.evidence


def test_history_with_lost_evidence_or_forged_arithmetic_is_not_charted():
    data = advice()
    data["facts"] = data["facts"][:1]
    assert not return_scenario_rows(data)
    data = advice()
    data["return_expectation"]["scenarios"][0]["price_return"] = .9
    assert not return_scenario_rows(data)
    data = advice()
    data["allocation"][0]["min_weight"] = float("nan")
    assert not allocation_rows(data)
    data = advice()
    data["compliance"]["status"] = "BLOCK"
    assert not return_scenario_rows(data) and not allocation_rows(data)


def test_streamlit_panel_charts_and_pdf_export_contain_both_core_sections():
    data = advice()
    app = AppTest.from_string(f"""
from frontend.presentation import render_conclusion_panel
render_conclusion_panel({data!r})
""").run(timeout=15)
    assert not app.exception
    assert len(app.get("vega_lite_chart")) == 2
    assert len(app.dataframe) == 2
    assert any("个人目标，不是系统预测" in item.value for item in app.markdown)
    exported = answer_export_payload(data, [], "合成测试")
    sections = dict(exported["sections"])
    assert "情景空间" in " ".join(sections["收益预期"])
    assert "40%–60%" in " ".join(sections["仓位建议"])


def test_old_history_and_blocked_results_render_without_fake_charts():
    for data in [{"compliance": {"status": "REVIEW"}}, {**advice(), "compliance": {"status": "BLOCK"}}]:
        app = AppTest.from_string(f"""
from frontend.investment_panel import render_investment_panel
render_investment_panel({data!r}, key='test')
""").run(timeout=15)
        assert not app.exception and not app.get("vega_lite_chart")
        assert any(item.label == "收益预期" and not item.proto.expanded for item in app.expander)


def test_source_filter_and_expansion_include_ancestor_and_original_link():
    data = advice()
    data["facts"].append({**data["facts"][0], "fact_id": "derived", "field": "valuation_score", "value": 70,
                          "derived_from": ["price"], "derivation_rule": "合成示例规则"})
    data["evidence"] = ["derived", "target"]
    data["agent_results"][0].update(agent_id="security", facts_used=["derived"])
    data["agent_results"].append({"agent_id": "industry", "status": "completed", "facts_used": ["target"]})
    app = AppTest.from_string(f"""
from frontend.presentation import render_source_trace, render_logic_chain
data = {data!r}
render_source_trace(data, collapsed=False)
render_logic_chain(data)
""").run(timeout=15)
    assert not app.exception
    app.multiselect[0].select("个股研究").run(timeout=15)
    assert not app.exception and len(app.dataframe[0].value) == 2
    assert "https://example.com/report" in app.dataframe[0].value.to_string()
    assert any("计算依据" in item.value for item in app.caption)


def test_default_contract_keeps_forecast_unavailable():
    assert ReturnExpectation().status == "unavailable"


def test_http_response_serializes_the_new_panel_contract(monkeypatch, semantic):
    from fastapi.testclient import TestClient
    from backend.app import main

    async def portfolio(request):
        return result()

    monkeypatch.setattr(main, "coordinator", CoordinatorAgent(
        {"portfolio": portfolio}, verify_facts, basic_compliance_check, semantic=semantic))
    response = TestClient(main.app).post("/api/v1/portfolio/analyze", json={
        "query": "请诊断我的持仓组合", "profile": profile().model_dump(mode="json"),
        "auto_fetch": False, "facts": [fact.model_dump(mode="json") for fact in facts()],
    })
    assert response.status_code == 200
    body = response.json()
    assert body["return_expectation"]["status"] == "scenario"
    assert body["return_expectation"]["scenarios"][0]["price_return"] == pytest.approx(.2)
    assert body["allocation"][0]["evidence"] == ["price", "target"]
    assert len(body["facts"]) >= 2
