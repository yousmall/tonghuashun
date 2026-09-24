"""用户界面回归：信息简化不能隐藏风险或破坏会话交互。"""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from frontend.streamlit_app import (
    NAVIGATION,
    analysis_chain_stages,
    display_value,
    format_api_error,
    plain_language,
    profile_evidence_language,
    portfolio_risk_snapshot,
    source_trace_rows,
)


SETUP = """
from unittest.mock import patch
import streamlit as st
from frontend import streamlit_app as ui
ui.init_session()
st.session_state.auth_token = 'ui-test'
st.session_state.auth_user = {'id': 1, 'username': 'test-user'}
"""
MAIN = """
with patch.object(ui, 'api_request', return_value={'id': 1, 'username': 'test-user'}), patch.object(ui, 'register_browser_session'), patch.object(ui, 'enforce_session_timeout'):
    ui.main()
"""


def test_login_renders_without_backend():
    app = AppTest.from_file(str(Path(__file__).parents[1] / 'frontend/streamlit_app.py')).run()
    assert not app.exception
    assert [tab.label for tab in app.tabs] == ['登录', '注册']
    assert not app.code


def test_main_reuses_login_user_without_rechecking_auth_me():
    script = SETUP + """
st.session_state.profile_restored = True
paths = []
def fake_api(base, method, path, payload=None, **kwargs):
    paths.append(path)
    return {'id': 1, 'username': 'test-user'}
with patch.object(ui, 'api_request', side_effect=fake_api), patch.object(ui, 'register_browser_session'), patch.object(ui, 'enforce_session_timeout'):
    ui.main()
st.session_state.request_paths = paths
"""
    app = AppTest.from_string(script).run()

    assert not app.exception
    assert "/auth/me" not in app.session_state["request_paths"]


def test_navigation_and_profile_call_to_action():
    app = AppTest.from_string(SETUP + MAIN).run()
    assert not app.exception
    assert app.radio[0].options == ['投资问答', '自选研究', '持仓分析', '投资偏好']
    assert app.chat_input[0].disabled
    next(button for button in app.button if button.label == '填写投资偏好').click().run(timeout=15)
    assert not app.exception
    assert app.title[0].value == '投资偏好'
    assert '用户标识' not in [field.label for field in app.text_input]
    plan = next(field for field in app.text_area if field.label == '你的投资计划')
    assert plan.placeholder == '例如：2 年后买房，最多接受 8% 亏损，期间可能随时需要使用这笔钱。'
    assert '投资 3 年' not in plan.placeholder
    assert '期望年化收益' not in plan.placeholder



def test_analysis_details_remains_reachable_without_sidebar_history_entry():
    script = SETUP + """
st.session_state.profile_restored = True
st.session_state.watchlist_loaded = True
st.session_state.advice = {'conclusion': '仍需核实。', 'compliance': {'status': 'REVIEW'}}
def fake_api(base, method, path, payload=None, **kwargs):
    if path == '/history?limit=20' or path == '/history':
        return []
    return None
with patch.object(ui, 'api_request', side_effect=fake_api), \\
     patch.object(ui, 'register_browser_session'), \\
     patch.object(ui, 'enforce_session_timeout'):
    ui.main()
"""
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    assert '历史记录' not in app.radio[0].options
    next(button for button in app.button if button.label == '查看分析详情').click().run(timeout=15)
    assert not app.exception
    assert app.title[0].value == '历史记录'
    next(button for button in app.button if button.label == '返回投资问答').click().run(timeout=15)
    assert not app.exception
    assert app.session_state['history_view'] is False


def test_home_hides_manual_research_fetch_and_uses_automatic_acquisition():
    script = SETUP + """
st.session_state.profile['confirmed'] = True
ui.page_home('http://localhost')
"""
    app = AppTest.from_string(script).run(timeout=15)

    assert not app.exception
    assert not any('研究资料' in item.label for item in app.expander)
    assert not any(button.label == '查询并加入资料' for button in app.button)
    assert not any(field.label == '想查什么' for field in app.selectbox)
    assert any('按需获取资料' in item.value for item in app.caption)


def test_advice_shown_once_and_all_risks_retained():
    script = SETUP + """
st.session_state.profile['confirmed'] = True
advice = {'compliance': {'status': 'REVIEW'}, 'conclusion': '价格仍可能下跌。',
          'risks': ['风险甲', '风险乙', '风险丙', '风险丁'],
          'next_steps': ['核对最新资料'], 'data_acquisition': {'mode': 'unavailable'}}
st.session_state.conversation = [
    {'role': 'user', 'content': '怎么看？'},
    {'role': 'assistant', 'content': advice['conclusion'], 'payload': advice}]
st.session_state.advice = advice
ui.page_home('http://localhost')
"""
    app = AppTest.from_string(script).run()
    assert not app.exception
    assert sum(item.value == '价格仍可能下跌。' for item in app.markdown) == 1
    assert not app.metric
    assert len(app.warning) == 2
    assert any('风险丁' in item.value for item in app.markdown)
    assert any('其余需要注意' in item.label for item in app.expander)


