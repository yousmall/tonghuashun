"""投资结果的中文文案、来源和风险展示组件。"""
from __future__ import annotations
import math
import re
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any
import streamlit as st

RISK_LEVELS = ["R1", "R2", "R3", "R4", "R5"]
PROFILE_LABELS = {
    "risk_level": "风险等级",
    "risk_score": "风险评分",
    "horizon_months": "投资期限",
    "max_drawdown": "最大可承受回撤",
    "liquidity_need": "流动性需求",
    "target": "投资目标",
    "single_security_limit": "单一标的上限",
    "industry_limit": "单一行业上限",
    "investment_experience_years": "投资经验",
    "expected_annual_return": "期望年化收益",
}
FIELD_LABELS = {
    "growth_score": "经济增长",
    "inflation_score": "通胀环境",
    "liquidity_score": "市场流动性",
    "policy_score": "政策环境",
    "risk_appetite_score": "风险态度",
    "prosperity_score": "行业景气度",
    "valuation_score": "估值水平",
    "capital_flow_score": "资金流向",
    "crowding_score": "交易拥挤度",
    "fundamental_score": "基本面评分",
    "technical_score": "技术面评分",
    "fund_risk_level": "基金风险等级",
    "fund_score": "基金综合评分",
    "fee_rate": "费率",
    "weight": "持仓权重",
    "close_price": "最新价",
}


FIELD_LABELS.update({
    "change": "涨跌幅", "change_amount": "涨跌额", "volume": "成交量", "turnover_rate": "换手率",
    "pe_ttm": "市盈率TTM", "pe_static": "静态市盈率", "pe_dynamic": "动态市盈率", "pb": "市净率",
    "roe": "净资产收益率", "roe_weighted": "加权净资产收益率",
    "revenue_growth": "营业收入增长率", "tracking_error": "跟踪误差",
    "news": "新闻", "announcement": "公告", "research_report": "研报", "provider_response": "查询摘要",
    "company_name": "公司全称", "industry": "所属行业", "main_business": "主营业务", "listing_date": "上市日期",
    "revenue_composition": "主营构成", "major_customer": "主要客户", "major_supplier": "主要供应商",
    "major_contract": "重大合同", "controlling_shareholder": "控股股东", "actual_controller": "实际控制人",
    "total_shares": "总股本", "float_shares": "流通股本", "shareholder_count": "股东人数",
    "event": "重要事件", "institution": "研究机构", "rating": "机构评级", "target_price": "目标价",
    "earnings_forecast": "盈利预测", "conversion_premium_rate": "转股溢价率", "pure_bond_premium_rate": "纯债溢价率",
    "yield_to_maturity": "到期收益率", "remaining_size": "剩余规模", "bond_rating": "债券评级", "conversion_price": "转股价",
    "cpi": "居民消费价格指数", "ppi": "工业生产者价格指数", "pmi": "采购经理指数",
    "social_financing": "社会融资", "interest_rate": "利率", "event_score": "事件影响评分", "governance_score": "公司治理评分",
})
NAVIGATION = ["主页", "风险评估", "风险调整", "投资问答", "自选研究", "持仓分析"]
# 投资问答由后端自动判断并获取所需资料；以下类型映射仅供保留的资料管理工具使用。
DATA_KINDS = {"实时行情": "quote", "财务指标": "financial", "财经新闻": "news", "公告": "announcement",
              "研报": "research_report", "基金 / ETF": "fund", "行业排名": "industry", "可转债": "convertible"}
# 提问时的研究视角：由界面翻译成一句完整问题交给后端，用户不需要理解"智能体"。
QUICK_ASKS = ["市场解读", "行业分析", "个股研究", "基金筛选", "可转债分析"]
QUICK_ASK_PROMPTS = {
    "市场解读": "请解读当前市场环境、主要机会与风险",
    "行业分析": "请分析我关注行业的景气度与主要风险",
    "个股研究": "请分析这只股票的经营情况、估值与风险：",
    "基金筛选": "请帮我比较和筛选基金 / ETF：",
    "可转债分析": "请分析这只可转债的价格、对应股票与风险：",
}
# 把用户问题转成完整研究请求时使用的前缀，与后端意图识别保持一致。
RESEARCH_PREFIX = {"个股研究": "个股研究：", "行业分析": "行业分析：", "市场解读": "市场解读：",
                   "基金筛选": "基金筛选：", "可转债分析": "可转债分析："}
