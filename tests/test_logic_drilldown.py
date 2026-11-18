"""投资逻辑链逐级展开：四层结构、认可引用过滤、原始来源与派生父链。"""
from streamlit.testing.v1 import AppTest

from frontend.logic_drilldown import render_logic_chain_entry, render_logic_drilldown
from frontend.research_report import render_research_report

TIME = "2026-09-30T10:00:00+08:00"


def advice(**patch):
    data = {
        "conclusion": "综合结论：估值处于合理区间，需关注需求波动。",
        "compliance": {"status": "PASS"},
        "evidence": ["F1", "F2", "F3"],
        "facts": [
            {"fact_id": "F0", "entity": "示例公司", "field": "revenue", "value": 100,
             "unit": "亿元", "snapshot_time": TIME, "source_id": "IWENCAI:FINANCE",
             "source_url": "https://example.com/report.pdf", "period": "2026Q3", "quality": .9},
            {"fact_id": "F1", "entity": "示例公司", "field": "pe_ttm", "value": 18,
             "unit": "倍", "snapshot_time": TIME, "source_id": "IWENCAI:VALUATION",
             "source_url": "https://example.com/valuation.pdf", "period": "2026Q3", "quality": .9},
            {"fact_id": "F2", "entity": "示例行业", "field": "pmi", "value": 50.6,
             "snapshot_time": TIME, "source_id": "USER_SUPPLIED:统计局月度公布",
             "period": "202609", "quality": .9},
            {"fact_id": "F3", "entity": "示例公司", "field": "revenue_yoy", "value": .12,
             "snapshot_time": TIME, "source_id": "DERIVED:CALC", "derived_from": ["F0"],
             "derivation_rule": "YOY_V1: 本期与上期营收之比", "quality": .8},
            {"fact_id": "REJECTED", "entity": "被剔除的未认可资料", "field": "other",
             "value": "不应出现在逻辑链", "snapshot_time": TIME, "source_id": "DEMO_SNAPSHOT"},
        ],
        "agent_results": [
            {"agent_id": "industry", "status": "completed", "confidence": .8,
             "opinion": "行业景气温和回升。", "facts_used": ["F2", "REJECTED"],
             "confidence_reasons": ["数据时间较近"], "risk_flags": ["需求波动"],
             "invalidation_conditions": ["PMI 连续两月回落"]},
            {"agent_id": "security", "status": "completed", "confidence": .7,
             "opinion": "估值与盈利基本匹配。", "facts_used": ["F1", "F3"]},
        ],
        "research_report": {
            "title": "示例公司研究证据对照", "status": "reviewed",
            "summary": "依据已引用数据整理支持与不利因素。", "method": "按维度归集已核验事实。",
            "coverage": {"cited": 3, "available": 5, "dated": 3, "dimensions": ["个股研究", "行业分析"]},
            "key_findings": [{"title": "估值处于合理区间", "detail": "PE 与盈利增速基本匹配。",
                              "stance": "favorable", "fact_ids": ["F1", "REJECTED"]}],
            "sections": [{"entity": "示例公司", "title": "估值与盈利",
                          "evidence": [{"observation": "PE 18 倍", "interpretation": "低于行业中位。",
                                        "stance": "favorable", "fact_ids": ["F1"],
                                        "source": "IWENCAI:VALUATION",
                                        "source_url": "https://example.com/valuation.pdf"}],
                          "limitations": [], "change_conditions": ["盈利下修"]}],
            "limitations": [],
        },
    }
    data.update(patch)
    return data


ELEMENT_TYPES = ("markdown", "caption", "text", "header", "subheader", "expander",
                 # Streamlit 1.64 把带 key 的 expander 登记为 widget，测试树里以 status 呈现。
                 "status", "button", "link_button", "dataframe", "badge", "warning",
                 "info", "success")