def test_failed_analysis_does_not_append_or_send_result_payload():
    script = SETUP + """
st.session_state.profile['confirmed'] = True
st.session_state.conversation = [{'role': 'assistant', 'content': '旧结论', 'payload': {'internal': 'large'}}]
def fake_stream(base, payload):
    st.session_state.sent = payload
    return None
with patch.object(ui, 'stream_analysis', side_effect=fake_stream):
    ui.run_analysis('http://localhost', '新问题')
"""
    app = AppTest.from_string(script).run()
    assert not app.exception
    assert len(app.session_state['conversation']) == 1
    assert all('payload' not in turn for turn in app.session_state['sent']['context_messages'])


def test_portfolio_percent_and_independent_result():
    app = AppTest.from_string(SETUP + """
st.session_state.advice = {'conclusion': '别的研究结果'}
ui.page_portfolio('http://localhost')
""").run()
    assert not app.exception
    app.text_input[0].set_value('测试基金')
    app.number_input[0].set_value(25.0)
    next(button for button in app.button if button.label == '加入持仓').click().run(timeout=15)
    assert not app.exception
    assert app.session_state['portfolio'][0]['weight'] == .25
    assert len(app.get('vega_lite_chart')) >= 1
    assert not any(item.value == '别的研究结果' for item in app.markdown)


def test_watchlist_page_adds_account_item_and_supports_comparison():
    script = SETUP + """
st.session_state.profile['confirmed'] = True
st.session_state.watchlist = []
def fake_api(base, method, path, payload=None, **kwargs):
    if method == 'POST' and path == '/watchlist':
        return {'id': 1, 'target': payload['target'], 'asset_type': payload['asset_type'],
                'created_at': '2026-09-12T08:00:00Z'}
    return None
with patch.object(ui, 'api_request', side_effect=fake_api):
    ui.page_watchlist('http://localhost')
"""
    app = AppTest.from_string(script).run(timeout=15)
    next(field for field in app.text_input if field.label == '名称或代码').set_value('贵州茅台')
    next(button for button in app.button if button.label == '加入自选').click().run(timeout=15)

    assert not app.exception
    assert app.session_state['watchlist'][0]['target'] == '贵州茅台'
    assert any(button.label == '开始研究' for button in app.button)
    assert any(button.label == '移除' for button in app.button)


def test_watchlist_search_and_type_filter_keep_actions_on_visible_items():
    script = SETUP + """
st.session_state.profile['confirmed'] = True
st.session_state.watchlist = [
    {'id': 1, 'target': '贵州茅台', 'asset_type': '股票'},
    {'id': 2, 'target': '沪深300ETF', 'asset_type': '基金'},
    {'id': 3, 'target': '宁德时代', 'asset_type': '股票'},
]
ui.page_watchlist('http://localhost')
"""
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    next(field for field in app.text_input if field.label == '搜索自选').set_value('300').run(timeout=15)
    assert not app.exception
    assert len([button for button in app.button if button.label == '开始研究']) == 1
    assert any('沪深300ETF' in item.value for item in app.markdown)
    next(field for field in app.text_input if field.label == '搜索自选').set_value('').run(timeout=15)
    next(field for field in app.selectbox if field.label == '筛选类型').set_value('股票').run(timeout=15)
    assert not app.exception
    assert len([button for button in app.button if button.label == '开始研究']) == 2
    assert len(app.session_state['watchlist']) == 3


def test_history_chart_fetches_only_on_click_and_uses_dated_results():
    script = SETUP + """
st.session_state.watchlist = [{'id': 1, 'target': '600519', 'asset_type': '股票'}]
st.session_state.setdefault('history_fetches', [])
def fake_api(base, method, path, payload=None, **kwargs):
    if path == '/data/price-history':
        st.session_state.history_fetches.append(payload)
        return {'target': '600519', 'asset_type': '股票', 'status': 'ok',
                'metric_label': '收盘价', 'source_name': '同花顺问财',
                'fetched_at': '2026-09-24T08:00:00Z',
                'points': [{'date': '2026-09-21', 'value': 10.0},
                           {'date': '2026-09-22', 'value': 11.0}]}
    return None
with patch.object(ui, 'api_request', side_effect=fake_api):
    ui.page_watchlist('http://localhost')
"""
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    assert app.session_state['history_fetches'] == []
    assert not app.get('vega_lite_chart')
    next(button for button in app.button if button.label == '加载 / 刷新近 30 期走势').click().run(timeout=15)
    assert not app.exception
    assert app.session_state['history_fetches'] == [
        {'target': '600519', 'asset_type': '股票', 'limit': 30}
    ]
    assert len(app.get('vega_lite_chart')) == 1


