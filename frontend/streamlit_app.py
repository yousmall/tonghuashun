"""问策智投 Streamlit 前端。

本页面刻意不承担投资判断、数据抓取或合规决策：所有业务判断都通过 FastAPI
提交给后端。前端负责收集用户画像和可选事实快照，并把后端自动取数后返回的
``AdvicePackage`` 以可追溯、可解释的方式展示出来。

启动方式（先启动 backend，再在另一个终端执行）：

    streamlit run frontend/streamlit_app.py
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import httpx
import streamlit as st


DEFAULT_API_BASE = os.getenv("WENCE_API_BASE", "http://127.0.0.1:8000/api/v1")
RISK_LEVELS = ["R1", "R2", "R3", "R4", "R5"]
STATUS_LABELS = {
    "completed": "已完成",
    "running": "执行中",
    "pending": "等待中",
    "degraded": "已降级",
    "unknown": "信息不足",
    "failed": "失败",
    "skipped": "已跳过",
}


def utc_now() -> str:
    """生成 API 要求的带时区 ISO-8601 时间，避免浏览器本地时区造成核验歧义。"""
    return datetime.now(timezone.utc).isoformat()


def demo_facts() -> list[dict[str, Any]]:
    """返回可运行演示的完整事实快照。

    演示事实明确标记为 ``DEMO_SNAPSHOT``，让界面不会把固定示例伪装为实时行情。
    每次加载都使用新 snapshot_time，只是为了便于本地展示事实时效闭环。
    """
    timestamp = utc_now()
    rows = [
        # 宏观市场五维：MacroAgent 仅在五项均存在时输出完整评分。
        ("宏观快照", "growth_score", 58),
        ("宏观快照", "inflation_score", 52),
        ("宏观快照", "liquidity_score", 55),
        ("宏观快照", "policy_score", 50),
        ("宏观快照", "risk_appetite_score", 48),
        # 行业五维：同一 entity 才可构成可重算行业评分。
        ("新能源", "prosperity_score", 62),
        ("新能源", "valuation_score", 48),
        ("新能源", "capital_flow_score", 56),
        ("新能源", "policy_score", 60),
        ("新能源", "crowding_score", 42),
        # 个股研究：允许展示基本面和技术面的分歧，而不强制“统一答案”。
        ("示例科技", "fundamental_score", 72),
        ("示例科技", "valuation_score", 55),
        ("示例科技", "technical_score", 44),
        # 基金准入与候选质量。
        ("示例ETF", "fund_risk_level", 3),
        ("示例ETF", "fund_score", 66),
        ("示例ETF", "fee_rate", 0.005),
        # 组合诊断至少需要持仓权重事实。
        ("示例ETF", "weight", 0.15),
    ]
    return [
        {
            "fact_id": f"DEMO-{index:03d}",
            "entity": entity,
            "field": field,
            "value": value,
            "snapshot_time": timestamp,
            "source_id": "DEMO_SNAPSHOT",
            "quality": 0.80,
        }
        for index, (entity, field, value) in enumerate(rows, start=1)
    ]


def init_session() -> None:
    """初始化会话状态；刷新浏览器会丢失数据，这是本地演示版的刻意边界。"""
    st.session_state.setdefault(
        "profile",
        {
            "user_id": "demo-user",
            "risk_level": "R3",
            "risk_score": None,
            "horizon_months": 24,
            "max_drawdown": 0.10,
            "liquidity_need": "中",
            "constraints": [],
            "target": None,
            "single_security_limit": 0.20,
            "industry_limit": 0.30,
            "version": 1,
            "confirmed": False,
        },
    )
    st.session_state.setdefault("facts", [])
    st.session_state.setdefault("portfolio", [])
    st.session_state.setdefault("advice", None)
    st.session_state.setdefault("last_error", None)
    st.session_state.setdefault("conversation", [])
    st.session_state.setdefault("questionnaire", {})


def api_request(api_base: str, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """请求后端并将网络/服务异常转成可理解的前端提示。

    不把异常详情显示给普通用户，防止内部 URL、调用栈或代理配置泄漏；开发者
    仍可在 Streamlit 终端看到完整异常。请求失败后返回 ``None``，调用方不要把
    它错误当作一个空的业务响应。
    """
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.request(method, f"{api_base}{path}", json=payload)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.json().get("detail", "请求格式或服务状态错误")
        st.error(f"接口返回 {exc.response.status_code}：{detail}")
    except httpx.HTTPError:
        st.error("无法连接后端。请确认 FastAPI 已在 http://127.0.0.1:8000 启动。")
    return None


def show_status(status: str) -> None:
    """以一致颜色展示后端的三态合规结果，避免把 REVIEW 误读成可执行建议。"""
    if status == "PASS":
        st.success("PASS：已通过当前事实与合规闸门。")
    elif status == "REVIEW":
        st.warning("REVIEW：仅可作教育性说明或人工复核，不能视为投资建议。")
    else:
        st.error("BLOCK：请求已被合规/适当性规则拦截，未生成投资建议。")


def profile_ready() -> bool:
    """个性化分析只接受经确认画像；前端在调用前再次提示，但后端仍是最终闸门。"""
    return bool(st.session_state.profile.get("confirmed"))


def add_fact(fact: dict[str, Any]) -> None:
    """按 fact_id 替换同名快照，防止证据中心展示同一事实的重复版本。"""
    facts = [row for row in st.session_state.facts if row["fact_id"] != fact["fact_id"]]
    facts.append(fact)
    st.session_state.facts = facts


def render_advice(advice: dict[str, Any]) -> None:
    """渲染 AdvicePackage 的共同部分，所有页面使用同一展示规则。"""
    compliance = advice["compliance"]
    show_status(compliance["status"])
    left, middle, right, consensus = st.columns(4)
    left.metric("综合置信度", f"{advice['confidence']:.0%}")
    middle.metric("引用事实", len(advice.get("evidence", [])))
    right.metric("专业结果", len(advice.get("agent_results", [])))
    cross_validation = advice.get("cross_validation", {})
    consensus_value = cross_validation.get("consensus_score")
    consensus.metric("协调器共识", "无" if consensus_value is None else f"{consensus_value:.1f}")
    st.caption(f"Trace ID：{advice['trace_id']}  |  快照时点：{advice.get('snapshot_time') or '无可采信快照'}")
    acquisition = advice.get("data_acquisition", {})
    mode = acquisition.get("mode")
    acquisition_summary = (
        f"自动取数：{mode or '未知'} · 实取 {acquisition.get('fetched_fact_count', 0)} 条"
        f" · 派生 {acquisition.get('derived_fact_count', 0)} 条"
    )
    if mode in {"live", "mixed"}:
        st.success(acquisition_summary)
    elif mode == "unavailable":
        st.warning(f"{acquisition_summary}。{acquisition.get('message') or '当前仅按已有事实安全降级。'}")
    else:
        st.caption(f"{acquisition_summary}。{acquisition.get('message') or ''}")
    if acquisition.get("failed_capabilities") or acquisition.get("empty_capabilities"):
        with st.expander("自动取数明细"):
            if acquisition.get("successful_capabilities"):
                st.write(f"成功：{'、'.join(acquisition['successful_capabilities'])}")
            if acquisition.get("empty_capabilities"):
                st.write(f"无结果：{'、'.join(acquisition['empty_capabilities'])}")
            if acquisition.get("failed_capabilities"):
                st.write(f"失败并安全降级：{'、'.join(acquisition['failed_capabilities'])}")
    st.subheader("综合结论")
    st.write(advice["conclusion"])
    if advice.get("user_fit"):
        st.info(f"画像适配：{advice['user_fit']}")
    if advice.get("risks"):
        st.subheader("风险提示")
        for risk in advice["risks"]:
            st.warning(risk)
    if advice.get("next_steps"):
        st.subheader("下一步")
        for step in advice["next_steps"]:
            st.write(f"- {step}")
    if compliance.get("reason"):
        st.caption(f"审核说明：{compliance['reason']}")
    if compliance.get("risk_notice"):
        st.caption(compliance["risk_notice"])
    if cross_validation.get("issues"):
        with st.expander("跨智能体与跨来源一致性检查", expanded=True):
            for issue in cross_validation["issues"]:
                st.warning(f"{issue['code']}：{issue['message']}")


def run_analysis(api_base: str, query: str) -> dict[str, Any] | None:
    """携带最近多轮上下文调用统一分析端点，并更新会话历史。"""
    if not profile_ready():
        st.warning("请先到“画像中心”确认画像，再执行个性化分析。")
        return None
    st.session_state.conversation.append({"role": "user", "content": query, "created_at": utc_now()})
    payload = {
        "query": query,
        "profile": st.session_state.profile,
        "facts": st.session_state.facts,
        "auto_fetch": True,
        "portfolio": st.session_state.portfolio,
        "conversation_id": "streamlit-session",
        "context_messages": st.session_state.conversation[-20:],
    }
    with st.spinner("正在规划任务、核验事实并进行合规审核…"):
        advice = api_request(api_base, "POST", "/portfolio/analyze", payload)
    if advice:
        for fact in advice.get("facts", []):
            add_fact(fact)
        st.session_state.advice = advice
        st.session_state.last_error = None
        st.session_state.conversation.append(
            {"role": "assistant", "content": advice["conclusion"], "created_at": utc_now()}
        )
    return advice


def page_home(api_base: str) -> None:
    """首页/多轮对话：展示上下文历史，并由后端统一完成意图与投研编排。"""
    st.title("问策智投")
    st.caption("投资研究辅助 · 事实快照驱动 · 不自动交易 · 不承诺收益")
    st.info("使用流程：确认画像 → 提交问题 → 系统自动取数并核验 → 在协作与证据页面复核。也可手工补充事实。")
    first, second = st.columns([3, 1])
    with first:
        if st.button("加载完整演示快照", width="stretch"):
            st.session_state.facts = demo_facts()
            st.success("已载入标记为 DEMO_SNAPSHOT 的演示事实。")
    with second:
        if st.button("清空对话", width="stretch"):
            st.session_state.conversation = []
            st.session_state.advice = None

    for turn in st.session_state.conversation:
        with st.chat_message(turn["role"]):
            st.write(turn["content"])
    query = st.chat_input("例如：结合刚才的行业判断，再诊断我的持仓")
    if query:
        run_analysis(api_base, query.strip())
        st.rerun()
    if st.session_state.advice:
        st.divider()
        render_advice(st.session_state.advice)


def page_profile(api_base: str) -> None:
    """画像中心：将抽取、用户核对和确认做成不可跳过的两步流程。"""
    st.title("画像中心")
    st.caption("画像草稿不会自动生效；请核对信息后显式确认。")
    current = st.session_state.profile
    with st.form("profile_assessment"):
        user_id = st.text_input("用户标识", value=current["user_id"])
        narrative = st.text_area(
            "自然语言补充",
            placeholder="例如：我投资 3 年，2 年后买房，最多接受 8% 回撤，期望年化收益 8%。",
        )
        profile_left, profile_right = st.columns(2)
        experience_years = profile_left.number_input(
            "投资经验（年）",
            min_value=0.0,
            max_value=100.0,
            value=float(current.get("investment_experience_years") or 0),
            step=0.5,
        )
        expected_return_percent = profile_right.number_input(
            "期望年化收益（%）",
            min_value=-100.0,
            max_value=500.0,
            value=float(current.get("expected_annual_return") or 0) * 100,
            step=0.5,
        )
        investment_history_text = st.text_area(
            "投资历史（每行一项）",
            value="\n".join(current.get("investment_history", [])),
            placeholder="例如：2024 年开始定投宽基 ETF",
        )
        with st.expander("填写风险问卷（全部填写后才计算 R1-R5）"):
            questionnaire = {
                "financial_capacity": st.slider("财务承受能力", 0, 100, 50),
                "loss_tolerance": st.slider("主观损失容忍度", 0, 100, 50),
                "investment_horizon": st.slider("投资期限适配度", 0, 100, 50),
                "knowledge_experience": st.slider("知识与经验", 0, 100, 50),
                "behavior_stability": st.slider("行为稳定度", 0, 100, 50),
            }
        create_draft = st.form_submit_button("生成画像草稿", type="primary")
    if create_draft:
        st.session_state.questionnaire = questionnaire
        result = api_request(
            api_base,
            "POST",
            "/profile/assess",
            {
                "user_id": user_id,
                "narrative": narrative or None,
                "questionnaire": questionnaire,
                "investment_experience_years": experience_years,
                "investment_history": [line.strip() for line in investment_history_text.splitlines() if line.strip()],
                "holding_history": st.session_state.portfolio,
                "expected_annual_return": expected_return_percent / 100,
            },
        )
        if result:
            st.session_state.profile = result["profile"]
            st.session_state.profile["confirmed"] = False
            st.session_state.profile_draft = result

    draft = st.session_state.get("profile_draft")
    if draft:
        st.subheader("待确认画像")
        st.json(draft["profile"])
        if draft.get("evidence"):
            st.write("提取依据：")
            for item in draft["evidence"]:
                st.write(f"- {item}")
        if draft.get("missing_fields"):
            st.warning(f"仍缺少：{'、'.join(draft['missing_fields'])}")
        if st.button("我已核对，确认画像", type="primary"):
            confirmed = api_request(api_base, "POST", "/profile/confirm", {"profile": st.session_state.profile})
            if confirmed:
                st.session_state.profile = confirmed
                # 关闭草稿卡片，避免用户在刷新前重复点击导致版本无意义地连续递增。
                st.session_state.pop("profile_draft", None)
                st.success(f"画像已确认，当前版本为 v{confirmed['version']}。")

    st.divider()
    status = "已确认" if profile_ready() else "未确认"
    st.subheader(f"当前画像（{status}）")
    st.json(st.session_state.profile)
    if st.session_state.questionnaire:
        st.subheader("风险画像维度")
        st.bar_chart(st.session_state.questionnaire, horizontal=True)


def page_market(api_base: str) -> None:
    """市场驾驶舱同时承担本地快照管理职责，所有新增记录均显式展示来源和时点。"""
    st.title("市场驾驶舱与事实快照")
    st.caption("这里展示的是用户授权的本地事实包，不是实时行情终端。")
    top_left, top_right = st.columns(2)
    with top_left:
        if st.button("载入完整演示快照", width="stretch"):
            st.session_state.facts = demo_facts()
            st.success("演示事实已载入。")
    with top_right:
        if st.button("清空事实快照", width="stretch"):
            st.session_state.facts = []
            st.session_state.advice = None
            st.info("已清空；历史分析结果也已移除，避免混淆证据。")

    with st.expander("从问财 SkillHub/OpenAPI 拉取真实数据"):
        fetch_left, fetch_right = st.columns(2)
        fetch_label = fetch_left.selectbox(
            "数据类型",
            ["实时行情", "财务指标", "财经新闻", "公告", "研报", "基金/ETF", "行业排名", "可转债"],
        )
        fetch_target = fetch_right.text_input("标的或查询条件", placeholder="例如：600519 或 新能源行业近一个月")
        fetch_kind = {
            "实时行情": "quote",
            "财务指标": "financial",
            "财经新闻": "news",
            "公告": "announcement",
            "研报": "research_report",
            "基金/ETF": "fund",
            "行业排名": "industry",
            "可转债": "convertible",
        }[fetch_label]
        if st.button("拉取并加入事实包", disabled=not fetch_target.strip()):
            fetched = api_request(
                api_base,
                "POST",
                "/data/fetch",
                {"kind": fetch_kind, "target": fetch_target.strip(), "filters": {}},
            )
            if fetched:
                for fact in fetched.get("facts", []):
                    add_fact(fact)
                st.success(f"已从 {fetched['provider']} 加入 {len(fetched.get('facts', []))} 条事实。")

    with st.expander("手工录入一条 FactRecord"):
        with st.form("fact_form", clear_on_submit=True):
            first, second = st.columns(2)
            fact_id = first.text_input("事实 ID", placeholder="F-CUSTOM-001")
            entity = second.text_input("实体", placeholder="示例ETF / 新能源 / 证券代码")
            field = first.text_input("字段", placeholder="weight / fundamental_score / close_price")
            value_text = second.text_input("数值或文本值", placeholder="0.15")
            source_id = first.text_input("来源 ID", value="MANUAL_SNAPSHOT")
            quality = second.slider("证据质量", 0.0, 1.0, 0.80, 0.05)
            submit_fact = st.form_submit_button("加入事实包")
        if submit_fact:
            if not all([fact_id.strip(), entity.strip(), field.strip(), value_text.strip(), source_id.strip()]):
                st.warning("事实 ID、实体、字段、值和来源均不能为空。")
            else:
                try:
                    value: Any = float(value_text)
                except ValueError:
                    value = value_text
                add_fact(
                    {
                        "fact_id": fact_id.strip(),
                        "entity": entity.strip(),
                        "field": field.strip(),
                        "value": value,
                        "snapshot_time": utc_now(),
                        "source_id": source_id.strip(),
                        "quality": quality,
                    }
                )
                st.success("已加入事实包。")

    facts = st.session_state.facts
    st.metric("当前授权事实数", len(facts))
    if facts:
        st.dataframe(facts, width="stretch", hide_index=True)
        average_quality = sum(float(fact["quality"]) for fact in facts) / len(facts)
        st.progress(average_quality, text=f"平均证据质量：{average_quality:.0%}")
        numeric_scores = {
            f"{fact['entity']} · {fact['field']}": float(fact["value"])
            for fact in facts
            if isinstance(fact.get("value"), (int, float)) and 0 <= float(fact["value"]) <= 100
        }
        if numeric_scores:
            st.subheader("可比数值快照")
            st.bar_chart(numeric_scores, horizontal=True)
    else:
        st.info("尚无事实。可载入演示快照或手工录入。")


def page_research(api_base: str) -> None:
    """标的/行业研究页面：复用统一接口，避免前端产生另一个未核验的分析逻辑。"""
    st.title("标的与行业研究")
    research_type = st.radio("研究类型", ["个股研究", "行业分析", "市场解读", "基金筛选", "可转债分析"], horizontal=True)
    default_query = {
        "个股研究": "请研究示例科技这只个股",
        "行业分析": "请分析新能源行业",
        "市场解读": "请分析当前市场环境",
        "基金筛选": "请筛选适合我的ETF基金",
        "可转债分析": "请分析示例可转债的估值、正股与风险",
    }[research_type]
    query = st.text_input("问题", value=default_query)
    if st.button("开始研究", type="primary"):
        run_analysis(api_base, query)
    if st.session_state.advice:
        render_advice(st.session_state.advice)
        score_rows = {
            result["agent_id"]: result["score"]
            for result in st.session_state.advice.get("agent_results", [])
            if result.get("score") is not None
        }
        if score_rows:
            st.subheader("专业智能体评分对比")
            st.bar_chart(score_rows, horizontal=True)
        st.subheader("专业观点与分歧")
        for result in st.session_state.advice.get("agent_results", []):
            title = f"{result['agent_id']} · {STATUS_LABELS.get(result['status'], result['status'])}"
            with st.expander(title):
                st.write(result["opinion"])
                st.caption(f"置信度：{result['confidence']:.0%}  |  引用：{', '.join(result['facts_used']) or '无'}")
                if result.get("risk_flags"):
                    st.write("风险：" + "、".join(result["risk_flags"]))
                st.json(result.get("details", {}))


def page_portfolio(api_base: str) -> None:
    """组合页只收集权重和发起诊断；任何调整建议均由后端合规层控制。"""
    st.title("组合诊断")
    st.caption("仅提供集中度与再平衡复核提示；系统不会自动下单。")
    with st.form("portfolio_form"):
        name = st.text_input("持仓名称", placeholder="示例ETF")
        weight = st.number_input("权重", min_value=0.0, max_value=1.0, value=0.10, step=0.01, format="%.2f")
        add_holding = st.form_submit_button("加入持仓")
    if add_holding:
        if not name.strip():
            st.warning("请输入持仓名称。")
        else:
            holding = {"name": name.strip(), "weight": float(weight)}
            st.session_state.portfolio.append(holding)
            add_fact(
                {
                    "fact_id": f"PORTFOLIO-{len(st.session_state.portfolio):03d}",
                    "entity": holding["name"],
                    "field": "weight",
                    "value": holding["weight"],
                    "snapshot_time": utc_now(),
                    "source_id": "USER_PORTFOLIO_SNAPSHOT",
                    "quality": 1.0,
                }
            )
            st.success("持仓与对应权重事实已加入本次会话。")
    if st.session_state.portfolio:
        st.dataframe(st.session_state.portfolio, width="stretch", hide_index=True)
        total = sum(item["weight"] for item in st.session_state.portfolio)
        st.metric("权重合计", f"{total:.0%}")
        if total > 1.05:
            st.warning("权重合计超过 100%，请确认是否存在杠杆或口径差异。")
    else:
        st.info("尚未录入持仓；也可以在市场驾驶舱中使用演示权重事实。")
    if st.button("运行组合诊断", type="primary"):
        run_analysis(api_base, "请诊断我的持仓组合")
    if st.session_state.advice:
        render_advice(st.session_state.advice)
        if st.session_state.advice.get("allocation"):
            st.subheader("组合诊断摘要")
            st.dataframe(st.session_state.advice["allocation"], width="stretch", hide_index=True)


def page_collaboration() -> None:
    """将 Task DAG 用状态表展示；依赖关系与后端返回值一一对应，便于追溯。"""
    st.title("协作过程")
    advice = st.session_state.advice
    if not advice:
        st.info("尚无分析记录。提交一次研究或组合诊断后，这里会显示 Task DAG。")
        return
    plan = advice["task_plan"]
    st.caption(f"Trace ID：{plan['trace_id']}  |  意图：{plan['intent']}")
    if plan.get("clarification_question"):
        st.warning(plan["clarification_question"])
    rows = []
    for node in plan["nodes"]:
        rows.append(
            {
                "任务": node["task_id"],
                "智能体": node["agent_id"],
                "依赖": "、".join(node["depends_on"]) or "无（可并行）",
                "优先级": node["priority"],
                "时限（秒）": node["timeout_seconds"],
                "状态": STATUS_LABELS.get(node["status"], node["status"]),
            }
        )
    st.dataframe(rows, width="stretch", hide_index=True)
    st.caption("专业节点可并行；事实核验完成后才运行合规审核。状态和依赖均由后端生成。")


def page_evidence() -> None:
    """证据中心：按本次 AdvicePackage 的 evidence 回溯到用户授权的 FactRecord。"""
    st.title("证据中心")
    advice = st.session_state.advice
    if not advice:
        st.info("尚无分析记录。分析完成后可在这里查看实际引用的事实。")
        return
    used_ids = set(advice.get("evidence", []))
    used = [fact for fact in st.session_state.facts if fact["fact_id"] in used_ids]
    if used:
        st.success(f"共 {len(used)} 条事实通过核验并进入最终建议包。")
        st.dataframe(used, width="stretch", hide_index=True)
    else:
        st.warning("没有可展示的通过核验事实；请检查是否过期、来源为空或质量过低。")
    st.subheader("未进入最终证据的输入")
    not_used = [fact for fact in st.session_state.facts if fact["fact_id"] not in used_ids]
    if not_used:
        st.dataframe(not_used, width="stretch", hide_index=True)
        st.caption("未被引用不必然错误：可能是当前智能体不需要该字段，也可能因时效/质量被核验器降级。")
    else:
        st.caption("本次输入事实均被至少一个可用结论引用。")


def main() -> None:
    """配置页面、侧栏状态和七个产品页面。"""
    st.set_page_config(page_title="问策智投", page_icon="📈", layout="wide")
    init_session()
    st.markdown(
        """
        <style>
        .block-container {max-width: 1280px; padding-top: 2rem;}
        [data-testid="stMetricValue"] {font-size: 1.7rem;}
        </style>
        """,
        unsafe_allow_html=True,
    )
    with st.sidebar:
        st.header("问策智投")
        api_base = st.text_input("后端地址", value=DEFAULT_API_BASE)
        if st.button("检查后端连接", width="stretch"):
            health = api_request(api_base, "GET", "/health")
            if health:
                st.success("后端连接正常。")
                readiness = api_request(api_base, "GET", "/readiness")
                if readiness:
                    st.caption(f"分析模式：{readiness['mode']}")
                    st.caption(f"问财数据源：{'已配置' if readiness['iwencai_skillhub_configured'] else '未配置'}")
        st.divider()
        page = st.radio(
            "导航",
            ["首页/对话", "画像中心", "市场驾驶舱", "标的研究", "组合诊断", "协作过程", "证据中心"],
        )
        st.divider()
        st.caption(f"画像：{'已确认' if profile_ready() else '未确认'}")
        st.caption(f"事实：{len(st.session_state.facts)} 条")
        if st.session_state.advice:
            st.caption(f"最近合规状态：{st.session_state.advice['compliance']['status']}")

    pages = {
        "首页/对话": lambda: page_home(api_base),
        "画像中心": lambda: page_profile(api_base),
        "市场驾驶舱": lambda: page_market(api_base),
        "标的研究": lambda: page_research(api_base),
        "组合诊断": lambda: page_portfolio(api_base),
        "协作过程": page_collaboration,
        "证据中心": page_evidence,
    }
    pages[page]()


if __name__ == "__main__":
    main()
