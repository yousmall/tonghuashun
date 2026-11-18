"""Completed, bounded consultation using supplied snapshots and fixed rules.

This intentionally has no model, provider, document reader or recovery hook.
Free-form intent inference, forecasting and fresh screening belong to deep mode.
"""
from __future__ import annotations

import re
from uuid import uuid4

from backend.app.agents.coordinator import (
    CoordinatorAgent, basic_compliance_check, cross_validate_results,
    merge_compliance, semantic_compliance, verify_facts,
)
from backend.app.agents.rule_agents import make_rule_agents
from backend.app.fact_taxonomy import FIELD_LABELS, SCORE_FIELDS
from backend.app.fact_units import PERCENT_FIELDS
from backend.app.numeric import finite_number
from backend.app.models import (
    AdvicePackage, AgentResult, ComplianceResult, ComplianceStatus, DataAcquisitionResult,
    Intent, OrchestrationRequest, ReturnExpectation, TaskNode, TaskPlan, TaskStatus,
)
from backend.app.research_routing import DIRECTION_FOR_INTENT, user_question
from backend.app.risk_questionnaire import assessment_is_current
from backend.app.semantic import RequestUnderstanding
from backend.app.services.research import AutomatedResearchPipeline
from backend.app.services.context_link import carry_context_entity
from backend.app.services.comparison_review import review_comparison
from backend.app.services.evidence_coverage import AGENT_FIELDS

OMITTED = ["实时取数与资料补取", "复杂筛选与股票推荐", "收益预测", "多轮模型解读"]
NOTICE = "精简咨询已完成，仅整理已有资料并检查固定风险约束；最新数据、复杂问答和详细研判请使用深入研究。"
CONCEPTS = {
    "市盈率": "市盈率＝股价÷每股收益，反映价格相对盈利的水平。亏损时通常不适用，应结合行业、盈利稳定性与增长看待，不能单凭高低决定买卖。",
    "市净率": "市净率＝股价÷每股净资产，反映价格相对账面净资产的水平。资产质量和行业差异会影响比较，低市净率不等于低风险。",
    "ROE": "ROE是净资产收益率，通常以净利润与平均净资产的比例衡量股东资金的盈利能力。高杠杆也可能推高ROE，需要结合负债和现金流观察。",
    "ETF": "ETF是可在交易所买卖的基金，常用于跟踪指数。需要比较跟踪误差、费用、成交活跃度与所跟踪资产的风险；交易便利不代表不会亏损。",
    "最大回撤": "最大回撤是某一观察区间内从高点到随后低点的最大跌幅，描述历史下跌幅度。不同观察区间的回撤可能差异很大。",
    "分散投资": "分散投资是把资金分配到不同资产或风险来源，减少单一标的对组合的影响。相关性较高的资产可能同时下跌，分散不能消除市场风险。",
}
ROLES = {
    Intent.MARKET_ANALYSIS: ("market", "industry"), Intent.INDUSTRY_ANALYSIS: ("industry",),
    Intent.SECURITY_RESEARCH: ("security",), Intent.CONVERTIBLE_BOND_ANALYSIS: ("security",),
    Intent.FUND_SCREENING: ("fund",), Intent.PORTFOLIO_REVIEW: ("portfolio",),
}
RAW_FIELDS = {
    "market": {"cpi", "ppi", "pmi", "interest_rate", "social_financing", "m2_growth", "close_price", "change"},
    "industry": {"industry_revenue_growth", "industry_median_change", "industry_turnover_percentile", "change"},
    "security": {"close_price", "change", "pe_ttm", "pe_static", "pe_dynamic", "pb", "roe", "roe_weighted",
                 "revenue_growth", "net_profit_growth", "gross_margin", "net_margin", "operating_margin", "turnover_rate",
                 "convert_premium_rate", "conversion_premium_rate", "bond_price"},
    "fund": {"fee_rate", "tracking_error", "liquidity_score", "fund_score", "net_value", "change"},
    "portfolio": {"weight", "portfolio_weight"},
}