def test_history_chart_shows_empty_state_without_a_curve():
    script = SETUP + """
st.session_state.watchlist = [{'id': 1, 'target': '600519', 'asset_type': '股票'}]
def fake_api(base, method, path, payload=None, **kwargs):
    if path == '/data/price-history':
        return {'target': '600519', 'asset_type': '股票', 'status': 'empty',
                'points': [], 'message': '问财暂未返回足够的带日期数值，无法绘制走势。'}
    return None
with patch.object(ui, 'api_request', side_effect=fake_api):
    ui.page_watchlist('http://localhost')
"""
    app = AppTest.from_string(script).run(timeout=15)
    next(button for button in app.button if button.label == '加载 / 刷新近 30 期走势').click().run(timeout=15)
    assert not app.exception
    assert not app.get('vega_lite_chart')
    assert any('无法绘制走势' in item.value for item in app.info)


def test_portfolio_risk_snapshot_is_deterministic_and_respects_profile_limit():
    snapshot = portfolio_risk_snapshot(
        [
            {'name': '甲', 'weight': 0.35},
            {'name': '乙', 'weight': 0.25},
            {'name': '丙', 'weight': 0.10},
        ],
        {'single_security_limit': 0.30},
    )

    assert snapshot['total'] == pytest.approx(0.70)
    assert snapshot['top_two'] == pytest.approx(0.60)
    assert snapshot['unallocated'] == pytest.approx(0.30)
    assert snapshot['over_limit'] == ['甲']


def test_plain_language_preserves_small_values_and_disagreement():
    assert display_value('fee_rate', .005) == '0.5%'
    result = plain_language('协调器置信加权共识分为 50.0。security：基本面与技术面存在分歧，不能确定上涨。')
    assert '共识' not in result and 'security' not in result
    assert '分歧' in result and '不能确定上涨' in result


def test_profile_evidence_uses_friendly_language_without_internal_field_names():
    evidence = [
        '从“两年后准备买房”提取 horizon_months：24',
        '从“接受5%的年亏损”提取 max_drawdown：0.05',
        '从“两年后准备买房”提取 liquidity_need：高',
        '从“两年后准备买房”提取 target：买房',
        '已按五项问卷加权公式计算风险分',
        '已记录用户明确提交的 investment_experience_years',
        '已记录用户明确提交的 investment_history',
        '已记录用户明确提交的 expected_annual_return',
    ]

    rendered = [profile_evidence_language(item) for item in evidence]

    assert rendered == [
        '你提到“两年后准备买房”，因此评估为：计划投资约 2 年。',
        '你提到“接受5%的年亏损”，因此评估为：最多可接受约 5% 的阶段性亏损。',
        '你提到“两年后准备买房”，因此评估为：这笔资金可能需要随时使用。',
        '你提到“两年后准备买房”，因此评估为：投资目标是买房。',
        '根据你对五项风险问题的回答，已综合评估你的风险承受能力。',
        '已记录你填写的投资经验。',
        '已记录你填写的投资经历。',
        '已记录你填写的期望年化收益。',
    ]
    assert not any(
        token in item
        for item in rendered
        for token in (
            'horizon_months', 'max_drawdown', 'liquidity_need', 'target',
            'investment_experience_years', 'investment_history', 'expected_annual_return',
        )
    )


def test_internal_review_diagnostics_are_replaced_before_rendering():
    diagnostic = (
        '两个节点均标记为degraded且未形成有效观点，nodes_without_opinion已明确其不构成矛盾方，'
        '故conflicting_agents为空。事实支持方面：market节点引用资料IW-CC98B95DA0E14918，'
        '但存在跨时段混用，属证据支持不充分，标记UNSUPPORTED_CLAIM。'
    )
    public_text = plain_language(diagnostic)

    assert public_text == (
        '部分表述与现有资料或数据时点不完全一致，暂不能作为投资判断依据。'
        '请使用同一时点的最新资料重新核对。'
    )
    assert plain_language('该分析被标记为degraded且没有结论。') == (
        '现有资料有限，部分分析暂未形成可靠结论。'
        '请补充最新且可核验的数据后再作判断。'
    )
    assert not any(marker in public_text for marker in (
        'degraded', 'nodes_without_opinion', 'conflicting_agents', 'market节点',
        'IW-CC98B95DA0E14918', 'UNSUPPORTED_CLAIM',
    ))

    script = SETUP + f"""
advice = {{
    'conclusion': {diagnostic!r},
    'compliance': {{'status': 'REVIEW', 'reason': {diagnostic!r}}},
}}
ui.render_conclusion_panel(advice)
"""
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    rendered = '\n'.join(
        [item.value for item in app.markdown]
        + [item.value for item in app.caption]
    )
    assert public_text in rendered
    assert rendered.count(public_text) == 1
    assert not any(marker in rendered for marker in (
        'degraded', 'nodes_without_opinion', 'conflicting_agents', 'market节点',
        'IW-CC98B95DA0E14918', 'UNSUPPORTED_CLAIM',
    ))


