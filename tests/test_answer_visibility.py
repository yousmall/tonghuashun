"""Collapsed diagnostics must not erase material investment risks or raw audit."""
from copy import deepcopy
import json
import pytest
from streamlit.testing.v1 import AppTest

from frontend.answer_visibility import verification_notes, visible_risks, visible_next_steps
from frontend.result_views import answer_export_payload


def report():
    return {
        "conclusion": "已核对可用财务资料；价格仍可能下跌。",
        "risk_conclusion": "本次资料的引用、时点、同项记录或分项观点存在待核对之处，暂不能形成可靠的风险判断。",
        "compliance": {"status": "REVIEW", "risk_notice": "不构成收益承诺。",
                       "reason": "毛利率同项记录不一致，需核对时间与口径。"},
        "data_acquisition": {"mode": "mixed", "missing_fields_by_agent": {"market": ["liquidity_score"]}},
        "risks": ["宏观流动性数据缺失", "证据不足", "持仓集中度风险较高", "提价对业绩影响尚不明确"],
        "next_steps": ["补齐各专业智能体列出的缺失字段", "在执行任何调整前复核风险标记和证伪条件"],
        "cross_validation": {"status": "REVIEW", "issues": [
            {"code": "INTERNAL_VALUE_CONFLICT", "message": "毛利率同项记录不一致，需核对时间与口径。", "fact_ids": ["conflict"]},
            {"code": "AGENT_SCORE_DISPERSION", "message": "基本面与技术面观点存在分歧"},
        ]},
        "facts": [], "evidence": [],
    }


@pytest.mark.parametrize("compact", [False, True])
def test_latest_and_history_hide_diagnostics_by_default_but_keep_review_and_real_risks(compact):
    data = report()
    script = "import json\nfrom frontend.result_views import render_advice\n" + (
        f"render_advice(json.loads({json.dumps(data, ensure_ascii=False)!r}), compact={compact})"
    )
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    body = "\n".join([item.value for item in app.markdown] + [item.value for item in app.caption])
    assert "同项记录不一致" not in body
    assert "尚缺" not in body and "宏观流动性数据缺失" not in body
    assert "补齐各专业智能体" not in body
    assert "集中度风险较高" in body and "提价对业绩影响尚不明确" in body
    assert "观点存在分歧" in body
    assert app.warning and "仍需核实" in app.warning[0].value
    assert any(item.label == "核验记录" for item in app.expander)


def test_projection_preserves_original_audit_and_pdf_keeps_details_in_appendix():
    data = report()
    original = deepcopy(data)
    assert "证据不足" not in visible_risks(data)
    assert "补齐各专业智能体列出的缺失字段" not in visible_next_steps(data)
    assert any("毛利率同项记录" in text for text in verification_notes(data))
    exported = dict(answer_export_payload(data, [], "测试问题")["sections"])
    assert "同项记录不一致" not in "\n".join(exported["需要注意"])
    assert "证据不足" not in "\n".join(exported["需要注意"])
    assert "同项记录不一致" not in "\n".join(exported["风险结论"])
    assert "集中度风险较高" in "\n".join(exported["需要注意"])
    assert any("同项记录不一致" in text for text in exported["核验记录（附录）"])
    assert data == original and data["compliance"]["status"] == "REVIEW"


@pytest.mark.parametrize("risk", [
    "流动性数据不足且存在赎回风险", "财报资料不足，业绩可能下滑",
    "提价效果没有数据支持，利润改善尚不确定", "持仓集中度风险超过上限",
])
def test_mixed_material_uncertainty_is_never_filtered_as_diagnostic(risk):
    assert risk in visible_risks({"risks": [risk]})


def test_unrecognized_issues_and_material_risks_remain_visible():
    data = {"cross_validation": {"issues": [
        {"code": "NEW_RISK_RULE", "message": "需要确认产品流动性风险"},
        {"code": "INCOMPLETE_ANALYSIS", "message": "缺少财报资料，业绩改善尚待验证"},
    ]}}
    assert visible_risks(data) == ["需要确认产品流动性风险", "缺少财报资料，业绩改善尚待验证"]