def observation_value(fact):
    """Read finite numeric text without rewriting the audited source value."""
    if fact.unit == "conflicting":
        return None
    if fact.field in PERCENT_FIELDS:
        return fact.normalized_value
    return finite_number(fact.value, allow_text=True)


def observation_unit(fact):
    if fact.field in PERCENT_FIELDS:
        return "%"
    if fact.unit:
        return {"CNY": "元", "USD": "美元", "HKD": "港元"}.get(fact.unit, fact.unit)
    if fact.field in SCORE_FIELDS:
        return "分（规则推导）"
    if fact.field in {"pe_ttm", "pe_static", "pe_dynamic", "pb"}:
        return "倍"
    if fact.field in {"close_price", "bond_price", "net_value"}:
        return "（单位未注明）"
    return ""


def concept_for(query):
    question = user_question(query).strip(" 。？?！!")
    aliases = {"PE": "市盈率", "PB": "市净率"}
    for alias, name in aliases.items():
        question = re.sub(r"(?<![A-Za-z])" + alias + r"(?![A-Za-z])", name, question, flags=re.I)
    for name in CONCEPTS:
        accepted = {name, f"{name}是什么", f"什么是{name}", f"解释{name}", f"解释一下{name}",
                    f"请解释{name}", f"请解释{name}的含义", f"请解释{name}的含义，不推荐股票"}
        if question.casefold() in {value.casefold() for value in accepted}:
            return name
    return None


def entity_mentioned(fact, question):
    """Names match literally; numeric codes must have complete token boundaries."""
    from backend.app.services.security_identity import _stock_symbols
    code = str(fact.entity_code or "").upper().split(".")[0]
    if code and code in _stock_symbols(question, [], stock_page=True):
        from backend.app.services.security_identity import _market_suffix
        if str(fact.entity_code).upper() in {code, f"{code}.{_market_suffix(code)}"}:
            return True
    names = [fact.entity, fact.entity_code or ""]
    for name in names:
        if not name:
            continue
        if re.fullmatch(r"\d{6}(?:\.(?:SH|SZ|BJ))?", name, re.I):
            tokens = re.findall(r"(?<![0-9A-Za-z_])([0-9]{6}(?:\.(?:SH|SZ|BJ))?)(?![0-9A-Za-z_])", question, re.I)
            if any(token.upper() == name.upper() if "." in token else token == name[:6] for token in tokens):
                return True
        elif name.casefold() in question.casefold():
            return True
    return False