def test_history_restores_payload_and_navigates_to_chat():
    script = SETUP + """
st.session_state.setdefault('navigation', '历史记录')
answer = {'compliance': {'status': 'REVIEW'}, 'conclusion': '历史判断仍需核实。'}
def fake_api(base, method, path, payload=None, **kwargs):
    if path.startswith('/history?limit=21'):
        return [{'id': 'saved', 'title': '已保存的研究', 'message_count': 2, 'updated_at': '2026-09-09'}]
    if path == '/history/saved':
        return {'id': 'saved', 'title': '已保存的研究', 'messages': [
            {'role': 'user', 'content': '原问题', 'created_at': '2026-09-09'},
            {'role': 'assistant', 'content': answer['conclusion'], 'payload': answer, 'created_at': '2026-09-09'}]}
    if path == '/profile':
        return {'profile': {'risk_level': 'R3', 'confirmed': False}}
    return {'id': 1, 'username': 'test-user'}
with patch.object(ui, 'api_request', side_effect=fake_api), patch.object(ui, 'register_browser_session'), patch.object(ui, 'enforce_session_timeout'):
    ui.main()
"""
    app = AppTest.from_string(script).run()
    next(button for button in app.button if button.label == '恢复并继续对话').click().run(timeout=15)
    assert not app.exception
    assert app.radio[0].value == '投资问答'
    assert app.session_state['conversation_id'] == 'saved'
    assert app.session_state['conversation'][1]['payload']['compliance']['status'] == 'REVIEW'
    assert len(app.warning) == 1


def test_market_query_passes_fund_filter_and_keeps_news():
    script = SETUP + """
def fake_api(base, method, path, payload=None):
    st.session_state.sent = payload
    return {'facts': [{'fact_id': 'news-1', 'field': 'news', 'entity': '测试基金', 'value': '一条测试公告',
                       'snapshot_time': '2026-09-09T08:00:00Z', 'source_id': 'IWENCAI_SKILLHUB', 'quality': .9}]}
with patch.object(ui, 'api_request', side_effect=fake_api):
    ui.render_materials_body('http://localhost')
"""
    app = AppTest.from_string(script).run(timeout=15)
    app.selectbox[0].select('基金 / ETF')
    next(field for field in app.text_input if field.label == '股票、基金或查询条件').set_value('低费率宽基')
    next(button for button in app.button if button.label == '查询并加入资料').click().run(timeout=15)
    assert not app.exception
    assert app.session_state['sent']['filters'] == {'query': '低费率宽基'}
    assert app.session_state['sent']['kind'] == 'fund'
    assert '一条测试公告' in app.dataframe[0].value.to_string()


def test_iwencai_configuration_error_stays_actionable():
    message = format_api_error("尚未配置 IWENCAI_API_KEY，请在 .env 中配置只读密钥并重启后端。")

    assert message == "尚未配置问财访问密钥。请在 .env 中设置 IWENCAI_API_KEY，并重启后端。"


def test_material_query_explains_provider_failure_and_keeps_existing_facts():
    script = SETUP + """
st.session_state.facts = [{'fact_id': 'old', 'field': 'news', 'entity': '旧资料', 'value': '保留内容'}]
def fake_api(base, method, path, payload=None):
    return {'status': 'unavailable', 'facts': [],
            'message': '无法建立问财服务连接，请检查网络、DNS 或代理设置后重试。'}
with patch.object(ui, 'api_request', side_effect=fake_api):
    ui.render_materials_body('http://localhost')
"""
    app = AppTest.from_string(script).run(timeout=15)
    next(field for field in app.text_input if field.label == '股票、基金或查询条件').set_value('贵州茅台')
    next(button for button in app.button if button.label == '查询并加入资料').click().run(timeout=15)

    assert not app.exception
    assert [fact['fact_id'] for fact in app.session_state['facts']] == ['old']
    assert any('无法建立问财服务连接' in warning.value for warning in app.warning)
    assert not app.error


def test_manual_material_preserves_percent_source_and_date():
    app = AppTest.from_string(SETUP + "ui.render_materials_body('http://localhost')").run(timeout=15)
    next(field for field in app.text_input if field.label == '资料涉及的对象').set_value('测试基金')
    next(field for field in app.selectbox if field.label == '指标或资料类型').select('fee_rate')
    app.text_area[0].set_value('0.5%')
    next(field for field in app.text_input if field.label == '资料来源').set_value('基金合同')
    next(button for button in app.button if button.label == '保存资料').click().run(timeout=15)
    assert not app.exception
    fact = app.session_state['facts'][0]
    assert fact['value'] == .005
    assert fact['source_id'] == 'USER_SUPPLIED:基金合同'
    assert fact['snapshot_time'].endswith('+08:00')
    assert '0.5%' in app.dataframe[0].value.to_string()


def test_clear_materials_retains_historical_evidence():
    script = SETUP + """
if 'prepared' not in st.session_state:
    st.session_state.prepared = True
    st.session_state.facts = [{'fact_id': 'old', 'field': 'news', 'entity': '旧资料', 'value': '原始新闻'}]
    st.session_state.advice = {'trace_id': 'analysis-1', 'evidence': ['old']}
ui.render_materials_body('http://localhost')
"""
    app = AppTest.from_string(script).run(timeout=15)
    next(button for button in app.button if button.label == '清空研究资料').click().run(timeout=15)
    assert not app.exception
    assert not app.session_state['facts']
    assert app.session_state['advice']['facts'][0]['value'] == '原始新闻'