def rendered_text(app) -> str:
    parts = []
    for name in ELEMENT_TYPES:
        for element in getattr(app, name, []):
            for attribute in ("value", "label"):
                # Stateless link buttons have labels but no session-state value.
                if attribute == "value" and element.type == "link_button":
                    continue
                value = getattr(element, attribute, None)
                if isinstance(value, str):
                    parts.append(value)
    # 新版 AppTest 未为 link_button / 部分容器登记快捷属性，遍历整棵树兜底收集，
    # 保证折叠层里的「打开原文」等标签也能被断言到。
    for element in app.main:
        for attribute in ("value", "label"):
            if attribute == "value" and element.type == "link_button":
                continue
            value = getattr(element, attribute, None)
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)


def run(code: str) -> AppTest:
    from unittest.mock import patch
    from frontend import streamlit_app as ui

    # AppTest shares imported modules with later tests; always restore mocks.
    with patch.object(ui, "recent_conversations", return_value=[]):
        return AppTest.from_string(code).run(timeout=30)


def test_drilldown_renders_four_levels_and_original_sources():
    app = run(f"from frontend.logic_drilldown import render_logic_drilldown\n"
              f"render_logic_drilldown({advice()!r})")
    assert not app.exception, [e.value for e in app.exception]
    text = rendered_text(app)
    assert "① 综合结论与关键信息" in text
    assert "② 分项观点" in text
    assert "③ 数据点" in text
    assert "④ 原始来源" in text
    assert "上游原始资料（1 条）" in text
    assert "YOY_V1" in text
    assert "打开原文" in text
    assert "完整数据溯源" in text


def test_drilldown_keeps_only_verified_evidence():
    app = run(f"from frontend.logic_drilldown import render_logic_drilldown\n"
              f"render_logic_drilldown({advice()!r})")
    assert not app.exception
    text = rendered_text(app)
    assert "不应出现在逻辑链" not in text
    assert "被剔除的未认可资料" not in text
    assert "[有利] 估值处于合理区间 · 1 条依据" in text
    assert "01 · 行业分析 · 1 条数据点" in text
    assert "02 · 个股研究 · 2 条数据点" in text


def test_evidence_explanation_appears_in_logic_chain_not_chat_summary():
    data = advice()
    basis = "本期市盈率为18倍，估值与盈利基本匹配。"
    data["agent_results"][1]["opinion"] = basis
    app = run("import streamlit as st\n"
              "from frontend.result_views import render_advice\n"
              "from frontend.logic_drilldown import render_logic_drilldown\n"
              f"with st.chat_message('assistant'):\n    render_advice({data!r})\n"
              f"render_logic_drilldown({data!r})")
    assert not app.exception
    bubble = "\n".join(item.value for item in app.get("chat_message")[0].markdown)
    assert data["conclusion"] in bubble and "18倍" not in bubble
    assert basis in rendered_text(app)


def test_legacy_evidence_and_limits_move_out_of_chat_without_losing_evaluation():
    data = advice(conclusion=(
        "市场：核心判断：景气偏结构性，并非普遍复苏。支持证据：制造业指标低于荣枯线。"
        "判断限制：观测窗口较短。投资评价：产业链走强更值得跟踪。\n\n"
        "行业：**核心判断**：需求侧线索强于政策侧。\n**反面证据**\n尚无订单改善的量化记录。"
    ))
    app = run("import streamlit as st\n"
              "from frontend.result_views import render_advice\n"
              "from frontend.logic_drilldown import render_logic_drilldown\n"
              f"with st.chat_message('assistant'):\n    render_advice({data!r})\n"
              f"render_logic_drilldown({data!r})")
    assert not app.exception
    bubble = "\n".join(item.value for item in app.get("chat_message")[0].markdown)
    for evaluation in ("市场：景气偏结构性，并非普遍复苏", "产业链走强更值得跟踪", "行业：需求侧线索强于政策侧"):
        assert evaluation in bubble
    chain = rendered_text(app)
    for rationale in ("制造业指标低于荣枯线", "观测窗口较短", "尚无订单改善的量化记录"):
        assert rationale not in bubble and rationale in chain


