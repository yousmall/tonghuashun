"""自动规划的原文输入与三项核心结果。"""
from __future__ import annotations

import json
from typing import Callable

import streamlit as st

from backend.app.risk_questionnaire import QUESTIONNAIRE_VERSION
from backend.app.services.automatic_planning import build_automatic_planning


def render_planning_result(result: dict) -> None:
    risk = result["risk_preference"]
    returns = result["return_expectation"]
    horizon = result["horizon_plan"]
    with st.container(horizontal=True):
        with st.container(border=True):
            st.subheader("风险偏好识别")
            st.write(risk["label"])
            if risk.get("capacity_level"):
                status = "已确认且有效" if risk["confirmed_assessment"] else "待确认或需重新测评"
                st.caption(f"问卷承受能力：{risk['capacity_level']} · {status}")
            else:
                st.caption("风险承受能力需由完整问卷评估。")
            if risk.get("drawdown_limit") is not None:
                st.caption(f"自述可接受回撤上限：{risk['drawdown_limit']:.1%}")
        with st.container(border=True):
            st.subheader("收益预期分析")
            st.write(returns["label"])
            st.caption(returns["notice"])
            if returns.get("required_annual_for_goal") is not None:
                st.caption(f"目标金额对应的年化需求：{returns['required_annual_for_goal']:.2%}（本金一次投入，无追加投入）")
        with st.container(border=True):
            st.subheader("投资期限规划")
            st.write(horizon["label"])
            if horizon.get("exact_months"):
                st.caption(f"明确期限：{horizon['exact_months']}个月")
            else:
                st.caption("保留期限区间；明确用款时间后可生成具体日期。")
    for message in result.get("missing_information", []):
        st.info(message)
    scenario = returns.get("scenario")
    if scenario:
        st.write(f"按目标进行算术演示：本金 {scenario['principal']:,.2f} 元，"
                 f"{scenario['months']}个月后为 {scenario['ending_amount']:,.2f} 元，"
                 f"差额 {scenario['profit']:,.2f} 元。")
        st.caption(scenario["basis"])


def render_automatic_planning(api_base: str, *, api_request: Callable) -> None:
    profile = st.session_state.get("profile", {})
    editing = st.session_state.get("risk_assessment_editing", False)
    answers = dict(st.session_state.get("risk_assessment_answers", {})) if editing else profile.get("risk_answers")
    signature = json.dumps({"profile":profile, "answers":answers, "editing":editing}, ensure_ascii=False, sort_keys=True, default=str)
    if st.session_state.get("automatic_plan_signature") != signature:
        st.session_state.pop("automatic_plan_preview", None)
        st.session_state.automatic_plan_signature = signature
    st.subheader("自动投资规划")
    st.caption("问卷完成后自动生成三项规划；也可以补充一段话，预览自己的具体目标。")
    with st.form("automatic_plan_form"):
        narrative = st.text_area("描述您的投资计划", key="automatic_plan_narrative", max_chars=2000,
            placeholder="例如：我偏好稳健，可投资资金20万元，计划投资3年，2年后买房，最多可接受回撤8%，期望年化收益8%。")
        submitted = st.form_submit_button("自动识别并规划", type="primary")
    if submitted:
        st.session_state.pop("automatic_plan_preview", None)
        payload = {"narrative":narrative}
        # 未确认草稿或编辑中的答案必须交由服务端重算，不能借用账号旧评级。
        if editing or profile.get("risk_answers") and not profile.get("confirmed"):
            payload.update(questionnaire_version=QUESTIONNAIRE_VERSION, risk_answers=answers or {})
        result = api_request(api_base, "POST", "/profile/auto-plan", payload)
        if result:
            st.session_state.automatic_plan_preview = result
    result = st.session_state.get("automatic_plan_preview")
    if not result and not editing and profile.get("risk_level"):
        # 兼容旧账号并刷新有效期提示；纯计算，不发起网络或写账号。
        result = build_automatic_planning(profile)
    if result:
        render_planning_result(result)