WATCHLIST_RESEARCH_PREFIX = {
    "股票": "个股研究：",
    "基金": "基金筛选：",
    "行业": "行业分析：",
    "可转债": "可转债分析：",
}
TOPIC_LABELS = {"market": "市场环境", "macro": "市场环境", "industry": "行业分析", "security": "个股研究",
                "stock": "个股研究", "fund": "基金筛选", "portfolio": "持仓分析", "fact_verifier": "资料核对", "compliance": "风险检查"}
PROGRESS_LABELS = {"completed": "已完成", "running": "正在分析", "pending": "等待处理", "degraded": "资料有限",
                   "unknown": "待补充资料", "failed": "暂未完成", "skipped": "本次无需处理"}


def display_value(key: str, value: Any) -> str:
    """将服务端字段转换为面向用户的简洁文本，不暴露原始结构。"""

    if value is None or value == "":
        return "待补充"
    if key == "risk_level":
        return {"R1": "保守型", "R2": "稳健型", "R3": "平衡型", "R4": "成长型", "R5": "进取型"}.get(str(value), "待评估")
    if key in {
        "max_drawdown", "single_security_limit", "industry_limit", "expected_annual_return", "weight", "fee_rate"
    }:
        if isinstance(value, str) and value.strip().endswith(("%", "％")):
            return value.strip()
        try:
            return f"{float(value) * 100:g}%"
        except (ValueError, TypeError):
            return str(value)
    if key == "horizon_months":
        return f"{int(value)} 个月"
    if key == "investment_experience_years":
        return f"{float(value):g} 年"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, list):
        return "、".join(str(item) for item in value) or "无"
    return str(value)


def fact_label(field: str) -> str:
    return FIELD_LABELS.get(field) or (field if re.search(r"[\u4e00-\u9fff]", field) else "其他资料")


def fact_source(fact: dict[str, Any]) -> str:
    source = str(fact.get("source_id", ""))
    if source.startswith("USER_SUPPLIED:"):
        return "用户补充 · " + source.partition(":")[2]
    if fact.get("source_note"):
        return str(fact["source_note"])
    if source.startswith("IWENCAI"):
        return "同花顺问财"
    if source.startswith("DERIVED"):
        return "根据原始资料计算"
    if source == "DEMO_SNAPSHOT":
        return "示例数据"
    if source.startswith("USER_") or source == "MANUAL_SNAPSHOT":
        return "用户提供"
    return "市场数据服务" if source else "来源待确认"


def fact_time(value: Any) -> str:
    if not value:
        return "未提供"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        pass
    return str(value)


def fact_period(value: Any) -> str:
    """把报告期转成可读日期；内部逐条标识不展示给用户。"""

    text = str(value or "").strip()
    if not text:
        return "—"
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if re.fullmatch(r"\d{6}", text):
        return f"{text[:4]}-{text[4:]}"
    if re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", text):
        return text
    if text.startswith("REC-") or re.fullmatch(r"[A-Za-z0-9_\-]{8,}", text):
        return "近期"
    return text


