"""Compose a bounded dossier after verification; never acquire data or call a model."""
from collections import defaultdict
from datetime import date, datetime, timezone
from calendar import monthrange
from zoneinfo import ZoneInfo
import re
from urllib.parse import urlsplit

from backend.app.fact_taxonomy import FIELD_LABELS, SCORE_FIELDS, NEWS_FIELDS, fact_is_current
from backend.app.models import ComplianceStatus
from backend.app.models.research_report import (
    ReportCoverage,
    ResearchEvidence,
    ResearchFinding,
    ResearchReport,
    ResearchSection,
)
from backend.app.answer_text import clean_answer_text
from backend.app.services.local_consultation import observation_value, observation_unit

DIMENSIONS = (
    ("行情与交易", {"close_price", "change", "industry_median_change", "turnover_rate", "volume", "technical_score"},
     "单个时点不能确认趋势，需结合连续价格、成交量及同口径基准。",
     "价格方向或量价配合发生变化时，重新核对行情判断。"),
    ("经营与盈利", {"revenue", "net_profit", "revenue_growth", "net_profit_growth", "industry_revenue_growth",
                    "roe", "roe_weighted", "gross_margin", "net_margin", "fundamental_score", "prosperity_score"},
     "需核对财报期间、经营现金流及非经常性损益，正增长不等于盈利质量改善。",
     "新财报出现增长放缓、利润与现金流背离时，重新评估经营判断。"),
    ("估值与定价", {"pe_ttm", "pe_static", "pe_dynamic", "pb", "valuation_score", "bond_price",
                    "conversion_premium_rate", "convert_premium_rate", "yield_to_maturity"},
     "缺少同口径同行及历史分位时，不判断便宜或昂贵；规则分数不等于估值倍数。",
     "盈利口径、市场价格、可比估值或转债条款变化时，重新评估定价。"),
    ("资金与拥挤度", {"capital_flow", "turnover_value", "capital_flow_ratio", "capital_flow_score",
                      "industry_turnover_percentile", "crowding_score"},
     "单日净流入不等于持续增配；需结合连续资金、成交额与换手率。",
     "资金方向反转或交易拥挤明显上升时，重新核对资金支持。"),
    ("宏观与政策", {"cpi", "ppi", "pmi", "interest_rate", "social_financing", "m2_growth",
                    "growth_score", "inflation_score", "liquidity_score", "policy_score", "risk_appetite_score"},
     "指标所属期与政策实施时间需核对，不能直接从宏观指标推出个股涨跌。",
     "新宏观数据或政策原文改变原有假设时，重新研究。"),
    ("基金与持仓", {"fee_rate", "tracking_error", "fund_risk_level", "fund_score", "net_value",
                    "weight", "portfolio_weight"},
     "需结合基金合同、投资期限及完整持仓判断适配性，单项费率或权重不能替代风险评估。",
     "基金规则、持仓比例或个人风险承受能力变化时，重新评估适配性。"),
    ("新闻与事件", set(NEWS_FIELDS) | {"event", "event_score", "governance_score"},
     "资料标题或摘要只用于定位事件，不能替代政策、公告及财报原文核实。",
     "正式公告澄清事件或后续落地情况改变时，重新核对事件影响。"),
)
FIELD_DIMENSION = {field: index for index, (_, fields, _, _) in enumerate(DIMENSIONS) for field in fields}
LABELS = {"capital_flow": "资金净流入", "turnover_value": "成交额", "net_value": "单位净值"}
DIRECTIONAL = {
    "change": ("价格上涨", "价格下跌", "价格表现不代表未来收益或经营改善。"),
    "revenue_growth": ("营业收入同比增长", "营业收入同比下降", "需结合利润率及现金流核对增长质量。"),
    "net_profit_growth": ("归母净利润同比增长", "归母净利润同比下降", "需核查非经常性损益与利润持续性。"),
    "industry_revenue_growth": ("行业收入同比增长", "行业收入同比下降", "需核对行业样本及利润表现。"),
    "capital_flow": ("本期资金净流入", "本期资金净流出", "需结合统计口径与连续交易日判断持续性。"),
}


