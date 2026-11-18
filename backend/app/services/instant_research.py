"""Completed evidence review with no model, network, recovery or forecast wait.

This deliberately has a narrower contract than a fully reviewed online report.
Every displayed numeric observation is cited before the existing verification and
conflict gates; missing dimensions remain missing and prevent an overall score.
"""
from __future__ import annotations

from collections import defaultdict
import re

from backend.app.fact_taxonomy import FIELD_LABELS, SCORE_FIELDS
from backend.app.models import TaskStatus
from backend.app.services.local_consultation import LocalConsultationService, RAW_FIELDS, observation_value, observation_unit
from backend.app.services.evidence_coverage import AGENT_FIELDS

NOTICE = "3秒证据研判已完成：仅使用已加载且通过核验的资料，包含指标解释、证据缺口和固定风险检查；未执行实时查询、多轮模型或完整在线报告审核。"
OMITTED = ["实时查询和资料补取", "多轮模型与完整在线报告审核", "全市场筛选和股票推荐", "收益预测和交易执行", "未明确指向标的的多轮意图推断"]

TOPICS = (
    (r"估值|市盈率|市净率|\bPE\b|\bPB\b", {"pe_ttm", "pe_static", "pe_dynamic", "pb", "valuation_score"}),
    (r"价格|股价|行情|涨跌", {"close_price", "change", "turnover_rate", "bond_price", "net_value"}),
    (r"盈利|财务|营收|利润|ROE|现金流", {"roe", "roe_weighted", "revenue_growth", "net_profit_growth", "net_margin", "gross_margin", "operating_margin", "fundamental_score"}),
    (r"费率|跟踪", {"fee_rate", "tracking_error", "liquidity_score"}),
    (r"溢价|转股|到期", {"conversion_premium_rate", "convert_premium_rate", "yield_to_maturity", "bond_price"}),
)
NOTES = {
    "pe_ttm": "市盈率需要结合同业及盈利稳定性比较，单一数值不足以判定贵贱。",
    "pb": "市净率需结合资产质量和盈利能力，低于1也不等于安全。",
    "roe": "净资产收益率需结合杠杆及现金流，不能单凭高低判断投资价值。",
    "tracking_error": "跟踪误差需在相同统计区间和计算口径下比较。",
    "conversion_premium_rate": "转股溢价率需结合正股波动、债底和赎回条款判断。",
}


def brief_analysis(chosen):
    """Explain relationships supported by the cited observations, without a forecast."""
    by_field = {}
    for fact in chosen:
        previous = by_field.get(fact.field)
        if previous is None or (str(fact.period or ""), fact.snapshot_time) > (str(previous.period or ""), previous.snapshot_time):
            by_field[fact.field] = fact
    fields = {field: observation_value(fact) for field, fact in by_field.items()}
    periods = {field: fact.period for field, fact in by_field.items()}
    revenue = fields.get("revenue_growth")
    profit = fields.get("net_profit_growth")
    if (revenue is not None and profit is not None and periods.get("revenue_growth")
            and periods["revenue_growth"] == periods.get("net_profit_growth")):
        if revenue > 0 > profit:
            return "营收增长而净利润下降，收入增长尚未转化为利润增长；需核查成本、费用及非经常性项目。"
        if revenue < 0 < profit:
            return "营收下降而净利润增长，两者走势背离；需核查利润增长的来源及持续性。"
        if revenue > 0 and profit > revenue:
            return "净利润增速高于营收增速，利润扩张快于收入；仍需核查利润质量及持续性。"
    pe = next((fields[field] for field in ("pe_ttm", "pe_static", "pe_dynamic") if fields.get(field) is not None), None)
    roe = next((fields[field] for field in ("roe", "roe_weighted") if fields.get(field) is not None), None)
    if pe is not None and roe is not None:
        if pe <= 0:
            return "市盈率不为正，通常不宜按常规市盈率比较估值；还需核查盈利口径。"
        if roe <= 0:
            return "市盈率与净资产收益率呈现不同的盈利信号，需核查统计期间及盈利质量。"
        return "市盈率和净资产收益率均有可用数据，但缺少同行基准及盈利稳定性信息，暂不能判断估值是否合理。"
    return ""


