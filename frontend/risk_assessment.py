"""单框、逐题的风险测评及证券式结果展示。"""
from __future__ import annotations

from html import escape
from typing import Callable

import streamlit as st

from backend.app.risk_questionnaire import (
    QUESTIONNAIRE_VERSION, QUESTIONS, RISK_NAMES, SCORING_NOTICE,
    answer_issues, assessment_is_current,
)


def _remember_answer(question_id: str) -> None:
    value = st.session_state.get(f"risk_answer_{question_id}")
    if value is not None:
        st.session_state.risk_assessment_answers[question_id] = value


def _move_question(offset: int) -> None:
    st.session_state.risk_assessment_index = max(0, min(len(QUESTIONS) - 1, st.session_state.risk_assessment_index + offset))


def _start_assessment() -> None:
    for key in list(st.session_state):
        if key.startswith("risk_answer_"):
            st.session_state.pop(key, None)
    st.session_state.risk_assessment_answers = dict(st.session_state.profile.get("risk_answers") or {})
    st.session_state.risk_assessment_index = 0
    st.session_state.risk_assessment_editing = True
    st.session_state.pop("profile_draft", None)


@st.dialog("投资者分类标准及适当性匹配规则")
def _matching_rules() -> None:
    st.table({
        "风险承受能力等级": [f"C{i}（{name}）" for i, name in enumerate(RISK_NAMES, 1)],
        "匹配的产品或服务等级": ["、".join(f"R{j}" for j in range(i, 0, -1)) for i in range(1, 6)],
    })
    st.caption("投资品种和投资期限还应分别匹配，风险等级匹配不代表保证收益。")
    if st.button("关闭", width="stretch"):
        st.rerun()


def result_html(profile: dict) -> str:
    band = int((profile.get("investor_category") or "C1")[1])
    steps = "<span class='risk-step-line'></span>".join(
        f"<span class='risk-step{' active' if i == band else ''}'>{i}</span>" for i in range(1, 6)
    )
    product_levels = "R1" if band == 1 else f"R1–R{band}"
    return f"""<section class="risk-result-card">
      <div class="risk-result-caption">您当前风险等级为</div>
      <h2>{escape(profile.get('risk_description') or RISK_NAMES[band - 1])}</h2>
      <div class="risk-result-category">{escape(profile.get('investor_category') or '')}</div>
      <div class="risk-result-steps">{steps}</div>
      <div class="risk-result-validity">有效期至 {escape(str(profile.get('valid_until') or '待确认'))}</div>
      <dl><div><dt>适合投资风险等级为：</dt><dd>{product_levels}的产品或服务</dd></div>
      <div><dt>拟投资的品种为：</dt><dd>{escape('，'.join(profile.get('preferred_product_types') or []))}</dd></div>
      <div><dt>拟投资的期限为：</dt><dd>{escape(profile.get('investment_horizon_label') or '待填写')}</dd></div></dl>
    </section>"""


def render_risk_assessment(api_base: str, *, api_request: Callable) -> None:
    current = st.session_state.profile
    is_new = current.get("questionnaire_version") == QUESTIONNAIRE_VERSION
    first_missing = next((i for i, q in enumerate(QUESTIONS) if q.id not in (current.get("risk_answers") or {})), 0)
    st.session_state.setdefault("risk_assessment_index", first_missing)
    st.session_state.setdefault("risk_assessment_answers", dict(current.get("risk_answers") or {}))
    st.session_state.setdefault("risk_assessment_editing", not is_new)
    if not is_new and current.get("risk_answers") and not st.session_state.risk_assessment_editing:
        st.session_state.risk_assessment_editing = True
        st.session_state.risk_assessment_index = first_missing

    with st.container(border=True, key="risk-assessment-box"):
        st.html('<div class="risk-box-heading">风险评估</div>')
        if st.session_state.risk_assessment_editing:
            if not is_new and current.get("risk_answers"):
                st.caption("问卷已增加财务规划补充题，原有答案已保留，请补答后核对并保存。")
            index = max(0, min(len(QUESTIONS) - 1, st.session_state.risk_assessment_index))
            question = QUESTIONS[index]
            answers = st.session_state.risk_assessment_answers
            st.progress((index + 1) / len(QUESTIONS))
            st.html(f'<div class="risk-question-counter">{index + 1}/{len(QUESTIONS)}</div>')
            with st.container(key="risk-question-content"):
                st.markdown(f"### {index + 1}. {question.title}")
                letters = list(question.labels)
                previous = answers.get(question.id)
                selected = st.radio(
                    f"第{index + 1}题答案", letters,
                    index=letters.index(previous) if previous in letters else None,
                    format_func=question.labels.__getitem__, label_visibility="collapsed",
                    key=f"risk_answer_{question.id}", on_change=_remember_answer, args=(question.id,),
                )
                if selected is not None:
                    answers[question.id] = selected
            issues = answer_issues(answers)
            for issue in issues:
                st.warning(issue)
            missing = [q for q in QUESTIONS if q.id not in answers]
            with st.container(horizontal=True, key="risk-question-actions"):
                st.button("上一题", key="risk_previous", disabled=index == 0, width="stretch",
                          on_click=_move_question, args=(-1,))
                if index < len(QUESTIONS) - 1:
                    st.button("下一题", key="risk_next", type="primary", width="stretch",
                              disabled=selected is None, on_click=_move_question, args=(1,))
                else:
                    if missing:
                        st.caption(f"还有 {len(missing)} 题待作答")
                    if st.button("提交", key="risk_submit", type="primary", width="stretch", disabled=bool(missing or issues)):
                        result = api_request(api_base, "POST", "/profile/assess", {
                            "questionnaire_version": QUESTIONNAIRE_VERSION, "risk_answers": dict(answers),
                        })
                        if result:
                            if result.get("missing_fields"):
                                st.warning("问卷尚未完成或答案不一致，请核对后重新提交。")
                            else:
                                result["profile"]["version"] = int(current.get("version") or 1)
                                for field in ("single_security_limit", "industry_limit", "constraints", "trading_analysis"):
                                    if field in current:
                                        result["profile"][field] = current[field]
                                st.session_state.profile = result["profile"]
                                st.session_state.profile_draft = result
                                st.session_state.risk_assessment_editing = False
                                st.rerun()
            st.caption(SCORING_NOTICE)
            return

        st.html(result_html(current))
        _render_guidance(current)
        if st.button("查看投资者分类标准及适当性匹配规则 ›", key="risk_matching_rules", width="stretch"):
            _matching_rules()
        draft = st.session_state.get("profile_draft")
        if draft:
            st.caption("请核对测评结果，确认后保存到您的账号。")
            if st.button("确认并保存", key="risk_confirm", type="primary", width="stretch", disabled=bool(draft.get("missing_fields"))):
                confirmed = api_request(api_base, "POST", "/profile/confirm", {"profile": current})
                if confirmed:
                    st.session_state.profile = confirmed
                    st.session_state.pop("profile_draft", None)
                    st.rerun()
            st.button("返回修改答案", key="risk_edit_answers", width="stretch", on_click=_start_assessment)
        else:
            if not assessment_is_current(current):
                st.warning("风险测评已过期，请重新测评后再开始个性化分析。")
            st.button("重新评估", key="risk_retake", type="primary", width="stretch", on_click=_start_assessment)