def friendly_fact_rows(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """展示全部业务资料，新闻及未映射指标也保留，不暴露内部标识。"""
    rows = []
    for fact in facts:
        value = fact.get("value")
        if isinstance(value, dict):
            value = "；".join(f"{fact_label(str(key))}：{display_value(str(key), item)}"
                             for key, item in value.items() if key not in {"trace_id", "fact_id", "source_id", "quality"})
        rows.append({
            "对象": fact.get("entity", "—"), "指标": fact_label(str(fact.get("field", ""))),
            "内容 / 数值": display_value(str(fact.get("field", "")), value),
            "数据时间（北京时间）": fact_time(fact.get("snapshot_time")),
            "来源": fact_source(fact), "报告期": fact_period(fact.get("period")),
        })
    return rows


def advice_facts(advice: dict[str, Any]) -> list[dict[str, Any]]:
    # 显式空资料也属于该结果；只有旧格式缺少 facts 时兼容当前会话。
    return advice["facts"] if "facts" in advice else st.session_state.get("facts", [])


def _status_code(value: Any, *, uppercase: bool = False) -> str:
    """兼容枚举、API 字符串和历史快照中的状态写法。"""

    raw = getattr(value, "value", value)
    code = str(raw or "").strip().split(".")[-1]
    return code.upper() if uppercase else code.lower()


def analysis_chain_stages(advice: dict[str, Any]) -> list[dict[str, str]]:
    """把现有建议包映射为展示链路；只汇总，不生成或改写投资判断。"""

    facts = advice_facts(advice)
    available_ids = {str(fact.get("fact_id")) for fact in facts if fact.get("fact_id")}
    used_ids = {str(item) for item in advice.get("evidence", [])}
    used = [fact for fact in facts if str(fact.get("fact_id", "")) in used_ids]
    results = advice.get("agent_results", [])
    result_statuses = [_status_code(item.get("status")) for item in results]
    terminal_statuses = {"completed", "degraded", "unknown", "failed", "skipped"}
    reviewed = sum(status in terminal_statuses for status in result_statuses)
    completed = result_statuses.count("completed")
    limited = sum(status in {"degraded", "unknown"} for status in result_statuses)
    failed = result_statuses.count("failed")
    skipped = result_statuses.count("skipped")
    active = len(results) - reviewed
    review_notes = []
    if completed:
        review_notes.append(f"{completed} 项结论完整")
    if limited:
        review_notes.append(f"{limited} 项资料有限")
    if failed:
        review_notes.append(f"{failed} 项暂未形成结论")
    if skipped:
        review_notes.append(f"{skipped} 项本次无需处理")
    if active:
        review_notes.append(f"{active} 项仍在处理中")
    facts_by_id = {str(fact.get("fact_id")): fact for fact in facts if fact.get("fact_id")}
    source_ids: set[str] = set()

    def collect_source(fact_id: str, visited: set[str]) -> None:
        if fact_id in visited or fact_id not in facts_by_id:
            return
        visited.add(fact_id)
        fact = facts_by_id[fact_id]
        parents = fact.get("derived_from") or []
        if parents:
            for parent_id in parents:
                collect_source(str(parent_id), visited)
        elif fact.get("source_id"):
            source_ids.add(str(fact["source_id"]))

    for fact_id in used_ids:
        collect_source(fact_id, set())
    source_count = len(source_ids)
    missing_evidence = used_ids - available_ids
    rejected_count = sum(
        count for result in results
        if isinstance(result.get("details"), dict)
        for count in [result["details"].get("rejected_reference_count")]
        if isinstance(count, int) and count > 0
    )
    missing_count = max(len(missing_evidence), rejected_count)
    cross_status = _status_code(
        advice.get("cross_validation", {}).get("status"), uppercase=True
    ) or "REVIEW"
    if not results or not used:
        cross_status = "REVIEW"
    compliance_status = _status_code(
        advice.get("compliance", {}).get("status"), uppercase=True
    ) or "REVIEW"
    status_label = {"PASS": "已通过", "REVIEW": "待复核", "BLOCK": "已拦截"}
    cross_issues = {
        item.get("code") for item in advice.get("cross_validation", {}).get("issues", [])
        if isinstance(item, dict)
    }
    cross_note = (
        "部分引用未通过核验" if "EVIDENCE_REFERENCE_REJECTED" in cross_issues
        else "同项资料存在不一致" if cross_issues & {"INTERNAL_VALUE_CONFLICT", "SOURCE_VALUE_CONFLICT"}
        else "发现数据或观点分歧" if cross_status == "REVIEW"
        else "单一来源内核验通过" if cross_status == "PASS" and source_count == 1
        else "已核对资料与观点" if cross_status == "PASS"
        else "检查资料与观点是否一致"
    )
    return [
        {
            "number": "01",
            "label": "数据基础",
            "value": f"{len(used)} 条引用",
            "note": " · ".join(filter(None, [
                f"{source_count} 个来源" if used else "",
                f"{missing_count} 条引用待补齐" if missing_count else "",
            ])) or "暂无可引用资料",
            "tone": "ok" if used and source_count and not missing_count else (
                "review" if used or missing_count else "muted"
            ),
        },
        {
            "number": "02",
            "label": "分项研判",
            "value": f"{reviewed}/{len(results)} 已研判" if results else "未形成分项",
            "note": " · ".join(review_notes) if results else "等待问题与资料",
            "tone": "ok" if results and reviewed == len(results) and not limited and not failed else (
                "review" if results else "muted"
            ),
        },
        {
            "number": "03",
            "label": "交叉核验",
            "value": status_label.get(str(cross_status), "待确认"),
            "note": cross_note,
            "tone": "ok" if cross_status == "PASS" else (
                "block" if cross_status == "BLOCK" else "review"
            ),
        },
        {
            "number": "04",
            "label": "风险结论",
            "value": (
                "规则未触发" if compliance_status == "PASS"
                else status_label.get(str(compliance_status), "待确认")
            ),
            "note": "查看下方具体风险判断",
            "tone": "ok" if compliance_status == "PASS" else (
                "block" if compliance_status == "BLOCK" else "review"
            ),
        },
    ]


def source_trace_rows(advice: dict[str, Any]) -> list[dict[str, Any]]:
    """按事实引用关系生成业务化溯源表，不向界面暴露内部 fact_id。"""

    facts = advice_facts(advice)
    used_ids = {str(item) for item in advice.get("evidence", [])}
    supported_by: dict[str, list[str]] = {}
    for result in advice.get("agent_results", []):
        topic = TOPIC_LABELS.get(result.get("agent_id"), "相关分析")
        for fact_id in result.get("facts_used", []):
            labels = supported_by.setdefault(str(fact_id), [])
            if topic not in labels:
                labels.append(topic)
    rows = []
    for fact in facts:
        fact_id = str(fact.get("fact_id", ""))
        if fact_id not in used_ids:
            continue
        row = friendly_fact_rows([fact])[0]
        row = {"支持分析": "、".join(supported_by.get(fact_id, [])) or "综合结论", **row}
        row["原文"] = fact.get("source_url") or None
        rows.append(row)
    return rows


def _facts_used_by_result(advice: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    # 展开页只展示最终建议包认可的 evidence，避免历史/异常数据把已剔除事实重新挂回观点。
    verified = {str(item) for item in advice.get("evidence", [])}
    wanted = {str(item) for item in result.get("facts_used", [])} & verified
    return [fact for fact in advice_facts(advice) if str(fact.get("fact_id", "")) in wanted]


def render_chain_overview(advice: dict[str, Any]) -> None:
    cards = "".join(
        "<div class='logic-stage " + escape(stage["tone"]) + "'>"
        f"<div class='logic-step'>{escape(stage['number'])}</div>"
        f"<div class='logic-label'>{escape(stage['label'])}</div>"
        f"<div class='logic-value'>{escape(stage['value'])}</div>"
        f"<div class='logic-note'>{escape(stage['note'])}</div></div>"
        for stage in analysis_chain_stages(advice)
    )
    st.html(f"<section class='logic-chain'>{cards}</section>")


def render_conclusion_panel(advice: dict[str, Any], *, key: str = "analysis-conclusion") -> None:
    """展示后端已审核的结论，不在前端重新计算建议。"""

    with st.container(key=key, border=True):
        st.html("<div class='analysis-kicker'>研究结论 · 风险与依据</div>")
        _render_conclusion_content(advice)


def _render_conclusion_content(advice: dict[str, Any]) -> None:
    compliance = advice.get("compliance", {})
    facts = advice_facts(advice)
    used_ids = set(advice.get("evidence", []))
    used = [fact for fact in facts if fact.get("fact_id") in used_ids]
    with st.container(horizontal=True, gap="xsmall"):
        if compliance.get("status") == "PASS":
            st.badge("风险检查已通过", icon=":material/verified:", color="green")
        elif compliance.get("status") == "REVIEW":
            st.badge("需要人工复核", icon=":material/fact_check:", color="orange")
        else:
            st.badge("未形成结论", icon=":material/do_not_disturb:", color="gray")
        if advice.get("profile_version"):
            st.badge(f"投资偏好 第 {advice['profile_version']} 版", icon=":material/person_check:", color="blue")
        st.badge(f"引用 {len(used)} 条资料", icon=":material/rule:", color="primary" if used else "gray")
        if advice.get("agent_results"):
            st.badge(f"{len(advice['agent_results'])} 个分析维度", icon=":material/hub:", color="blue")
    st.markdown("**分析结论**")
    conclusion = plain_language(advice.get("conclusion")) or "现有资料不足，暂时无法作出判断。"
    st.write(conclusion)
    if advice.get("risk_conclusion"):
        st.markdown("**风险结论**")
        st.write(plain_language(advice["risk_conclusion"]))
    if compliance.get("status") != "PASS" and compliance.get("reason"):
        compliance_note = plain_language(compliance["reason"])
        if compliance_note and compliance_note != conclusion:
            st.caption(compliance_note)
    issues = [item.get("message", "") for item in advice.get("cross_validation", {}).get("issues", [])]
    render_points("需要注意", [*advice.get("risks", []), *issues], visible=99)
    render_points("接下来可以做", advice.get("next_steps", []))
    if advice.get("user_fit"):
        with st.expander("与您的投资偏好是否匹配", icon=":material/person_check:"):
            st.write(plain_language(advice["user_fit"]))
    if compliance.get("risk_notice"):
        st.caption(plain_language(compliance["risk_notice"]))
    for disclosure in compliance.get("required_disclosures", []):
        st.caption(plain_language(disclosure))


def render_logic_chain(advice: dict[str, Any]) -> None:
    """逐级展示“事实 -> 分项观点 -> 核验 -> 合规”的既有执行结果。"""

    st.markdown("**投资逻辑链**")
    st.caption("依次展开每个分析维度，可查看观点、适用条件及其实际引用的数据。")
    render_chain_overview(advice)
    results = advice.get("agent_results", [])
    if not results:
        clarification = advice.get("task_plan", {}).get("clarification_question")
        if clarification:
            st.info(plain_language(clarification))
        else:
            st.caption("本次没有形成可展开的分项分析。")
        return
    for index, result in enumerate(results, start=1):
        topic = TOPIC_LABELS.get(result.get("agent_id"), "相关分析")
        result_status = _status_code(result.get("status"))
        status = PROGRESS_LABELS.get(result_status, "待确认")
        linked = _facts_used_by_result(advice, result)
        label = f"{index:02d} · {topic} · {status} · {len(linked)} 条依据"
        with st.expander(label, icon=":material/account_tree:"):
            badge_color = {
                "completed": "green", "degraded": "orange", "unknown": "orange",
                "failed": "red", "running": "blue", "pending": "gray", "skipped": "gray",
            }.get(result_status, "gray")
            with st.container(horizontal=True, gap="xsmall"):
                st.badge(status, icon=":material/task_alt:", color=badge_color)
                confidence = result.get("confidence")
                if isinstance(confidence, (int, float)) and 0 <= confidence <= 1:
                    st.badge(f"可信度 {confidence:.0%}", icon=":material/monitoring:", color="blue")
            st.write(plain_language(result.get("opinion")) or "资料不足，暂未形成结论。")
            if linked:
                st.markdown("**该观点引用的数据**")
                st.dataframe(friendly_fact_rows(linked), width="stretch", hide_index=True)
            else:
                st.caption("这一分析维度没有通过核验的资料引用，因此只能作为有限参考。")
            render_points("判断把握受哪些因素影响", result.get("confidence_reasons", []), visible=99)
            render_points("相关风险", result.get("risk_flags", []), visible=99)
            render_points("哪些变化需要重新判断", result.get("invalidation_conditions", []), visible=99)
    issues = advice.get("cross_validation", {}).get("issues", [])
    cross_validation = advice.get("cross_validation", {})
    compliance = advice.get("compliance", {})
    with st.expander("最后一步 · 交叉核验与风险检查", icon=":material/fact_check:"):
        cross_status = _status_code(cross_validation.get("status"), uppercase=True) or "REVIEW"
        compliance_status = _status_code(compliance.get("status"), uppercase=True) or "REVIEW"
        status_text = {"PASS": "已通过", "REVIEW": "待复核", "BLOCK": "已拦截"}
        status_color = {"PASS": "green", "REVIEW": "orange", "BLOCK": "red"}
        with st.container(horizontal=True, gap="xsmall"):
            st.badge(
                f"观点核验：{status_text.get(cross_status, '待确认')}",
                icon=":material/compare_arrows:", color=status_color.get(cross_status, "gray"),
            )
            st.badge(
                f"风险检查：{status_text.get(compliance_status, '待确认')}",
                icon=":material/shield:", color=status_color.get(compliance_status, "gray"),
            )
        st.caption("核验范围：本次可用资料的时效、引用、同项数值与分析观点；单一来源结果不代表独立来源验证。")
        if issues:
            render_points("发现的分歧或待核实问题", [item.get("message", "") for item in issues], visible=99)
        elif cross_status == "PASS":
            st.write("未发现需要单独提示的跨维度冲突。")
        else:
            st.caption("交叉核验尚未通过，请结合上方状态与风险检查结果复核。")
        supporting = [
            TOPIC_LABELS.get(item, "相关分析")
            for item in cross_validation.get("supporting_agents", [])
        ]
        dissenting = [
            TOPIC_LABELS.get(item, "相关分析")
            for item in cross_validation.get("dissenting_agents", [])
        ]
        if supporting:
            st.caption(f"一致支持的分析维度：{'、'.join(dict.fromkeys(supporting))}")
        if dissenting:
            st.warning(f"需要重点复核的分析维度：{'、'.join(dict.fromkeys(dissenting))}")
        if compliance.get("reason"):
            st.caption(plain_language(compliance["reason"]))


def render_source_trace(advice: dict[str, Any], *, collapsed: bool = True) -> None:
    rows = source_trace_rows(advice)

    def body() -> None:
        if rows:
            st.caption("每条资料都标明它支持的分析维度、数据时间与来源；有原文地址时可直接打开。")
            st.dataframe(
                rows, width="stretch", hide_index=True,
                column_config={"原文": st.column_config.LinkColumn("原文", display_text="查看原文")},
            )
        else:
            render_empty_state("没有可溯源的结论依据", "当前结论没有通过核验的资料引用，请先补充数据。",
                               ":material/rule:")

    if collapsed:
        with st.expander(f"数据溯源（{len(rows)} 条）", icon=":material/database:"):
            body()
    else:
        body()


def render_profile_summary(profile: dict[str, Any]) -> None:
    labels = {"risk_level": "投资风格", "horizon_months": "计划投资多久", "max_drawdown": "最多接受亏损",
              "liquidity_need": "随时用钱的需要", "target": "投资目标", "expected_annual_return": "期望每年收益"}
    st.caption(f"当前投资偏好版本：第 {int(profile.get('version') or 1)} 版")
    cards = "".join(
        f"<div class='profile-card'><div class='profile-card-label'>{label}</div>"
        f"<div class='profile-card-value'>{escape(display_value(key, profile.get(key)))}</div></div>"
        for key, label in labels.items()
    )
    st.html(f"<div class='profile-grid'>{cards}</div>")



def plain_language(value: Any) -> str:
    """将服务端文案转换成用户语言，并拦截内部诊断信息。"""
    text = str(value or "").strip()
    # 模型复核说明会保留给后台审计，但不能原样进入浏览器。它可能同时包含
    # 状态枚举、节点字段、事实编号和合规规则代码；逐词替换仍会留下难以理解的
    # 调试式长段落，因此在展示边界统一收口为简短、可行动的业务提示。
    internal_markers = re.compile(
        # Python 的 \b 会把中文也视为“单词字符”，无法识别“标记为degraded且”；
        # 这里用 ASCII 标识符边界，覆盖中英文紧邻的真实模型输出。
        r"(?:(?<![A-Za-z0-9_])(?:degraded|completed|unknown|failed|skipped|pending|running)(?![A-Za-z0-9_])"
        r"|(?<![A-Za-z0-9_])(?:nodes_without_opinion|conflicting_agents|supporting_agents|dissenting_agents"
        r"|agent_results|facts_used|fact_id|source_id|trace_id)(?![A-Za-z0-9_])"
        r"|(?<![A-Za-z0-9_])(?:UNSUPPORTED_CLAIM|PRIVACY_SECRET|RETURN_GUARANTEE|UNVERIFIED_RUMOR"
        r"|SUITABILITY_R1_HIGH_RISK)(?![A-Za-z0-9_])"
        r"|(?<![A-Za-z0-9_-])IW-[A-Za-z0-9-]{8,}(?![A-Za-z0-9_-])"
        r"|(?<![A-Za-z0-9_])(?:market|macro|industry|security|stock|fund|portfolio|fact_verifier|compliance)节点)",
        re.IGNORECASE,
    )
    if internal_markers.search(text):
        if re.search(r"UNSUPPORTED_CLAIM|证据支持不充分|跨时段|时点|口径", text, re.IGNORECASE):
            return "部分表述与现有资料或数据时点不完全一致，暂不能作为投资判断依据。请使用同一时点的最新资料重新核对。"
        return "现有资料有限，部分分析暂未形成可靠结论。请补充最新且可核验的数据后再作判断。"
    text = re.sub(r"协调器置信加权共识分为\s*[\d.]+。?", "", text)
    for code, label in {"market": "市场", "macro": "市场", "industry": "行业", "security": "个股", "stock": "个股",
                        "fund": "基金", "portfolio": "持仓", "fact_verifier": "数据核对", "compliance": "风险检查"}.items():
        text = re.sub(rf"\b{code}[：:]", f"{label}：", text)
    replacements = {
        "授权事实不足，暂不形成强结论。": "现有资料不足，暂时无法作出可靠判断。",
        "补齐各专业智能体列出的缺失字段": "补充所分析的股票、基金名称及相关资料，再重新分析",
        "补充有来源、含时间戳的事实后重新核验": "补充注明来源和日期的最新资料，再重新分析",
        "在执行任何调整前复核风险标记和证伪条件": "调整持仓前，先确认风险以及哪些变化会让结论不再适用",
        "关注证据时点与证伪条件，定期复核": "关注最新信息；情况变化时重新分析",
        "已确认画像": "投资偏好", "画像适配": "是否适合您", "画像": "投资偏好",
        "最大回撤": "最多可接受的阶段性亏损", "流动性需求": "随时用钱的需要",
        "基本面与技术面": "公司经营情况与短期价格走势", "证伪条件": "结论不再适用的情况",
        "授权事实": "已有资料", "事实不足": "资料不足", "证据不足": "资料不足",
        "安全降级": "仅作有限参考", "快照时点": "数据日期", "快照": "数据",
        "事实核验": "数据核对", "合规闸门": "风险检查", "适当性审核": "风险匹配检查",
    }
    replacements.update({
        "专业智能体评分分散度较高，协调器保留分歧并要求人工复核。": "不同分析的看法差异较大，仍需进一步确认。",
        "语义复核不可用或不确定，需要人工复核。": "部分判断尚未确认，请进一步核实后再作决定。",
        "组合已完成规则型集中度诊断；调整应分批执行并在新快照下复核。": "已检查持仓是否过于集中。如需调整，请分步进行，并根据最新持仓重新分析。",
        "单标的集中度超限": "某只股票或基金的占比超过了您设定的上限",
        "单标的上限": "单只股票或基金的比例上限",
        "可重算综合评分已生成；正反催化剂需随快照复核。": "已完成初步分析，利好和不利因素仍需结合最新资料确认。",
        "的可用研究维度已按授权快照汇总。": "的已有研究资料已整理，仍需关注最新变化。",
        "基于五个已授权宏观维度": "根据现有的经济、资金和政策资料",
        "证据不足，仅可展示教育性说明。": "资料不足，以下内容只帮助理解相关知识。",
    })
    for original, friendly in sorted(replacements.items(), key=lambda item: -len(item[0])):
        text = text.replace(original, friendly)
    for field, label in FIELD_LABELS.items():
        text = re.sub(rf"\b{re.escape(field)}\b", label, text)
    text = re.sub(r"\b(?:market|industry|security|fund|portfolio) 声称完成但没有通过核验的事实引用。", "部分分析缺少可核对的资料，暂时不能据此作出判断。", text)
    for code, name in zip(RISK_LEVELS, ["保守型", "稳健型", "平衡型", "成长型", "进取型"]):
        text = re.sub(rf"\b{code}\b", name, text)
    return text


def profile_evidence_language(value: Any) -> str:
    """把画像评估的审计文案转换为用户可直接理解的说明。"""

    text = str(value or "").strip()
    field_labels = {
        **PROFILE_LABELS,
        "investment_history": "投资经历",
        "holding_history": "持仓经历",
        "behavioral_notes": "投资行为说明",
        "constraints": "投资限制",
    }
    extraction = re.fullmatch(
        r"从“(?P<source>.+)”提取\s+(?P<field>[A-Za-z_][A-Za-z0-9_]*)[：:]\s*(?P<raw>.*)",
        text,
    )
    if extraction:
        source = extraction.group("source")
        field = extraction.group("field")
        raw = extraction.group("raw").strip()
        if field == "horizon_months":
            try:
                months = int(float(raw))
                duration = f"{months // 12:g} 年" if months % 12 == 0 else f"{months} 个月"
            except (TypeError, ValueError):
                duration = raw
            detail = f"计划投资约 {duration}"
        elif field == "max_drawdown":
            detail = f"最多可接受约 {display_value(field, raw)} 的阶段性亏损"
        elif field == "liquidity_need":
            detail = {
                "高": "这笔资金可能需要随时使用",
                "中": "这笔资金需要保留一定的灵活性",
                "低": "这笔资金可以较长期使用",
            }.get(raw, f"资金使用需求为{raw}")
        elif field == "target":
            detail = f"投资目标是{raw}"
        elif field == "expected_annual_return":
            detail = f"期望的年收益约为 {display_value(field, raw)}（不代表保证收益）"
        elif field == "investment_experience_years":
            detail = f"投资经验约为 {display_value(field, raw)}"
        elif field in {"constraints", "investment_history", "behavioral_notes"}:
            detail = f"已记录{field_labels[field]}：{raw}"
        else:
            detail = f"已记录{field_labels.get(field, '相关信息')}：{display_value(field, raw)}"
        return f"您提到“{source}”，因此评估为：{detail}。"

    explicit = re.fullmatch(r"已记录用户明确提交的\s+(?P<field>[A-Za-z_][A-Za-z0-9_]*)", text)
    if explicit:
        label = field_labels.get(explicit.group("field"), "相关信息")
        return f"已记录您填写的{label}。"

    if text == "已按五项问卷加权公式计算风险分":
        return "根据您对五项风险问题的回答，已综合评估您的风险承受能力。"
    if text == "问卷维度不完整，未计算风险等级":
        return "风险问题尚未全部回答，因此暂时无法评估您的风险承受能力。"
    if text.startswith("以下线索缺少可核对的原文，未采用："):
        fields = text.partition("：")[2].split("、")
        labels = [field_labels.get(field.strip(), "相关信息") for field in fields if field.strip()]
        return f"以下信息在您的描述中缺少明确依据，本次暂未采用：{'、'.join(labels)}。"
    if text.startswith("语义提取不可用或不确定"):
        return "暂时无法准确理解您的文字描述，请补充上方的具体选项，或稍后重试。"
    if text.startswith(("未从你的描述中识别出明确的画像线索", "未从您的描述中识别出明确的画像线索")):
        return "暂时没有从您的描述中识别出明确的投资计划，请补充投资期限、可接受亏损或资金用途。"
    return plain_language(text)


def render_points(title: str, items: list[str], *, visible: int = 3) -> None:
    points = list(dict.fromkeys(plain_language(item) for item in items if item))
    if not points:
        return
    st.markdown(f"**{title}**")
    for point in points[:visible]:
        st.write(f"- {point}")
    if len(points) > visible:
        with st.expander(f"其余{title}（{len(points) - visible}项）"):
            for point in points[visible:]:
                st.write(f"- {point}")



def render_empty_state(title: str, detail: str, icon: str = ":material/inbox:") -> None:
    """带图标的空状态，替代一行裸提示。"""

    with st.container(border=True, horizontal_alignment="center"):
        st.markdown(icon)
        st.markdown(f"**{title}**")
        st.caption(detail)