def select_scope(request):
    """Explicit page scope or a small exact task catalog; never guesses free-form intent."""
    question = user_question(request.query).strip(" 。？?！!")
    if concept_for(question):
        return RequestUnderstanding(intent=Intent.EDUCATION, confidence=1, risk_rules=[], reason="标准概念解释")
    risk_patterns = {
        "PRIVACY_AND_PERMISSION": r"(?:其他|别人|另一个|所有|管理员).{0,12}(?:用户|账号|账户|客户).{0,20}(?:持仓|密钥|密码|资料|资产)|(?:泄露|打印|显示|给我|读取).{0,15}(?:api.?key|密钥|系统提示词|密码)",
        "NO_RETURN_PROMISE": r"稳赚|(?:保证|承诺).{0,8}(?:盈利|收益|翻倍)|推荐保本",
    }
    risk_question = re.sub(r"(?:不用|无需|不需要|不要求|不要)(?:你|给我)?(?:保证|承诺)(?:投资)?(?:盈利|收益|回报)", "", question)
    for rule, pattern in risk_patterns.items():
        if re.search(pattern, risk_question, re.I):
            return RequestUnderstanding(intent=Intent.UNKNOWN, confidence=1, risk_rules=[rule], reason="咨询问题超出了资料查询和分析范围。")
    if re.search(r"(?:执行|替我|帮我|直接|自动).{0,8}(?:买入|下单|卖出|交易|删除)|忽略.{0,8}(?:规则|指令)|<script|drop\s+table|system:", question, re.I):
        return RequestUnderstanding(intent=Intent.UNKNOWN, confidence=1, risk_rules=[], reason="请求不在只读研究范围内")
    from backend.app.services.security_identity import _stock_symbols
    symbols = _stock_symbols(question, request.facts, stock_page=request.research_direction == "个股研究")
    # Page selection alone must never turn an unrelated message into a market view.
    relevant = re.search(r"市场|大盘|指数|宏观|行业|板块|股票|个股|公司|基金|ETF|可转债|持仓|组合|估值|市盈率|市净率|风险|财务|营收|利润|净值|净资产|现金流|行情|价格|股价|涨跌|概览|资料|标的|评分|研判|政策|费率|溢价|跟踪|ROE|\bPE\b|\bPB\b|(?<!\d)\d{6}(?!\d)", question, re.I)
    if not relevant and not symbols and not any(entity_mentioned(f, question) for f in request.facts):
        return RequestUnderstanding(intent=Intent.UNKNOWN, confidence=0, risk_rules=[], reason="请说明研究标的及问题")
    denied = {"推荐稳赚股票": "NO_RETURN_PROMISE", "给我其他用户的持仓和密钥": "PRIVACY_AND_PERMISSION"}
    if question in denied:
        return RequestUnderstanding(intent=Intent.UNKNOWN, confidence=1, risk_rules=[denied[question]], reason="咨询问题超出了资料查询和分析范围。")
    intent = next((intent for intent, label in DIRECTION_FOR_INTENT.items()
                   if label == request.research_direction), Intent.UNKNOWN)
    if request.comparison_targets and intent in ROLES and intent is not Intent.PORTFOLIO_REVIEW:
        return RequestUnderstanding(intent=intent, confidence=1, risk_rules=[], reason="解读所选自选组的可比指标。")
    catalog = {"请诊断我的持仓组合": Intent.PORTFOLIO_REVIEW, "帮我看看持仓": Intent.PORTFOLIO_REVIEW}
    if request.research_direction == "市场解读" and (symbols or re.search(r"(?<!\d)\d{6}(?!\d)|个股|股票|基金|可转债", question)):
        return RequestUnderstanding(intent=Intent.UNKNOWN, confidence=1, risk_rules=[], reason="LOCAL_CLARIFY:这个问题涉及具体标的，请切换对应的个股、基金或可转债研究页面。")
    if intent is Intent.UNKNOWN:
        intent = Intent.SECURITY_RESEARCH if symbols else catalog.get(question, Intent.UNKNOWN)
        if re.fullmatch(r"请(?:研究|分析).{1,60}(?:这只个股|股票)", question):
            intent = Intent.SECURITY_RESEARCH
        elif re.fullmatch(r"请分析.{1,60}可转债", question):
            intent = Intent.CONVERTIBLE_BOND_ANALYSIS
    return RequestUnderstanding(intent=intent, confidence=1 if intent is not Intent.UNKNOWN else 0,
                                risk_rules=[], reason="按已选择的页面范围整理已有资料；未推断自由问句意图。")