def safe_url(value):
    try:
        url = urlsplit(value or "")
        return value if url.scheme in {"https", "http"} and url.hostname and not url.username and not url.password else None
    except ValueError:
        return None


def period_end(fact):
    if fact.observation_date:
        return fact.observation_date
    value = fact.period or ""
    try:
        if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
        match = re.fullmatch(r"(20\d{2})Q([1-4])", value)
        if match:
            year, month = int(match[1]), int(match[2]) * 3
            return date(year, month, monthrange(year, month)[1])
        if re.fullmatch(r"20\d{2}", value):
            return date(int(value), 12, 31)
    except ValueError:
        pass
    return None


def dated(fact):
    return period_end(fact) is not None


def _format_observation(value: float, unit: str) -> str:
    """金额按万/亿换算，百分比保留两位，避免报告里出现 2.2e+08。"""

    if unit in {"元", "CNY"}:
        if abs(value) >= 1e12:
            return f"{value / 1e12:,.2f} 万亿元"
        if abs(value) >= 1e8:
            return f"{value / 1e8:,.2f} 亿元"
        if abs(value) >= 1e4:
            return f"{value / 1e4:,.2f} 万元"
        return f"{value:,.2f} 元"
    if unit == "%":
        return f"{value:,.2f}%"
    if not unit:
        return f"{value:,.2f}" if not float(value).is_integer() else f"{value:,.0f}"
    return f"{value:,.2f}{unit}" if not float(value).is_integer() else f"{value:,.0f}{unit}"


def evidence_item(fact):
    value = observation_value(fact)
    label = LABELS.get(fact.field, FIELD_LABELS.get(fact.field, "资料指标"))
    period = str(fact.observation_date or fact.period or "统计期未注明")
    if value is None:
        if fact.field not in NEWS_FIELDS | {"event"} or not isinstance(fact.value, str):
            return None
        observation = label + "：" + fact.value[:180]
        interpretation = "事件资料待核对原文、发布时间和实际影响，暂不直接判断利好或利空。"
    else:
        unit = "分（规则推导）" if fact.field in SCORE_FIELDS else observation_unit(fact)
        observation = f"{label}：{_format_observation(value, unit)}"
        interpretation = "该指标描述对应统计口径，需结合其他维度判断。"
    stance = "neutral"
    if value is not None and fact.field in DIRECTIONAL and dated(fact) and (fact.field != "capital_flow" or fact.unit in {"CNY", "元", "万元", "亿元"}):
        positive, negative, limit = DIRECTIONAL[fact.field]
        stance = "favorable" if value > 0 else "caution" if value < 0 else "neutral"
        interpretation = ("本条数据反映" + (positive if value > 0 else negative) + "；" + limit
                          if value != 0 else "本条记录未显示方向性变化；" + limit)
    if fact.field in SCORE_FIELDS:
        interpretation = "这是规则映射结果，不是原始行情、资金金额、上涨概率或收益预测。"
    if not dated(fact):
        stance = "neutral"
        interpretation += "统计日期或报告期不完整，不能据此判断最新方向。"
    return ResearchEvidence(observation=observation, interpretation=interpretation, stance=stance,
                            fact_ids=[fact.fact_id], period=period, source=fact.source_id,
                            source_url=safe_url(fact.source_url))