def _render_guidance(profile: dict) -> None:
    """与结果一起展示解释和资金约束；详细答题依据可展开核对。"""
    if not profile.get("financial_plan"):
        return
    st.markdown("### 评级依据与用户画像")
    st.caption(f"前19题基础风险分：{profile.get('risk_score', '未提供')} / 100。本金安全或零回撤选择按保守型处理；补充题不增加风险分。")
    for reason in profile.get("assessment_reasons", []):
        st.markdown(f"**{reason['dimension']}**：{reason['summary']}")
    for warning in profile.get("financial_warnings", []):
        st.warning(warning)
    plan = profile["financial_plan"]
    st.markdown("### 财务规划与资金安排")
    st.table({
        "项目": ["计划投入资金", "未来12个月必要支出", "已有应急储备", "每月还款负担", "首要目标", "最早用款时间", "流动性需求"],
        "您的情况": [plan["investment_capital_label"], plan["near_term_spending_label"], plan["emergency_reserve_label"],
                  plan["debt_service_label"], plan["goal_label"], plan["goal_horizon_label"], profile["liquidity_need"]],
    })
    st.caption("资金金额和时间保留您选择的区间；近期必要支出按比例区间上限预留，可在明确金额后重新评估。")
    st.markdown("### 根据资金约束调整的配置参考")
    rows = profile.get("allocation_guidance", [])
    if rows:
        st.table({"资产类别": [r["asset_class"] for r in rows],
                  "参考区间": [f"{r['min_weight']:.0%}–{r['max_weight']:.0%}" for r in rows],
                  "示例占比": [f"{r['reference_weight']:.1%}" for r in rows]})
    st.caption("示例占比合计100%，用于解释资金约束；区间不能各自取上限后直接相加。产品仍需匹配风险等级、期限和持仓集中度。")
    with st.expander("查看答题依据、配置规则与情景测算"):
        breakdown = profile.get("scoring_breakdown", [])
        if breakdown:
            st.table({"评分维度": [r["dimension"] for r in breakdown],
                      "得分 / 上限": [f"{r['points']:g} / {r['maximum']:g}" for r in breakdown]})
        st.caption(profile.get("scoring_notice") or SCORING_NOTICE)
        st.caption("平台基础分档：低于20分为C1，20至不足37分为C2，37至不足54分为C3，54至不足83分为C4，83分及以上为C5。本金安全或不能接受回撤时按C1处理。")
        for reason in profile.get("assessment_reasons", []):
            st.markdown(f"**{reason['dimension']}**")
            for answer in reason["answers"]:
                st.markdown(f"- {answer}")
        for explanation in plan.get("policy_explanations", []):
            st.markdown(f"- {explanation}")
        st.caption("情景一假设权益跌20%、固收跌5%、现金及低波动类不变；情景二分别跌40%、10%、0%。按示例占比计算，不是行情预测或实际最大回撤。")
        for scenario in profile.get("stress_scenarios", []):
            suffix = "，超过您选择的回撤上限" if scenario["exceeds_tolerance"] else ""
            st.markdown(f"**{scenario['name']}**：假设组合损失 {scenario['estimated_loss']:.1%}{suffix}。")
        st.caption("低波动资产也可能亏损；情景以外的跌幅、相关性变化、赎回限制和费用可能使损失更大。")
    for followup in plan.get("followups", []):
        st.info(followup)