# Explain evidence limits as well as observed values. These are reading guides,
# never newly acquired facts, forecasts or completed research claims.
FOLLOW_UP = {
    "industry": [
        "补齐行业营收、利润与订单数据，核对景气改善是否落实到经营表现。",
        "补齐同口径行业估值及历史区间，核对估值与盈利是否匹配。",
        "结合连续交易日资金净流入、成交额与换手率，核对资金持续性和拥挤风险。",
        "核对政策原文、实施时间与适用范围，区分政策预期和已落地影响。",
    ],
    "security": [
        "核对最新财报的营收、利润和经营现金流，确认盈利质量。",
        "补齐同行与历史估值基准，并核对最新公告中的重大事项。",
        "结合连续价格、成交量和换手率，核对行情变化是否有量能配合。",
    ],
    "market": ["补齐增长、通胀、流动性及政策数据，核对宏观信号是否相互支持。"],
    "fund": ["核对基金合同、费率口径、跟踪区间、流动性及风险等级。"],
    "portfolio": ["核对完整持仓、行业集中度及流动性，结合风险承受能力复核配置。"],
}


def evidence_reading(chosen):
    """Return only interpretations anchored to the selected, later-verified facts."""
    fields = {fact.field for fact in chosen}
    reading = []
    if fields & {"change", "industry_median_change"}:
        reading.append("涨跌幅反映对应区间的价格表现；单日上涨不能证明趋势延续，也不能直接说明行业盈利改善。")
    if "industry_median_change" in fields:
        reading.append("行业涨跌幅中值描述成分股的中间位置，与行业指数涨跌幅口径不同，不能直接当作同一指标比较。")
    if "industry_revenue_growth" in fields:
        reading.append("行业营收增长反映收入变化，还需结合利润率、现金流和样本覆盖判断增长质量。")
    if "turnover_rate" in fields or "industry_turnover_percentile" in fields:
        reading.append("换手率描述交易活跃度；活跃既可能来自新增关注，也可能来自分歧，需结合价格与成交额核对。")
    if "capital_flow_score" in fields:
        reading.append("资金流向分数是规则映射结果，不是资金净流入金额，也不是上涨概率；需回查原始净流入及其统计区间。")
    if "valuation_score" in fields:
        reading.append("估值分数是规则映射结果，不是市盈率或市净率；分数为零不表示资产价值为零，也不能据此认定便宜。")
    if fields & SCORE_FIELDS:
        reading.append("规则评分只说明本次规则下的结果，不等同于完整研究评级或收益预测。")
    if any(not fact.period for fact in chosen):
        reading.append("部分指标未注明统计日期或报告期；获取时间不等于数据所属期，不能据此与已注明日期的数据比较或推断最新走势。")
    return reading


def research_next_steps(results):
    roles = dict.fromkeys(result.agent_id for result in results)
    return list(dict.fromkeys(step for role in roles for step in FOLLOW_UP.get(role, [])))


def format_observation(fact):
    value = observation_value(fact)
    period = f"（{fact.period}）" if fact.period else "（统计期未注明）"
    unit = "分（规则推导）" if fact.field in SCORE_FIELDS else observation_unit(fact)
    label = {"capital_flow": "资金净流入", "turnover_value": "成交额"}.get(fact.field, FIELD_LABELS.get(fact.field, "资料指标"))
    return f"{label}{period}为{value:g}{unit}"


def entity_review(entity, chosen, missing):
    raw = [format_observation(fact) for fact in chosen if fact.field not in SCORE_FIELDS]
    scores = [format_observation(fact) for fact in chosen if fact.field in SCORE_FIELDS]
    summary = ("现有证据可描述部分行情或经营表现，但研究维度尚不齐全，暂不能判断投资价值或给出完整综合评级。"
               if missing else "本次仅解读已加载资料；维度有记录不代表已完成在线复核或具备买卖依据。")
    parts = [f"**{entity} · 研判摘要**", summary]
    if raw:
        parts += ["**已核验数据**", *("- " + value for value in raw)]
    if scores:
        parts += ["**规则评分（与原始指标分开阅读）**", *("- " + value for value in scores)]
    analysis = brief_analysis(chosen)
    reading = evidence_reading(chosen)
    if analysis or reading:
        parts += ["**这些数据意味着什么**"]
        if analysis:
            parts.append("简要分析：" + analysis)
        parts += ["- " + value for value in reading]
    if missing:
        parts += ["**证据缺口与判断限制**",
                  "尚缺" + "、".join(FIELD_LABELS.get(field, "研究维度") for field in missing)
                  + "，暂不形成完整综合评分。缺失项表示尚无可用证据，不等于中性、零分或不存在风险。"]
    return "\n\n".join(parts)


