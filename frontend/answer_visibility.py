"""Separate research diagnostics from investment risks without changing advice."""
from __future__ import annotations
import re
from typing import Any
from backend.app.fact_taxonomy import FIELD_LABELS

DIAGNOSTIC_CODES = frozenset({
    "INTERNAL_VALUE_CONFLICT", "SOURCE_VALUE_CONFLICT", "INCOMPLETE_ANALYSIS",
    "NO_VERIFIED_EVIDENCE", "EVIDENCE_REFERENCE_REJECTED", "COMPLETED_WITHOUT_EVIDENCE",
})
AGENT_LABELS = {"market": "市场环境", "industry": "行业分析", "security": "个股研究",
                "fund": "基金分析", "portfolio": "持仓分析"}
RESEARCH_STEPS = frozenset({
    "补充有来源、含时间戳的事实后重新核验", "补齐各专业智能体列出的缺失字段",
    "刷新缺失或过期的资料并重新分析；仍无法核实时转人工复核",
    "系统已自动补取相关资料；仍未解决的缺项或冲突，请取得可核验资料后复核",
})
# Mixed statements mentioning a material investment risk stay in the body.
MATERIAL_MARKERS = ("亏损", "回撤", "下跌", "上涨", "波动", "收益", "业绩", "提价",
                    "集中度", "违约", "承压", "赎回", "本金", "流动性风险")


def diagnostic_point(value: str) -> bool:
    text = str(value or "").strip().rstrip("。；;.")
    if not text or any(marker in text for marker in MATERIAL_MARKERS):
        return False
    if text in {"资料不足", "材料不足", "证据不足", "数据缺失", "观点资料缺失"}:
        return True
    return bool(re.search(
        r"(?:数据|资料|材料|事实|证据)(?:不足|缺失|不完整)|缺少字段[:：]|"
        r"(?:缺乏|缺少)(?:直接|有效|可核验|授权)*(?:数据|资料|事实|证据)|"
        r"(?:授权资料|本次资料)的引用、时点|缺少通过核验的资料引用|评分转换|规范化.*评分", text))


def visible_risks(advice: dict[str, Any]) -> list[str]:
    diagnostic_messages = {issue.get("message") for issue in (advice.get("cross_validation") or {}).get("issues", [])
                           if issue.get("code") in DIAGNOSTIC_CODES
                           and not any(marker in str(issue.get("message") or "") for marker in MATERIAL_MARKERS)}
    risks = [*advice.get("risks", []),
             *[issue.get("message") for issue in (advice.get("cross_validation") or {}).get("issues", [])
               if issue.get("code") not in DIAGNOSTIC_CODES
               or any(marker in str(issue.get("message") or "") for marker in MATERIAL_MARKERS)]]
    return list(dict.fromkeys(text for text in risks if text and text not in diagnostic_messages
                              and not diagnostic_point(text)))


def visible_next_steps(advice: dict[str, Any]) -> list[str]:
    return [text for text in advice.get("next_steps", []) if text not in RESEARCH_STEPS]


def visible_risk_conclusion(advice: dict[str, Any]) -> str | None:
    text = advice.get("risk_conclusion")
    return "本次分析仍需核实。" if text and diagnostic_point(text) else text


def visible_compliance_reason(advice: dict[str, Any]) -> str | None:
    text = (advice.get("compliance") or {}).get("reason")
    diagnostic_messages = {issue.get("message") for issue in (advice.get("cross_validation") or {}).get("issues", [])
                           if issue.get("code") in DIAGNOSTIC_CODES}
    if text and not any(marker in text for marker in MATERIAL_MARKERS) and (
            text in diagnostic_messages or diagnostic_point(text)):
        return None
    return text


def coverage_notices(acquisition: dict[str, Any]) -> list[str]:
    return [f"{AGENT_LABELS.get(agent, '相关分析')}尚缺：" + "、".join(FIELD_LABELS.get(field, "相关指标") for field in fields)
            for agent, fields in (acquisition.get("missing_fields_by_agent") or {}).items() if fields]


def verification_notes(advice: dict[str, Any]) -> list[str]:
    acquisition = advice.get("data_acquisition") or {}
    notes = coverage_notices(acquisition)
    if acquisition.get("mode") == "unavailable":
        notes.append("本次未能取得最新市场数据，分析可能不完整。")
    if any(acquisition.get(key) for key in ("failed_capabilities", "empty_capabilities",
                                           "recovery_failed_capabilities", "recovery_empty_capabilities")):
        notes.append("部分资料暂未取得，相关判断仍需补充信息。")
    notes.extend(issue.get("message") for issue in (advice.get("cross_validation") or {}).get("issues", [])
                 if issue.get("code") in DIAGNOSTIC_CODES)
    notes.extend(text for text in advice.get("risks", []) if diagnostic_point(text))
    notes.extend(text for text in advice.get("next_steps", []) if text in RESEARCH_STEPS)
    if advice.get("risk_conclusion") != visible_risk_conclusion(advice):
        notes.append(advice["risk_conclusion"])
    reason = (advice.get("compliance") or {}).get("reason")
    if reason and reason != visible_compliance_reason(advice):
        notes.append(reason)
    return list(dict.fromkeys(text for text in notes if text))