def test_drilldown_hidden_when_compliance_blocks():
    blocked = advice(compliance={"status": "BLOCK"})
    app = run(f"from frontend.logic_drilldown import render_logic_drilldown\n"
              f"render_logic_drilldown({blocked!r})")
    assert not app.exception
    text = rendered_text(app)
    assert "暂不提供逻辑链下钻" in text
    assert "① 综合结论与关键信息" not in text


def test_research_page_keeps_logic_chain_entry_visible_without_any_answer():
    """风险测评未完成、页面没有分析结果时，问答页仍能看到逐级展开与溯源入口。"""
    app = run("from frontend import streamlit_app as ui\n"
              "ui.render_logic_chain_entry(None, key='logic_entry_market')")
    assert not app.exception, [e.value for e in app.exception]
    assert "投资逻辑链 · 逐级展开与数据溯源" in rendered_text(app)


def test_logic_chain_entry_drills_into_latest_answer():
    app = run(f"from frontend.logic_drilldown import render_logic_chain_entry\n"
              f"render_logic_chain_entry({advice()!r}, key='entry', expanded=True)")
    assert not app.exception, [e.value for e in app.exception]
    text = rendered_text(app)
    assert "投资逻辑链 · 逐级展开与数据溯源" in text
    assert "① 综合结论与关键信息" in text
    assert "② 分项观点" in text
    assert "④ 原始来源" in text


def test_logic_chain_entry_explains_mechanism_without_answer():
    app = run("from frontend.logic_drilldown import render_logic_chain_entry\n"
              "render_logic_chain_entry(None, key='empty', expanded=True)")
    assert not app.exception, [e.value for e in app.exception]
    text = rendered_text(app)
    assert "① 综合结论与关键信息" in text
    assert "④ 原始来源" in text
    assert "完成风险测评并发起一次咨询" in text
    assert "引用编号由系统内部维护" in text


def test_logic_chain_does_not_add_a_separate_navigation_entry():
    """投资规划有独立入口，逻辑链入口仍位于主内容区顶部。"""
    from frontend.presentation import NAVIGATION
    from frontend.streamlit_app import render_logic_chain_banner
    assert NAVIGATION == ['主页', '风险评估', '投资规划', '投资问答', '自选研究', '持仓分析']
    assert callable(render_logic_chain_banner)
    app = run("import streamlit as st\n"
              "from frontend import streamlit_app as ui\n"
              "ui.init_session()\n"
              "ui.page_navigation()")
    assert not app.exception, [e.value for e in app.exception]
    assert not any(b.label == '逻辑链与溯源' for b in app.button)


def test_logic_chain_panels_start_collapsed_on_first_open():
    """页面刚打开时，顶部面板与问答页入口都是缩回的，只保留可点击的标题。"""
    from inspect import signature
    from frontend.logic_drilldown import render_logic_chain_entry
    from frontend.streamlit_app import render_logic_chain_banner

    assert signature(render_logic_chain_banner).parameters['expanded'].default is False
    assert signature(render_logic_chain_entry).parameters['expanded'].default is False

    app = run("import streamlit as st\n"
              "from frontend import streamlit_app as ui\n"
              "ui.init_session()\n"
              "st.session_state.conversation = []\n"
              "ui.render_logic_chain_banner('http://127.0.0.1:8000/api/v1')")
    assert not app.exception, [e.value for e in app.exception]
    text = rendered_text(app)
    assert '投资逻辑链 · 逐级展开与数据溯源' in text   # 收起时标题仍可点开
    assert '引用编号由系统内部维护' not in text        # 面板内容未渲染
    assert '演示逻辑链 · 四层一览' not in text

    entry = run("from frontend.logic_drilldown import render_logic_chain_entry\n"
                "render_logic_chain_entry(None, key='collapsed_entry')")
    assert not entry.exception, [e.value for e in entry.exception]
    entry_text = rendered_text(entry)
    assert '投资逻辑链 · 逐级展开与数据溯源' in entry_text
    assert '完成风险测评并发起一次咨询' not in entry_text