def test_quick_ask_submits_research_prefix():
    """原“专题研究”已并入问答页的研究方向提问。"""
    script = SETUP + """
st.session_state.profile['confirmed'] = True
def fake_stream(base, payload):
    st.session_state.sent_query = payload['query']
    return {'trace_id': 'research-1', 'conclusion': '个股专属结果', 'compliance': {'status': 'PASS'}}
with patch.object(ui, 'stream_analysis', side_effect=fake_stream):
    ui.render_quick_ask('http://localhost')
"""
    app = AppTest.from_string(script).run(timeout=15)
    app.segmented_control[0].select('个股研究').run(timeout=15)
    next(field for field in app.text_input if field.label == '关注的对象或想了解的问题').set_value('分析测试股票')
    next(button for button in app.button if button.label == '开始分析').click().run(timeout=15)
    assert not app.exception
    assert app.session_state['sent_query'] == '个股研究：分析测试股票'


def test_quick_ask_keeps_typed_question_when_direction_and_submit_share_one_run():
    """方向按钮和输入框同在一个 form 里：点方向不会触发重跑，所以“选方向→填内容→提交”
    对用户只是一次提交，上面的用例分两次 run 反而掩盖了真实路径。

    此时输入框若没有固定 key，它的控件标识会随 placeholder（这里跟着研究方向变化）改变：
    浏览器按旧标识回传内容，服务端按新标识找不到对应控件，用户输入被整段丢弃，只剩下默认问题。
    """
    script = SETUP + """
st.session_state.profile['confirmed'] = True
def fake_stream(base, payload):
    st.session_state.sent_query = payload['query']
    return {'trace_id': 'research-2', 'conclusion': '个股专属结果', 'compliance': {'status': 'PASS'}}
with patch.object(ui, 'stream_analysis', side_effect=fake_stream):
    ui.render_quick_ask('http://localhost')
"""
    app = AppTest.from_string(script).run(timeout=15)
    app.segmented_control[0].select('个股研究')
    next(field for field in app.text_input if field.label == '关注的对象或想了解的问题').set_value('分析测试股票')
    next(button for button in app.button if button.label == '开始分析').click().run(timeout=15)
    assert not app.exception
    assert app.session_state['sent_query'] == '个股研究：分析测试股票'


def test_details_uses_selected_result_evidence_not_current_materials():
    script = SETUP + """
st.session_state.facts = [{'fact_id':'e1','entity':'其他对象','field':'news','value':'不应串入'}]
st.session_state.analysis_detail_view = '数据溯源'
st.session_state.advice = {'trace_id':'a1', 'evidence':['e1'], 'compliance':{'status':'REVIEW'},
    'facts':[{'fact_id':'e1','entity':'原对象','field':'news','value':'原始资料'},
             {'fact_id':'e2','entity':'未引用对象','field':'news','value':'未引用资料'}],
    'agent_results':[{'agent_id':'security','opinion':'保留正反两种观点','status':'degraded'}],
    'task_plan':{'nodes':[{'agent_id':'security','status':'completed'}]}}
ui.render_analysis_details()
"""
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    assert [tab.label for tab in app.tabs] == ['结论面板','投资逻辑链','数据溯源','执行记录']
    assert '原始资料' in app.dataframe[0].value.to_string()
    assert '不应串入' not in app.dataframe[0].value.to_string()
    assert any('未被这次分析引用' in item.label for item in app.expander)
    assert not app.metric and not app.code


def test_logic_chain_labels_one_source_pass_as_internal_check() -> None:
    advice = {
        "evidence": ["one"],
        "facts": [{"fact_id": "one", "source_id": "IWENCAI_SKILLHUB",
                   "entity": "示例", "field": "pe_ttm", "value": 20}],
        "agent_results": [{
            "agent_id": "security", "status": "completed", "facts_used": ["one"]
        }],
        "cross_validation": {"status": "PASS", "issues": []},
        "compliance": {"status": "PASS"},
    }
    stages = analysis_chain_stages(advice)
    assert stages[2]["value"] == "已通过"
    assert stages[2]["note"] == "单一来源内核验通过"


def test_logic_chain_shows_automatic_evidence_gap_and_original_source() -> None:
    advice = {
        "evidence": ["derived"],
        "facts": [
            {"fact_id": "raw", "source_id": "MARKET_A", "field": "pe_ttm",
             "entity": "示例", "value": 20},
            {"fact_id": "derived", "source_id": "DERIVED_RULE_V1",
             "derived_from": ["raw"], "field": "valuation_score",
             "entity": "示例", "value": 60},
        ],
        "agent_results": [{
            "agent_id": "security", "status": "degraded",
            "details": {"rejected_reference_count": 2},
            "facts_used": ["derived"],
        }],
        "cross_validation": {
            "status": "REVIEW",
            "issues": [{"code": "EVIDENCE_REFERENCE_REJECTED", "message": "引用不足"}],
        },
        "compliance": {"status": "REVIEW"},
    }
    stages = analysis_chain_stages(advice)
    assert stages[0]["note"] == "1 个来源 · 2 条引用待补齐"
    assert stages[0]["tone"] == "review"
    assert stages[2]["note"] == "部分引用未通过核验"