def build_research_report(advice, *, now=None):
    """Use final cited evidence only; conflict descendants never become signals."""
    if (advice.compliance.status == ComplianceStatus.BLOCK
            or advice.response_scope not in {"full", "snapshot", "evidence_review"} or not advice.evidence):
        return None
    current = now or datetime.now(timezone.utc)
    facts = {fact.fact_id: fact for fact in advice.facts}
    allowed = set(advice.evidence)
    rejected = {fid for issue in advice.cross_validation.issues
                if issue.code in {"INTERNAL_VALUE_CONFLICT", "SOURCE_VALUE_CONFLICT", "EVIDENCE_REFERENCE_REJECTED"}
                for fid in issue.fact_ids}
    # Iteratively validate lineage, including parents not directly cited.
    from collections import deque
    remaining, children, valid, ready = {}, defaultdict(list), {}, deque()
    for fid, fact in facts.items():
        parents = set(fact.derived_from)
        remaining[fid] = len(parents & facts.keys())
        valid[fid] = (fid not in rejected and fact_is_current(fact, current)
                      and (period_end(fact) is None or period_end(fact) <= current.astimezone(ZoneInfo("Asia/Shanghai")).date())
                      and parents <= facts.keys()
                      and not (fact.source_id.upper().startswith("DERIVED_") and not parents))
        for parent in parents & facts.keys():
            children[parent].append(fid)
        if not remaining[fid]:
            ready.append(fid)
    safe = set()
    while ready:
        fid = ready.popleft()
        if valid[fid]:
            safe.add(fid)
        for child in children[fid]:
            valid[child] = valid[child] and valid[fid]
            remaining[child] -= 1
            if not remaining[child]:
                ready.append(child)
    groups = defaultdict(dict)
    for fid in sorted(allowed & safe):
        fact = facts[fid]
        index = FIELD_DIMENSION.get(fact.field)
        if index is None:
            continue
        # Keep period bases separate: annual and quarterly data are not substitutes.
        key = (fact.field, fact.period, str(fact.observation_date or ""), observation_unit(fact))
        records = groups[(fact.entity, index)]
        prior = records.get(key)
        if prior is None or fact.snapshot_time > prior.snapshot_time:
            records[key] = fact
    sections = []
    omitted = 0
    for (entity, index), records in sorted(groups.items()):
        if len(sections) >= 16:
            omitted += 1
            continue
        title, _, limitation, condition = DIMENSIONS[index]
        ordered = sorted(records.values(), key=lambda f: str(f.observation_date or f.period or ""), reverse=True)
        items = [item for fact in ordered[:6] if (item := evidence_item(fact)) is not None]
        if not items:
            continue
        limits = [limitation]
        if len(records) > 6:
            limits.append("本维度仅展示部分引用资料，完整数值请展开数据来源。")
        sections.append(ResearchSection(entity=entity, title=title, evidence=items,
                                        limitations=limits, change_conditions=[condition]))
    limitations = []
    if rejected:
        limitations.append("冲突指标及其派生结果已从证据对照中撤下，需核对原始来源。")
    if omitted:
        limitations.append("本报告仅展示部分标的及维度，完整记录请展开来源与分项分析。")
    if advice.research_mode in {"instant", "lite"}:
        limitations.append("本次只整理已加载资料，未新增在线查询或模型辩论。")
    status = ("needs_review" if rejected or advice.cross_validation.status != ComplianceStatus.PASS
              else "reviewed" if advice.compliance.status == ComplianceStatus.PASS else "limited")
    cited = [facts[fid] for fid in sorted(allowed & safe)]
    entities = [entity for entity in dict.fromkeys(fact.entity for fact in cited if fact.entity)][:6]
    dimensions = [section.title for section in sections]
    missing_dimensions = [title for title, _, _, _ in DIMENSIONS if title not in set(dimensions)]
    sources = defaultdict(int)
    for fact in cited:
        sources[fact.source_id] += 1
    coverage = ReportCoverage(
        cited=len(cited), available=len(advice.facts),
        dated=sum(1 for fact in cited if fact.observation_date or period_end(fact)),
        dimensions=list(dict.fromkeys(dimensions)),
        missing_dimensions=missing_dimensions,
        sources=dict(sorted(sources.items(), key=lambda item: -item[1])),
    )
    # 关键信息按维度提炼：每个维度取第一条有方向性的证据，避免同类资料堆砌。
    findings: list[ResearchFinding] = []
    for section in sections:
        candidate = next((item for item in section.evidence if item.stance != "neutral"), None)
        if candidate is not None:
            findings.append(ResearchFinding(
                title=f"{section.entity} · {section.title}",
                detail=f"{candidate.observation}。{candidate.interpretation}",
                stance=candidate.stance, fact_ids=candidate.fact_ids,
            ))
    favorable = [item for item in findings if item.stance == "favorable"][:4]
    caution = [item for item in findings if item.stance == "caution"][:4]
    neutral = [item for item in findings if item.stance == "neutral"][:2]
    key_findings = favorable + caution + neutral
    if not key_findings and sections:
        first = sections[0]
        if first.evidence:
            key_findings = [ResearchFinding(title=f"{first.entity} · {first.title}",
                                            detail=first.evidence[0].observation,
                                            stance="neutral", fact_ids=first.evidence[0].fact_ids)]
    summary = (f"已按标的整理 {len(cited)} 条引用资料，覆盖 {len(coverage.dimensions)} 个维度；"
               "支持因素与不利因素分别呈现，不以平均分代替判断。"
               if sections else "现有引用尚不足以形成证据对照，请先补齐并核对资料。")
    title = ("、".join(entities) + " 投资分析报告") if entities else "投资分析报告"
    return ResearchReport(
        version=2, title=title, generated_at=current, targets=entities,
        status=status, summary=summary, conclusion=advice.conclusion,
        key_findings=key_findings, coverage=coverage, sections=sections, limitations=limitations,
        method=("按已通过核验的引用资料，以固定维度（行情、经营、估值、资金、宏观、基金、新闻）"
                "提取关键指标并逐条标注口径、期间与来源；同一维度的资料不做加权平均。"),
    )


