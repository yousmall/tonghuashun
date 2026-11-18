"""Remove boilerplate guarantees from answer prose without hiding concrete risks."""
from __future__ import annotations

import re

_LEGACY = (
    "以上内容基于所示数据时点和公开/授权信息生成，仅用于投资研究辅助，"
    "不构成收益承诺或交易指令；请结合自身风险承受能力和独立判断审慎决策。"
)
_GUARANTEE = re.compile(
    r"(?:不构成|并非|不是|不代表|不作出?|不提供|不应视为|不能视为)"
    r"[^，。；！？\n]{0,12}(?:收益承诺|盈利承诺|承诺收益|承诺盈利|收益保证|盈利保证|损失上限保证|保证收益|保证盈利|保证本金|保证回报)"
    r"|(?:不|无法|不能|并不)(?:会|能)?(?:保证|承诺)(?:任何|实际|投资|未来|预期|目标|确定的){0,3}"
    r"(?:收益|盈利|回报|本金|损失上限)"
)
_GENERIC_PREFIX = re.compile(
    r"(?:本次(?:分析|回答|评估)|以上(?:内容|分析|结论)|本(?:分析|结论|报告|回答|内容)|"
    r"分析结果|研究结论|历史回撤|投资收益|历史业绩|过往业绩|情景测算|仅供参考|仅用于投资研究辅助)?"
    r"\s*(?:并|且|也|仍|均)?"
)
_PARENTHETICAL = re.compile(r"[（(][^（）()\n]{1,65}[）)]")
_CLAUSE = re.compile(r"[\s\S]+?(?:[，。；！？;!?\n]+|$)")


def clean_answer_text(value: str | None) -> str:
    text = (value or "").replace(_LEGACY, "")
    if not _GUARANTEE.search(text):
        return text.strip()
    text = _PARENTHETICAL.sub(lambda m: "" if _GUARANTEE.search(m[0]) else m[0], text)
    clauses = []
    for match in _CLAUSE.finditer(text):
        clause = match[0]
        guarantee = _GUARANTEE.search(clause)
        if guarantee:
            before = clause[:guarantee.start()].strip(" *_>-#\t\n")
            if _GENERIC_PREFIX.fullmatch(before) or before.endswith(("仅供参考", "仅供研究参考")):
                if clauses and clauses[-1].endswith(("，", "；", ";")):
                    clauses[-1] = clauses[-1][:-1] + "。"
                continue
            remainder = clause[guarantee.end():].rstrip("，。；！？;!?\n ").strip(" *_>-#")
            if not remainder or remainder.startswith(("或交易指令", "或投资建议", "和交易指令")):
                clause = clause[:guarantee.start()].rstrip(" ，；;、") + "。"
            else:
                clause = clause[:guarantee.start()] + clause[guarantee.end():]
        clauses.append(clause)
    return "".join(clauses).strip()


def _clean_list(values):
    return [text for value in values if (text := clean_answer_text(value))]


def clean_advice(advice):
    """Clean answer prose before delivery while retaining source records and rule results."""
    compliance = advice.compliance
    expectation = advice.return_expectation
    recommendation = advice.stock_recommendation
    if recommendation:
        def clean_candidate(candidate):
            return candidate.model_copy(update={
                "reasons": _clean_list(candidate.reasons),
                "risks": _clean_list(candidate.risks),
            })

        recommendation = recommendation.model_copy(update={
            "candidates": [clean_candidate(candidate) for candidate in recommendation.candidates],
            "recommendations": [clean_candidate(candidate) for candidate in recommendation.recommendations],
        })
    return advice.model_copy(update={
        "conclusion": clean_answer_text(advice.conclusion),
        "risk_conclusion": clean_answer_text(advice.risk_conclusion or "") or None,
        "risks": _clean_list(advice.risks),
        "next_steps": _clean_list(advice.next_steps),
        "user_fit": clean_answer_text(advice.user_fit or "") or None,
        "return_expectation": expectation.model_copy(update={
            "summary": clean_answer_text(expectation.summary),
            "assumptions": _clean_list(expectation.assumptions),
        }),
        "stock_recommendation": recommendation,
        "compliance": compliance.model_copy(update={
            "reason": clean_answer_text(compliance.reason or "") or None,
            "risk_notice": clean_answer_text(compliance.risk_notice or "") or None,
            "required_disclosures": _clean_list(compliance.required_disclosures),
        }),
        "agent_results": [result.model_copy(update={
            "opinion": clean_answer_text(result.opinion),
            "risk_flags": _clean_list(result.risk_flags),
            "confidence_reasons": _clean_list(result.confidence_reasons),
            "invalidation_conditions": _clean_list(result.invalidation_conditions),
        }) for result in advice.agent_results],
    })