def test_logic_chain_and_source_trace_keep_view_to_fact_relationship():
    advice = {
        'evidence': ['price', 'news'],
        'facts': [
            {'fact_id': 'price', 'entity': '测试公司', 'field': 'close_price', 'value': 10,
             'snapshot_time': '2026-09-11T01:00:00Z', 'source_id': 'IWENCAI_SKILLHUB',
             'source_url': 'https://example.com/price'},
            {'fact_id': 'news', 'entity': '测试公司', 'field': 'news', 'value': '公司发布公告',
             'snapshot_time': '2026-09-11T02:00:00Z', 'source_id': 'USER_SUPPLIED:公司财经'},
            {'fact_id': 'unused', 'entity': '其他公司', 'field': 'news', 'value': '不相关资料',
             'snapshot_time': '2026-09-11T03:00:00Z', 'source_id': 'USER_SUPPLIED:其他'},
        ],
        'agent_results': [
            {'agent_id': 'security', 'status': 'completed', 'facts_used': ['price', 'news']},
            {'agent_id': 'industry', 'status': 'degraded', 'facts_used': ['news']},
        ],
        'cross_validation': {'status': 'PASS'},
        'compliance': {'status': 'REVIEW'},
    }

    stages = analysis_chain_stages(advice)
    assert [stage['label'] for stage in stages] == ['数据基础', '分项研判', '交叉核验', '风险结论']
    assert stages[0]['value'] == '2 条引用'
    assert stages[1]['value'] == '2/2 已研判'
    assert stages[1]['note'] == '1 项结论完整 · 1 项资料有限'
    assert stages[1]['tone'] == 'review'
    rows = source_trace_rows(advice)
    assert len(rows) == 2
    assert rows[0]['支持分析'] == '个股研究'
    assert rows[1]['支持分析'] == '个股研究、行业分析'
    assert rows[0]['来源'] == '同花顺问财'
    assert rows[0]['原文'] == 'https://example.com/price'
    assert rows[1]['原文'] is None
    assert all('fact_id' not in row for row in rows)
    assert all('不相关资料' not in str(row) for row in rows)


def test_logic_chain_does_not_claim_missing_checks_passed_or_restore_rejected_evidence():
    advice = {
        'evidence': ['verified'],
        'facts': [
            {'fact_id': 'verified', 'entity': '测试公司', 'field': 'news', 'value': '已核验资料'},
            {'fact_id': 'rejected', 'entity': '测试公司', 'field': 'news', 'value': '已剔除资料'},
        ],
        'agent_results': [{
            'agent_id': 'security', 'status': 'TaskStatus.DEGRADED', 'confidence': 0.4,
            'opinion': '资料有限，仅作条件分析。', 'facts_used': ['verified', 'rejected'],
            'confidence_reasons': ['部分资料未通过核验'],
        }],
        'compliance': {'status': 'PASS'},
    }

    stages = analysis_chain_stages(advice)
    assert stages[1]['value'] == '1/1 已研判'
    assert stages[1]['note'] == '1 项资料有限'
    assert stages[2]['value'] == '待复核'
    assert stages[2]['tone'] == 'review'

    script = SETUP + f"""
advice = {advice!r}
ui.render_logic_chain(advice)
    """
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    markdown_values = [item.value for item in app.markdown]
    assert any('资料有限' in value for value in markdown_values)
    assert '已核验资料' in app.dataframe[0].value.to_string()
    assert '已剔除资料' not in app.dataframe[0].value.to_string()
    assert any('可信度 40%' in value for value in markdown_values)
    assert any('交叉核验尚未通过' in item.value for item in app.caption)
    assert not any('未发现需要单独提示' in value for value in markdown_values)


def test_display_keeps_unmapped_business_fields_and_explicit_percent():
    from frontend.streamlit_app import friendly_fact_rows, manual_fact_value
    import pytest
    rows = friendly_fact_rows([{'field':'公告全文','entity':'公司','value':'完整公告'},
                               {'field':'new_business_field','entity':'公司','value':123}])
    assert len(rows) == 2 and rows[0]['指标'] == '公告全文'
    assert rows[1]['指标'] == '其他资料'
    assert display_value('fee_rate','0.5%') == '0.5%'
    with pytest.raises(ValueError):
        manual_fact_value('close_price','NaN')


def test_logout_then_login_restores_saved_profile():
    """退出后再登录必须能恢复已确认画像，而不是要求用户重填问卷。"""
    script = SETUP + """
import streamlit as st
from frontend import streamlit_app as ui

if 'stage' not in st.session_state:
    st.session_state.stage = 'logout'

if st.session_state.stage == 'logout':
    ui.reset_user_session()
    assert 'auth_token' not in st.session_state
    assert st.session_state.profile['confirmed'] is False
    assert 'user_id' not in st.session_state.profile
    st.session_state.stage = 'login'
    st.rerun()
else:
    st.session_state.auth_token = 'token'
    with patch.object(ui, 'api_request', return_value={'profile': {
            'user_id': '7', 'risk_level': 'R3', 'confirmed': True, 'horizon_months': 24,
            'max_drawdown': 0.08, 'expected_annual_return': 0.08, 'constraints': []},
            'evidence': ['已从上次保存的投资偏好恢复。']}):
        ui._restore_profile('http://localhost')
"""
    app = AppTest.from_string(script).run()
    assert not app.exception
    assert app.session_state['profile']['confirmed'] is True
    # 前端状态里不能残留后端标识，避免任何界面回显账号 id。
    assert 'user_id' not in app.session_state['profile']