def research_report_markdown(report: ResearchReport, *, question: str | None = None) -> str:
    """把结构化报告渲染成可下载的 Markdown；不重新解释或扩写结论。"""

    lines = [f"# {report.title or '投资分析报告'}", ""]
    if report.generated_at:
        lines.append(f"- 生成时间：{report.generated_at.astimezone(ZoneInfo('Asia/Shanghai')):%Y-%m-%d %H:%M}（北京时间）")
    if question:
        lines.append(f"- 研究问题：{question}")
    if report.targets:
        lines.append(f"- 研究对象：{'、'.join(report.targets)}")
    lines.append(f"- 审核状态：{ {'reviewed': '已核验', 'needs_review': '待复核', 'limited': '范围受限'}.get(report.status, report.status) }")
    lines.extend(["", "## 一、结论", "", clean_answer_text(report.conclusion) or "资料不足，暂未形成结论。"])
    if report.key_findings:
        lines.extend(["", "## 二、关键信息", ""])
        for finding in report.key_findings:
            stance = {"favorable": "有利", "caution": "不利", "neutral": "中性/待核实"}[finding.stance]
            if detail := clean_answer_text(finding.detail):
                lines.append(f"- **[{stance}] {finding.title}**：{detail}")
    if report.coverage.cited:
        coverage = report.coverage
        lines.extend([
            "", "## 三、数据覆盖", "",
            f"- 引用资料 {coverage.cited} 条 / 本次可用 {coverage.available} 条，其中带明确日期 {coverage.dated} 条。",
            f"- 已覆盖维度：{'、'.join(coverage.dimensions) or '无'}。",
            f"- 未覆盖维度：{'、'.join(coverage.missing_dimensions) or '无'}。",
            f"- 来源分布：{'；'.join(f'{name} {count} 条' for name, count in coverage.sources.items()) or '无'}。",
        ])
    if report.sections:
        lines.extend(["", "## 四、分维度证据", ""])
        for section in report.sections:
            lines.append(f"### {section.entity} · {section.title}")
            for item in section.evidence:
                stance = {"favorable": "有利", "caution": "不利", "neutral": "中性"}[item.stance]
                period = item.period or "统计期未注明"
                lines.append(f"- [{stance}] {item.observation}（{period}）")
                if interpretation := clean_answer_text(item.interpretation):
                    lines.append(f"  - 解读：{interpretation}")
                source = item.source_url or item.source
                lines.append(f"  - 来源：{source}")
            lines.extend(f"- 判断限制：{text}" for value in section.limitations if (text := clean_answer_text(value)))
            lines.extend(f"- 重新评估条件：{text}" for value in section.change_conditions if (text := clean_answer_text(value)))
            lines.append("")
    if report.limitations:
        lines.extend(["## 五、报告限制", ""])
        lines.extend(f"- {text}" for value in report.limitations if (text := clean_answer_text(value)))
    lines.extend(["", "---", clean_answer_text(report.method)])
    return "\n".join(lines)


def attach_research_report(advice):
    return advice.model_copy(update={"research_report": build_research_report(advice)})
