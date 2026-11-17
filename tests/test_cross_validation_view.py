"""交叉核验与分歧展示须保留原始闸门、来源差异和缺失历史。"""
from copy import deepcopy
import json

import pytest
from streamlit.testing.v1 import AppTest

from backend.app.services.history import summarise_advice
from frontend.cross_validation_view import conflict_evidence_rows, disagreement_rows, validation_overview, disagreement_agents


def report():
    return {"trace_id": "offline-validation", "conclusion": "当前判断需复核。", "confidence": .4,
            "compliance": {"status": "REVIEW", "reason": "观点和数据尚待核实"},
            "evidence": ["a"], "facts": [{"fact_id": "a", "entity": "测试行业", "field": "pe_ttm",
                "value": 0, "unit": "倍", "period": "2026Q3", "source_id": "SYNTHETIC_A"}],
            "agent_results": [
                {"agent_id": "industry", "status": "degraded", "opinion": "行业估值记录需核对。", "facts_used": ["a"],
                 "details": {"rejected_reference_count": 1}},
                {"agent_id": "security", "status": "completed", "opinion": "基本面和短期走势存在不同判断。", "facts_used": ["a"]}],
            "task_plan": {"nodes": [{"agent_id": "fact_verifier", "status": "completed"}, {"agent_id": "compliance", "status": "completed"}]},
            "cross_validation": {"status": "REVIEW", "supporting_agents": ["industry"], "dissenting_agents": ["security"],
                "consensus_score": 55, "confidence": .4, "issues": [
                    {"code": "SOURCE_VALUE_CONFLICT", "severity": "warning", "message": "不同来源估值记录不一致",
                     "fact_ids": ["a", "b", "missing"], "evidence_details": [{"fact_id": "b", "entity": "测试行业",
                         "field": "pe_ttm", "value": 12, "period": "2026Q3", "source_id": "SYNTHETIC_B",
                         "source_url": "javascript:alert(1)"}]},
                    {"code": "SEMANTIC_AGENT_CONFLICT", "severity": "warning", "message": "两项观点的关键前提存在矛盾",
                     "agent_ids": ["industry", "security"]},
                    {"code": "EVIDENCE_REFERENCE_REJECTED", "severity": "warning", "message": "一条引用未通过核验", "agent_ids": ["industry"]},
                ]}}


def test_conflict_materials_preserve_unapproved_records_and_missing_history():
    data = report()
    original = deepcopy(data)
    rows, missing = conflict_evidence_rows(data)
    assert {row["资料编号"] for row in rows} == {"a", "b"}
    assert next(row for row in rows if row["资料编号"] == "a")["记录数值"] == "0"
    assert next(row for row in rows if row["资料编号"] == "b")["原文"] is None
    assert all(row["统计期"] == "2026Q3" for row in rows)
    assert missing == 1 and data == original


def test_unknown_issue_and_old_missing_status_are_not_promoted_to_pass():
    old = {"cross_validation": {"issues": [{"code": "FUTURE_CODE", "message": "原始问题说明"}]}}
    assert validation_overview(old)["cross"] == "未记录"
    assert disagreement_rows(old)[0]["本次记录"] == "原始问题说明"
    assert disagreement_rows(old)[0]["涉及维度"] == "未记录"
    assert "待确认" in disagreement_rows(old)[0]["处理原则"]


def test_dispersion_participants_are_visible_even_if_none_exceeds_distance_threshold():
    data = {"cross_validation": {"dissenting_agents": [], "issues": [{
        "code": "AGENT_SCORE_DISPERSION", "agent_ids": ["industry", "security", "fund"]}]}}
    assert disagreement_agents(data) == ["fund", "industry", "security"]


@pytest.mark.parametrize("compact,mode", [(False, "deep"), (True, "deep"), (False, "auto")])
def test_current_history_and_automatic_answers_expose_the_same_validation_panel(compact, mode):
    data = report()
    data["research_mode"] = mode
    if mode == "auto":
        data["response_scope"] = "clarification"
    script = ("import json\nfrom frontend.result_views import render_advice_details as render_advice\n"
              f"render_advice(json.loads({json.dumps(data, ensure_ascii=False)!r}), export_key='case', compact={compact})")
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    assert app.session_state["cross_validation_case"] is False
    assert any("交叉验证：待复核" in text.value for text in app.caption)
    app.session_state["cross_validation_case"] = True
    app.run(timeout=15)
    assert not app.exception
    assert any("逐项分歧与处理" in text.value for text in app.markdown)
    assert any("不能视为已经解决" in text.value for text in app.info)
    frames = [frame.value.to_string() for frame in app.dataframe]
    assert any("不同来源估值记录不一致" in frame and "处理原则" in frame for frame in frames)
    assert any("SYNTHETIC_B" in frame and "相关原始记录" not in frame for frame in frames)
    assert any("已剔除引用数" in frame for frame in frames)
    assert any("1 条相关原始记录" in text.value for text in app.caption)


def test_blocked_validation_shows_audit_without_redisplaying_specialist_opinions():
    data = report()
    data["compliance"]["status"] = "BLOCK"
    data["agent_results"][0]["opinion"] = "不应重新展示的投资观点"
    script = ("from frontend.cross_validation_view import render_validation_details\n"
              f"render_validation_details({data!r})")
    app = AppTest.from_string(script).run()
    assert not app.exception and app.error
    assert "已拦截" in app.error[0].value
    assert not any("不应重新展示的投资观点" in frame.value.to_string() for frame in app.dataframe)


def test_informational_record_does_not_override_a_recorded_pass():
    data = {"cross_validation": {"status": "PASS", "issues": [
        {"code": "SOURCE_NOTE", "severity": "info", "message": "本次保留来源提示"}]},
        "compliance": {"status": "PASS"}}
    app = AppTest.from_string("from frontend.cross_validation_view import render_validation_details\n"
                              f"render_validation_details({data!r})").run()
    assert not app.exception and not app.info and not app.error
    assert any("交叉验证已通过" in text.value for text in app.markdown)


def test_history_keeps_disagreement_metadata_and_reports_truncated_issue_count():
    data = report()
    original = deepcopy(data)
    data["cross_validation"]["issues"] = data["cross_validation"]["issues"] * 9
    stored = summarise_advice(data)
    cross = stored["cross_validation"]
    assert cross["supporting_agents"] == ["industry"] and cross["dissenting_agents"] == ["security"]
    assert cross["consensus_score"] == 55 and cross["confidence"] == .4
    assert cross["issues"][1]["agent_ids"] == ["industry", "security"]
    assert cross["issues"][0]["severity"] == "warning"
    assert cross["issue_count"] == 27 and len(cross["issues"]) == 20
    assert validation_overview(summarise_advice(stored))["count"] == 27
    assert original["cross_validation"]["status"] == data["cross_validation"]["status"] == "REVIEW"