def test_insights_merges_history_and_analysis_details():
    script = SETUP + """
import streamlit as st
answer = {'trace_id': 'a1', 'conclusion': '仍需核实。', 'compliance': {'status': 'REVIEW'}}
st.session_state.advice = answer
st.session_state.conversation = [
    {'role': 'user', 'content': '怎么看？'},
    {'role': 'assistant', 'content': answer['conclusion'], 'payload': answer}]
def fake_api(base, method, path, payload=None):
    if path == '/history':
        return [{'id': 'c1', 'title': '已保存的对话', 'message_count': 2, 'updated_at': '2026-09-10T09:00:00Z'}]
    if path == '/history/c1':
        return {'id': 'c1', 'title': '已保存的对话', 'messages': [
            {'role': 'user', 'content': '怎么看？', 'created_at': '2026-09-10T09:00:00Z'},
            {'role': 'assistant', 'content': '仍需核实。', 'payload': answer, 'created_at': '2026-09-10T09:00:01Z'}]}
    return {}
with patch.object(ui, 'api_request', side_effect=fake_api):
    ui.page_insights('http://localhost')
"""
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    # 外层是"对话记录 / 分析详情"，内层还会渲染分析详情自己的三个页签。
    assert [tab.label for tab in app.tabs][:2] == ['对话记录', '分析详情']




def test_recent_chats_collapsed_skips_history_request():
    script = SETUP + """
st.session_state['recent-chats'] = False
with patch.object(ui, 'api_request') as request:
    ui.render_recent_chats('http://localhost')
    st.session_state.history_requests = [
        call.args[2] for call in request.call_args_list
        if len(call.args) >= 3 and call.args[2].startswith('/history')
    ]
"""
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    assert app.session_state['recent-chats'] is False
    assert app.session_state['history_requests'] == []
    assert not any(button.key.startswith('recent_chat_') for button in app.button)


def test_sidebar_recent_chat_restores_history_and_new_chat_starts_fresh():
    script = SETUP + """
st.session_state.profile_restored = True
st.session_state.watchlist_loaded = True
st.session_state.facts = [{'fact_id': 'prepared', 'field': 'news',
                           'entity': '公司', 'value': '事先查好的资料'}]
def fake_api(base, method, path, payload=None, **kwargs):
    if path == '/history?limit=20':
        return [
            {'id': 'saved-one', 'title': '之前的问题', 'message_count': 2},
            {'id': 'saved-two', 'title': '另一段聊天', 'message_count': 2},
        ]
    if path == '/history/saved-one':
        return {'id': 'saved-one', 'title': '之前的问题', 'messages': [
            {'role': 'user', 'content': '之前的问题', 'created_at': '2026-09-10T09:00:00Z'},
            {'role': 'assistant', 'content': '旧结论', 'created_at': '2026-09-10T09:00:01Z',
             'payload': {'conclusion': '旧结论', 'compliance': {'status': 'REVIEW'}}},
        ]}
    return None
with patch.object(ui, 'api_request', side_effect=fake_api), \\
     patch.object(ui, 'register_browser_session'), \\
     patch.object(ui, 'enforce_session_timeout'):
    ui.main()
"""
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    assert any(button.label == '新建对话' for button in app.button)
    assert any(button.label == '之前的问题' for button in app.button)

    next(button for button in app.button if button.label == '之前的问题').click().run(timeout=15)
    assert not app.exception
    assert app.session_state['conversation_id'] == 'saved-one'
    assert [turn['role'] for turn in app.session_state['conversation']] == ['user', 'assistant']
    assert app.session_state['facts'][0]['fact_id'] == 'prepared'

    next(button for button in app.button if button.label == '新建对话').click().run(timeout=15)
    assert not app.exception
    assert app.session_state['conversation_id'] != 'saved-one'
    assert app.session_state['conversation'] == []
    assert app.session_state['facts'][0]['fact_id'] == 'prepared'


def test_new_conversation_keeps_user_prepared_materials():
    app = AppTest.from_string(SETUP + """
st.session_state.facts = [{'fact_id':'prepared','field':'news','entity':'公司','value':'事先查好的资料'}]
st.session_state.conversation = [{'role':'user','content':'旧问题'}]
ui.new_conversation()
""").run()
    assert not app.exception
    assert app.session_state['facts'][0]['fact_id'] == 'prepared'
    assert not app.session_state['conversation']