class LocalConsultationService:
    def __init__(self, *, detailed=False):
        self.detailed = detailed
        self.pipeline = AutomatedResearchPipeline(None)
        self.coordinator = CoordinatorAgent(make_rule_agents(), verify_facts, basic_compliance_check)

    async def run(self, request, understanding, *, progress_sink=None):
        trace_id = "L-" + uuid4().hex[:12].upper()
        intent = understanding.intent
        plan = TaskPlan(trace_id=trace_id, intent=intent)
        acquisition = DataAcquisitionResult(mode="not_required", reason_code="LOCAL_CONSULTATION",
                                            message=NOTICE, supplied_fact_count=len(request.facts))
        def simple(conclusion, scope="clarification", compliance=None):
            advice = AdvicePackage(trace_id=trace_id, intent=intent, conclusion=conclusion, confidence=0,
                task_plan=plan, compliance=compliance or ComplianceResult(status=ComplianceStatus.REVIEW,
                    matched_rules=["LIMITED_CONSULTATION"], reason="精简咨询仅提供资料概要与固定风险检查。"),
                response_scope=scope, scope_notice=NOTICE, omitted_features=list(OMITTED),
                next_steps=["需要最新资料或复杂问答时，请选择深入研究"],
                return_expectation=ReturnExpectation(summary="精简咨询不计算收益预测。", assumptions=[]))
            return request.model_copy(update={"facts": []}), acquisition, advice
        if understanding.risk_rules:
            return simple("请将问题限定在具体标的、公开资料或您的账户范围内。", compliance=semantic_compliance(
                understanding.risk_rules, understanding.reason, risk_level=request.profile.risk_level))
        if not assessment_is_current(request.profile):
            return simple("请先完成并确认有效的风险测评。")
        concept = concept_for(request.query)
        if concept:
            plan.nodes = [TaskNode(task_id="concept-explanation", agent_id="education", status=TaskStatus.COMPLETED),
                          TaskNode(task_id="fixed-risk-check", agent_id="compliance", status=TaskStatus.COMPLETED,
                                   depends_on=["concept-explanation"])]
            prepared, audit, advice = simple(CONCEPTS[concept], "education")
            return prepared, audit, advice.model_copy(update={"scope_notice": "精简咨询已完成，本次为标准概念解释，未查询实时行情。"})
        if intent is Intent.UNKNOWN:
            if understanding.reason.startswith("LOCAL_CLARIFY:"):
                return simple(understanding.reason.removeprefix("LOCAL_CLARIFY:"))
            return simple("精简咨询支持已有资料概览、持仓集中度检查和常见概念解释。请选择研究页面或使用深入研究处理自由问答。")
        if len(request.facts) > (1000 if self.detailed else 4000) or len(request.portfolio) > 100:
            return simple("本次资料超过精简咨询的处理范围，请选择深入研究。")
        if progress_sink:
            progress_sink("整理已有资料")
        prepared, acquisition = await self.pipeline.prepare(request.model_copy(update={"auto_fetch": False}), intent)
        acquisition = acquisition.model_copy(update={"reason_code": "LOCAL_CONSULTATION", "message": NOTICE})
        probe = AgentResult(agent_id="snapshot", status=TaskStatus.COMPLETED, opinion="资料核验", confidence=0,
                            facts_used=[fact.fact_id for fact in prepared.facts])
        validated = await verify_facts([probe], prepared.facts)
        allowed = set(validated[0].facts_used)
        valid_facts = [fact for fact in prepared.facts if fact.fact_id in allowed]
        # Scope asset and industry pages to the explicitly selected/requested entity.
        # Generic requests require one unambiguous entity.
        carried: str | None = None
        comparison = bool(request.comparison_targets) and intent is not Intent.PORTFOLIO_REVIEW
        if not comparison and intent in {Intent.SECURITY_RESEARCH, Intent.CONVERTIBLE_BOND_ANALYSIS, Intent.FUND_SCREENING, Intent.INDUSTRY_ANALYSIS}:
            question = user_question(request.query)
            generic = question.strip(" 。？?！!") in {"概览", "资料概览", "已有资料概览", "行业概览", "查看已有资料", "分析当前标的", "分析当前股票", "分析这只股票", "分析当前基金", "分析当前转债", "分析当前行业"}
            lookup = request.research_target if generic and request.research_target else question
            matching = {f.entity for f in valid_facts if entity_mentioned(f, lookup)}
            if not matching and not generic:
                # 追问里省略了标的（例如「它的市净率呢」）时，沿用上一轮提到的标的。
                carried = carry_context_entity(request, valid_facts)
                if carried:
                    matching = {carried}
            if generic:
                relevant_fields = RAW_FIELDS[ROLES[intent][0]] | set(AGENT_FIELDS.get(ROLES[intent][0], ()))
                candidates = matching if request.research_target else {f.entity for f in valid_facts if f.field in relevant_fields}
                if len(candidates) != 1:
                    return simple("当前资料涉及多个标的或所选标的资料未取得，请明确名称或代码后再分析。")
                matching = candidates
            requested_codes = set(re.findall(r"(?<![0-9A-Za-z_])([0-9]{6})(?![0-9A-Za-z_])", question))
            loaded_codes = {str(f.entity_code or f.entity).split(".")[0] for f in valid_facts}
            if requested_codes - loaded_codes and matching:
                return simple("您提到的部分标的尚无通过核验的资料，请先加载全部指定标的后再比较。")
            if re.search(r"比较|对比|相比", question) and len(matching) < 2:
                return simple("比较需要至少两个明确标的及相同口径的资料，请补充后再提问。")
            if not matching:
                from backend.app.services.security_identity import _stock_symbols, STOCK_NAMES
                symbols = _stock_symbols(question, [], stock_page=True)
                target = "、".join(f"{next((n for n, c in STOCK_NAMES.items() if c == code), code)}（{code}）" for code in symbols)
                return simple(f"已识别研究标的：{target}。当前没有该标的通过核验的已加载资料。请点击“补查资料并分析”，或切换“深度思考”后重试。" if target else
                              "已有资料无法确定您指定的标的，请加载该标的资料，或选择深入研究。")
            if matching:
                valid_facts = [f for f in valid_facts if f.entity in matching]
        view = prepared.model_copy(update={"facts": valid_facts})
        results = []
        if comparison:
            role = ROLES[intent][0]
            comparison_result = review_comparison(intent, request.comparison_targets, valid_facts, agent_id=role)
            if comparison_result is None:
                return simple("这组标的暂无全部共有且通过核验的同期间、同单位数值。可刷新对比资料，核对期间和单位后再解读。")
            results.append(comparison_result)
            plan.nodes.append(TaskNode(task_id=f"{role}-snapshot", agent_id=role, status=TaskStatus.COMPLETED))
        for role in (() if comparison else ROLES[intent]):
            baseline = await self.coordinator.agents[role](view)
            snapshot = self._snapshot(role, valid_facts)
            # Fund mode shows loaded data, never the rule agent's candidate ranking
            # or suitability-admission claim; those belong to complete research.
            if role != "fund" and baseline.status is TaskStatus.COMPLETED and baseline.facts_used:
                result = baseline
            elif snapshot:
                result = snapshot
                result.risk_flags = list(baseline.risk_flags)
                result.details = {"rule_checks": baseline.details}
            else:
                continue
            result.details = {**result.details, "engine": "local_rules", "snapshot_only": True}
            results.append(result)
            plan.nodes.append(TaskNode(task_id=f"{role}-snapshot", agent_id=role, status=TaskStatus.COMPLETED))
        if self.detailed and not comparison:
            from backend.app.services.instant_research import enrich_results
            results = enrich_results(results, valid_facts, request.query)
        if progress_sink:
            progress_sink("事实核验")
        results = await verify_facts(results, prepared.facts)
        for node, result in zip(plan.nodes, results, strict=True):
            node.status = result.status
        plan.nodes.append(TaskNode(task_id="fact-verification", agent_id="fact_verifier", status=TaskStatus.COMPLETED,
                                   depends_on=[n.task_id for n in plan.nodes]))
        cross = cross_validate_results(results, prepared.facts)
        safe_comparison = None
        if comparison and cross.issues:
            affected_ids = {fact_id for issue in cross.issues for fact_id in issue.fact_ids}
            if affected_ids:
                selected = set(results[0].facts_used) - affected_ids
                kept = [fact for fact in valid_facts if fact.fact_id in selected]
                safe_comparison = review_comparison(intent, request.comparison_targets, kept, agent_id=results[0].agent_id)
            if safe_comparison:
                safe_comparison.risk_flags.extend([
                    *results[0].risk_flags, "已撤下核验中发现冲突的指标记录。",
                ])
                results[0] = safe_comparison
        if cross.issues:
            cross.consensus_score = None
            cross.confidence = 0
            cross.supporting_agents = []
            cross.dissenting_agents = []
            for result in results:
                if result.facts_used and result is not safe_comparison:
                    result.opinion = "资料存在冲突或核验限制，请查看来源差异，暂不形成确定判断。"
                    result.conclusion = None
                    result.score = None
                    result.rule_score = None
        if progress_sink:
            progress_sink("风险检查")
        compliance = merge_compliance(await basic_compliance_check(prepared, results), ComplianceResult(
            status=ComplianceStatus.REVIEW, matched_rules=["LIMITED_CONSULTATION"]))
        plan.nodes.append(TaskNode(task_id="fixed-risk-check", agent_id="compliance", status=TaskStatus.COMPLETED,
                                   depends_on=["fact-verification"]))
        advice = self.coordinator._aggregate(prepared, plan, results, cross, compliance)
        conclusion = ("精简资料概要：\n\n" + "\n\n".join(result.summary_opinion for result in results)
                      if results else "当前没有通过核验的相关资料，无法形成数据概要。请先加载资料，或选择深入研究。")
        if results and (comparison or intent is Intent.PORTFOLIO_REVIEW):
            conclusion = results[0].summary_opinion
            if not cross.issues:
                advice.risk_conclusion = None
        if results and carried:
            conclusion = f"结合上一轮对话，本轮沿用标的：{carried}。\n\n{conclusion}"
        if compliance.status is ComplianceStatus.BLOCK:
            return simple("请求未通过风险检查，未生成投资建议。", compliance=compliance)
        # Return only cited evidence and its lineage/conflict records, not the full page cache.
        keep = set(advice.evidence) | {fid for issue in cross.issues for fid in issue.fact_ids}
        by_id = {f.fact_id: f for f in prepared.facts}
        pending = list(keep)
        while pending:
            fact = by_id.get(pending.pop())
            for fid in fact.derived_from if fact else ():
                if fid not in keep:
                    keep.add(fid)
                    pending.append(fid)
        prepared = prepared.model_copy(update={"facts": [f for f in prepared.facts if f.fact_id in keep]})
        return prepared, acquisition, advice.model_copy(update={"conclusion": conclusion,
            "response_scope": "snapshot" if results else "clarification", "scope_notice": NOTICE,
            "omitted_features": list(OMITTED), "allocation": [],
            "return_expectation": ReturnExpectation(summary="精简咨询不计算收益预测。", assumptions=[]),
            "next_steps": ["最新行情、完整研判或复杂问答请使用深入研究"]})

    @staticmethod
    def _snapshot(role, facts):
        fields = RAW_FIELDS[role] | set(AGENT_FIELDS.get(role, ()))
        latest = {}
        for fact in facts:
            if fact.field not in fields or observation_value(fact) is None:
                continue
            if fact.field in SCORE_FIELDS and not 0 <= observation_value(fact) <= 100:
                continue
            key = (fact.entity, fact.field, fact.period)
            if key not in latest or fact.snapshot_time > latest[key].snapshot_time:
                latest[key] = fact
        chosen = sorted(latest.values(), key=lambda f: (f.field not in RAW_FIELDS[role], f.entity, f.field))[:8]
        if not chosen:
            return None
        parts = []
        for fact in chosen:
            value = observation_value(fact)
            unit = observation_unit(fact)
            parts.append(f"{fact.entity}的{FIELD_LABELS.get(fact.field, '资料指标')}为{value:g}{unit}")
        return AgentResult(agent_id=role, status=TaskStatus.COMPLETED, opinion="已有资料：" + "；".join(parts) + "。",
            confidence=min(f.quality for f in chosen), facts_used=[f.fact_id for f in chosen],
            citations=sorted({f.source_id for f in chosen}), invalidation_conditions=["资料时点或数值变化后需要重新核验"])