def test_banner_keeps_a_new_conversation_empty_and_never_fetches_other_history():
    app = run("from unittest.mock import patch\n"
              "from frontend import streamlit_app as ui\n"
              "ui.init_session()\n"
              "with patch.object(ui, 'api_request', side_effect=AssertionError('must not fetch history')):\n"
              "    ui.render_logic_chain_banner('http://offline', expanded=True)\n")
    assert not app.exception
    text = rendered_text(app)
    assert '本对话尚未发起咨询' in text
    assert '演示逻辑链' not in text and '示例科技' not in text


def test_demo_advice_supports_full_four_level_drilldown():
    """折叠展开后使用的同一份演示包，确实能一路点到原文与派生父链。"""
    app = run("from frontend.logic_drilldown import render_logic_drilldown\n"
              "from frontend.logic_demo import DEMO_ADVICE\n"
              "render_logic_drilldown(DEMO_ADVICE, key='demo')")
    assert not app.exception, [e.value for e in app.exception]
    text = rendered_text(app)
    for expected in ('① 综合结论与关键信息', '② 分项观点', '③ 数据点', '④ 原始来源',
                     '上游原始资料（1 条）', '数据溯源（7 条）', '打开原文',
                     'YOY_V1', '22.4 倍'):
        assert expected in text, expected
    assert '不应出现在逻辑链里' not in text  # 未通过核验的资料被过滤


def test_banner_does_not_replace_an_unverified_answer_with_demo_or_old_answers():
    empty_answer = {"conclusion": "需要补充资料", "compliance": {"status": "PASS"}, "facts": [], "evidence": []}
    app = run("from frontend import streamlit_app as ui\n"
              "ui.init_session()\n"
              f"ui.st.session_state.conversation = [{{'role':'user','content':'当前问题'}},{{'role':'assistant','payload':{empty_answer!r}}}]\n"
              "ui.render_logic_chain_banner('http://offline', expanded=True)\n")
    assert not app.exception
    text = rendered_text(app)
    assert '没有通过核验的数据引用' in text
    assert '演示逻辑链' not in text and '示例科技' not in text


def test_has_traceable_evidence_filters_empty_and_blocked_answers():
    from frontend.logic_drilldown import has_traceable_evidence
    from frontend.logic_demo import DEMO_ADVICE
    assert has_traceable_evidence(DEMO_ADVICE)
    assert has_traceable_evidence(advice())
    assert not has_traceable_evidence(None)
    assert not has_traceable_evidence({"conclusion": "只有结论", "facts": [], "evidence": []})
    assert not has_traceable_evidence(advice(evidence=[], facts=[]))
    assert not has_traceable_evidence(advice(compliance={"status": "BLOCK"}))


def test_banner_drills_into_current_answer():
    app = run(f"import streamlit as st\n"
              f"from frontend import streamlit_app as ui\n"
              f"ui.init_session()\n"
              f"st.session_state.conversation = [{{'role': 'user', 'content': '请分析'}},\n"
              f"    {{'role': 'assistant', 'content': '答', 'payload': {advice()!r}}}]\n"
              f"ui.render_logic_chain_banner('http://127.0.0.1:8000/api/v1', expanded=True)")
    assert not app.exception, [e.value for e in app.exception]
    text = rendered_text(app)
    assert '① 综合结论与关键信息' in text
    assert '② 分项观点' in text
    assert '④ 原始来源' in text
    assert '上游原始资料（1 条）' in text
    assert '估值处于合理区间，需关注需求波动' in text  # 真实回答优先
    assert '演示数据 · 仅用于展示' not in text


def test_research_report_exposes_fact_ids_and_source_url():
    app = run(f"from frontend.research_report import render_research_report\n"
              f"render_research_report({advice()!r}, key='unit', compact=False)")
    assert not app.exception, [e.value for e in app.exception]
    text = rendered_text(app)
    assert "查看该维度的原始数据与来源（1 条）" in text
    assert "打开原文" in text
    assert "favorable" not in text  # 立场已中文化