def test_remove_selected_material_keeps_other_materials():
    app = AppTest.from_string(SETUP + """
if 'prepared' not in st.session_state:
    st.session_state.prepared = True
    st.session_state.facts = [
        {'fact_id':'one','field':'news','entity':'甲公司','value':'资料甲'},
        {'fact_id':'two','field':'news','entity':'乙公司','value':'资料乙'}]
ui.render_materials_body('http://localhost')
""").run(timeout=15)
    app.multiselect[0].set_value(['one'])
    app.run(timeout=15)
    next(button for button in app.button if button.label == '移除所选资料').click().run(timeout=15)
    assert not app.exception
    assert [fact['fact_id'] for fact in app.session_state['facts']] == ['two']


def test_question_page_no_longer_embeds_manual_materials_panel():
    """主页直接提问并自动取数，不再要求用户先手工查询或整理资料。"""
    home = AppTest.from_string(SETUP + """
st.session_state.facts = [{'fact_id': 'f1', 'field': 'news', 'entity': '甲公司', 'value': '资料甲'}]
ui.page_home('http://localhost')
""").run(timeout=15)
    assert not home.exception
    assert not any(button.label == '查询并加入资料' for button in home.button)
    assert not any(field.label == '股票、基金或查询条件' for field in home.text_input)
    assert not any(field.label == '在资料里查找' for field in home.text_input)
    assert not any(tab.label in {'查询资料', '补充资料'} for tab in home.tabs)
    assert '查找资料' not in NAVIGATION
    assert NAVIGATION[0] == '投资问答'


def test_ask_controls_remain_after_materials_panel_is_removed():
    """移除手工备料面板后，研究方向和自由提问入口仍应保留。"""
    home = AppTest.from_string(SETUP + """
st.session_state.facts = [{'fact_id': 'f1', 'field': 'news', 'entity': '甲公司', 'value': '资料甲'}]
ui.page_home('http://localhost')
""").run(timeout=15)
    assert not home.exception
    assert any(item.label == '研究方向' for item in home.segmented_control)
    assert not any('研究资料' in item.label for item in home.expander)
    assert len(home.chat_input) == 1



def test_explicit_risk_conclusion_is_rendered_without_raw_status():
    script = SETUP + """
advice = {
    'conclusion': '可核验资料已整理。',
    'risk_conclusion': '持仓超过已确认的单标的比例上限，存在集中度风险。',
    'compliance': {'status': 'REVIEW'},
}
ui.render_conclusion_panel(advice)
"""
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    rendered = "\\n".join(item.value for item in app.markdown)
    assert '**风险结论**' in rendered
    assert '存在集中度风险' in rendered


def test_risk_conclusion_survives_history_compaction():
    from backend.app.services.history import summarise_advice

    advice = {
        "conclusion": "研究结论",
        "risk_conclusion": "资料待核对，暂不能形成确定判断。",
    }
    assert summarise_advice(advice)["risk_conclusion"] == advice["risk_conclusion"]


def test_streamed_analysis_waits_for_final_result_before_returning_advice():
    script = SETUP + """
import json
import httpx

def handler(request):
    assert request.url.path == '/portfolio/analyze/stream'
    events = [
        {'type': 'progress', 'stage': '查找资料'},
        {'type': 'progress', 'stage': '事实核验'},
        {'type': 'result', 'advice': {'conclusion': '已完成核验', 'compliance': {'status': 'REVIEW'}}},
    ]
    body = ''.join(json.dumps(item, ensure_ascii=False) + chr(10) for item in events)
    return httpx.Response(200, text=body, headers={'content-type': 'application/x-ndjson'})

client = httpx.Client(transport=httpx.MockTransport(handler))
with patch.object(ui, 'backend_http_client', return_value=client):
    st.session_state.stream_result = ui.stream_analysis('http://localhost', {'query': '研究问题'})
client.close()
"""
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    assert app.session_state['stream_result']['conclusion'] == '已完成核验'
    assert any('事实核验' in item.value for item in app.markdown)


def test_history_page_searches_and_renames_selected_conversation():
    script = SETUP + """
def fake_api(base, method, path, payload=None, **kwargs):
    if path.startswith('/history?'):
        st.session_state.history_list_path = path
        return [{'id': 'saved', 'title': '原名称', 'message_count': 2, 'updated_at': '2026-09-10T09:00:00Z'}]
    if method == 'PATCH':
        st.session_state.renamed_payload = payload
        return {'id': 'saved', 'title': payload['title']}
    if path == '/history/saved':
        return {'id': 'saved', 'title': '原名称', 'messages': []}
    return None
with patch.object(ui, 'api_request', side_effect=fake_api):
    ui.page_conversations('http://localhost')
"""
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    next(field for field in app.text_input if field.label == '搜索对话名称').set_value('基金').run(timeout=15)
    assert 'q=%E5%9F%BA%E9%87%91' in app.session_state['history_list_path']
    next(field for field in app.text_input if field.label == '对话名称').set_value('新的名称')
    next(button for button in app.button if button.label == '保存名称').click().run(timeout=15)
    assert not app.exception
    assert app.session_state['renamed_payload'] == {'title': '新的名称'}
