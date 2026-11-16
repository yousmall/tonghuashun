"""风险评估页的个性化建议、配置金额与人工复核安排。"""
from __future__ import annotations

import json
from typing import Callable

import streamlit as st

from backend.app.risk_questionnaire import local_today
from backend.app.services.personalized_investment import build_personalized_investment
from frontend.charts import render_bar


def render_investment_result(result: dict) -> None:
    st.write(result["summary"])
    for message in result.get("missing_information", []):
        st.info(message)
    if result["status"] != "ready":
        return
    summary = result["profile_summary"]
    st.caption(f"承受能力 {summary['risk_level']} · 用款：{summary['horizon']} · 收益目标：{summary['return_goal']}")
    st.caption("可匹配产品风险等级：" + "、".join(summary["suitable_product_levels"]) +
               "；问卷拟投资品种：" + "、".join(summary["preferred_product_types"]))
    rows = result["allocation"]
    display = []
    for row in rows:
        item = {"资产类别": row["asset_class"], "参考区间": f"{row['min_weight']:.1%}–{row['max_weight']:.1%}",
                "示例占比": f"{row['reference_weight']:.1%}"}
        if "reference_amount" in row:
            item["参考金额（元）"] = f"{row['reference_amount']:,.2f}"
        display.append(item)
    st.table(display)
    render_bar([{"资产类别": row["asset_class"], "示例占比（%）": row["reference_weight"] * 100} for row in rows],
                 x="资产类别", y="示例占比（%）", horizontal=True, height=180)
    if result.get("investment_amount") is not None:
        st.caption(f"本次资金 {result['investment_amount']:,.2f} 元；金额按示例占比分配并处理分位舍入。该金额仅用于本次预览。")
    st.caption("示例占比合计100%；各类别区间的上下限不能独立相加。必要支出包含在现金及低波动类安排内。")
    for recommendation in result["recommendations"]:
        with st.container(border=True):
            st.markdown(f"**{recommendation['title']}**")
            st.write(recommendation["action"])
            st.caption("依据：" + recommendation["basis"])
    for message in result.get("warnings", []):
        st.warning(message)
    with st.expander("查看假设损失与复核安排"):
        for explanation in result["policy_explanations"]:
            st.write(explanation)
        for scenario in result["stress_scenarios"]:
            assumptions = "、".join(f"{name}{shock:+.0%}" for name, shock in scenario["assumptions"].items())
            amount = f"，对应 {scenario['loss_amount']:,.2f} 元" if scenario["loss_amount"] is not None else ""
            suffix = "，超过已选回撤边界" if scenario["exceeds_tolerance"] else ""
            st.write(f"{scenario['name']}（{assumptions}）：组合假设损失 {scenario['estimated_loss']:.1%}{amount}{suffix}。")
        st.table([{"复核时机": item["when"], "安排": item["action"]} for item in result["review_plan"]])
        st.caption(f"画像版本：{result['profile_version']} · 生成日期：{result['generated_on']} · 测评有效期至 {summary['valid_until']}")
    st.caption(result["notice"])


def render_personalized_investment(api_base: str, *, api_request: Callable) -> None:
    st.subheader("个性化投资建议与资产配置")
    profile = st.session_state.get("profile", {})
    editing = st.session_state.get("risk_assessment_editing", False)
    signature = json.dumps({"profile": profile, "editing": editing, "date": local_today().isoformat()},
                           ensure_ascii=False, sort_keys=True, default=str)
    if st.session_state.get("automatic_plan_investment_signature") != signature:
        st.session_state.pop("automatic_plan_investment_result", None)
        st.session_state.pop("automatic_plan_investment_amount", None)
        st.session_state.automatic_plan_investment_signature = signature
    if editing:
        st.info("请先提交并确认保存当前问卷，再生成个性化方案。")
        return
    baseline = build_personalized_investment(profile)
    if baseline["status"] != "ready":
        render_investment_result(baseline)
        return
    st.caption("方案使用已确认问卷；上方文字规划预览不会改写本方案。可补充当前投资金额查看资金安排。")
    with st.form("automatic_plan_investment_form"):
        amount = st.number_input("本次投资金额（元，填0仅查看比例）", min_value=0.0, max_value=1_000_000_000_000.0,
            value=0.0, step=1000.0, format="%.2f", key="automatic_plan_investment_amount")
        submitted = st.form_submit_button("生成个性化方案", type="primary")
    if submitted:
        st.session_state.pop("automatic_plan_investment_result", None)
        payload = {"investment_amount": amount} if amount else {}
        result = api_request(api_base, "POST", "/profile/investment-plan", payload)
        if not result:
            st.session_state.automatic_plan_investment_result = {
                "status": "unavailable", "summary": "本次方案尚未生成成功，请重新生成。"}
            st.error("本次方案生成失败，请重新生成。")
            return
        if result.get("profile_version") != profile.get("version", 1):
            st.session_state.automatic_plan_investment_result = {
                "status": "needs_assessment", "summary": "账号画像已更新，请刷新风险评估后重新生成。"}
            st.warning("账号画像已在其他页面更新，请刷新风险评估后重新生成。")
            return
        st.session_state.automatic_plan_investment_result = result
    render_investment_result(st.session_state.get("automatic_plan_investment_result") or baseline)