def enrich_results(results, facts, query):
    """Add observations and per-entity coverage before fact/conflict verification."""
    focus = set().union(*(fields for pattern, fields in TOPICS if re.search(pattern, query, re.I)))
    enriched = []
    for result in results:
        role = result.agent_id
        if role == "portfolio":
            enriched.append(result)
            continue
        required = AGENT_FIELDS.get(role, ())
        allowed = (RAW_FIELDS[role] | set(required) | {"net_profit_growth", "yield_to_maturity"}
                   | ({"capital_flow", "turnover_value", "pe_ttm", "pb"} if role == "industry" else set()))
        by_entity = defaultdict(dict)
        for fact in facts:
            if fact.field not in allowed or observation_value(fact) is None:
                continue
            if fact.field in SCORE_FIELDS and not 0 <= observation_value(fact) <= 100:
                continue
            key = (fact.field, fact.period)
            prior = by_entity[fact.entity].get(key)
            if prior is None or (fact.snapshot_time, fact.quality) > (prior.snapshot_time, prior.quality):
                by_entity[fact.entity][key] = fact
        if not by_entity or len(by_entity) > 3:
            enriched.append(result)
            continue
        observations, selected, coverage = [], [], {}
        for entity, records in sorted(by_entity.items()):
            # A dated record with the same value carries more context than a
            # duplicate undated cell returned by another capability.
            dated = [f for f in records.values() if f.period]
            unique = [f for f in records.values() if f.period or not any(
                old.field == f.field and observation_value(old) == observation_value(f)
                and (old.unit == f.unit or not old.unit or not f.unit) for old in dated)]
            ordered = sorted(unique, key=lambda f: (f.field not in focus, f.field in SCORE_FIELDS, f.field, str(f.period)))
            chosen = ordered[:16]
            available = {fact.field for fact in chosen}
            missing = [field for field in required if field not in available]
            coverage[entity] = {"available_fields": sorted(available), "missing_fields": missing}
            paragraph = entity_review(entity, chosen, missing)
            observations.append(paragraph)
            selected.extend(chosen)
        fields_used = {fact.field for fact in selected}
        notes = [text for field, text in NOTES.items() if field in fields_used and (not focus or field in focus)]
        missing_topics = []
        if re.search(r"价格|股价|行情", query) and not fields_used.intersection({"close_price", "bond_price", "net_value"}):
            missing_topics.append("本次价格资料未通过时效或口径核验，不能回答最新价格；请先刷新行情。")
        steps = FOLLOW_UP.get(role, [])
        opinion = "\n\n".join([*observations, *notes, *missing_topics,
                                  *(["**下一步重点核实**", *("- " + step for step in steps)] if steps else [])])
        enriched.append(result.model_copy(update={
            "opinion": opinion, "status": TaskStatus.COMPLETED,
            "facts_used": list(dict.fromkeys([*result.facts_used, *(f.fact_id for f in selected)])),
            "citations": sorted(set(result.citations) | {f.source_id for f in selected}),
            "score": None, "rule_score": None,
            "details": {**result.details, "coverage_by_entity": coverage, "engine": "local_evidence_review"},
        }))
    return enriched


class InstantResearchService(LocalConsultationService):
    def __init__(self):
        super().__init__(detailed=True)

    async def run(self, request, understanding, *, progress_sink=None):
        prepared, acquisition, advice = await super().run(request, understanding, progress_sink=progress_sink)
        scope = "evidence_review" if advice.response_scope == "snapshot" else advice.response_scope
        conclusion = advice.conclusion.replace("精简资料概要：", "证据研判：").replace("精简咨询", "证据研判").replace("请选择研究页面或使用深入研究处理自由问答", "请明确研究标的，复杂问题可使用后台完整报告")
        conclusion = conclusion.replace("或选择深入研究", "或在后台预生成完整报告").replace("请选择深入研究", "请在后台预生成完整报告")
        notice = (NOTICE if scope == "evidence_review" else
                  "概念解释已完成；本次未查询行情，也未生成标的研究报告。" if scope == "education" else
                  "请求检查已完成；本次未生成证据研判或投资建议，请查看下方原因。")
        acquisition = acquisition.model_copy(update={"reason_code": "INSTANT_EVIDENCE_REVIEW", "message": notice})
        advice = advice.model_copy(update={
            "conclusion": conclusion, "response_scope": scope, "scope_notice": notice,
            "omitted_features": list(OMITTED),
            "return_expectation": advice.return_expectation.model_copy(update={"summary": "3秒证据研判不计算收益预测。"}),
            "next_steps": (research_next_steps(advice.agent_results) + ["补齐资料后重新研判；需要在线补取与多轮分析时，可在后台生成完整报告"]
                           if scope == "evidence_review" else advice.next_steps),
            "compliance": advice.compliance.model_copy(update={
                "reason": advice.compliance.reason.replace("精简咨询", "证据研判").replace("精简范围", "本地证据范围")
                if advice.compliance.reason else None,
            }),
        })
        return prepared, acquisition, advice
