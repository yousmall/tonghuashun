"""用户界面回归：信息简化不能隐藏风险或破坏会话交互。"""
from pathlib import Path

from streamlit.testing.v1 import AppTest

from frontend.streamlit_app import display_value, plain_language


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


def test_navigation_and_profile_call_to_action():
    app = AppTest.from_string(SETUP + MAIN).run()
    assert not app.exception
    assert app.radio[0].options == ['投资问答', '查找资料', '持仓分析', '历史记录', '投资偏好']
    assert app.chat_input[0].disabled
    next(button for button in app.button if button.label == '填写投资偏好').click().run(timeout=15)
    assert not app.exception
    assert app.title[0].value == '投资偏好'
    assert '用户标识' not in [field.label for field in app.text_input]


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
with patch.object(ui, 'api_request', return_value=None) as request:
    ui.run_analysis('http://localhost', '新问题')
    st.session_state.sent = request.call_args.args[3]
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
    assert not any(item.value == '别的研究结果' for item in app.markdown)


def test_plain_language_preserves_small_values_and_disagreement():
    assert display_value('fee_rate', .005) == '0.5%'
    result = plain_language('协调器置信加权共识分为 50.0。security：基本面与技术面存在分歧，不能确定上涨。')
    assert '共识' not in result and 'security' not in result
    assert '分歧' in result and '不能确定上涨' in result


def test_history_restores_payload_and_navigates_to_chat():
    script = SETUP + """
st.session_state.setdefault('navigation', '历史记录')
answer = {'compliance': {'status': 'REVIEW'}, 'conclusion': '历史判断仍需核实。'}
def fake_api(base, method, path, payload=None, **kwargs):
    if path == '/history':
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
    ui.page_materials('http://localhost')
"""
    app = AppTest.from_string(script).run(timeout=15)
    app.selectbox[0].select('基金 / ETF')
    next(field for field in app.text_input if field.label == '股票、基金或查询条件').set_value('低费率宽基')
    next(button for button in app.button if button.label == '查询并加入资料').click().run(timeout=15)
    assert not app.exception
    assert app.session_state['sent']['filters'] == {'query': '低费率宽基'}
    assert app.session_state['sent']['kind'] == 'fund'
    assert '一条测试公告' in app.dataframe[0].value.to_string()


def test_manual_material_preserves_percent_source_and_date():
    app = AppTest.from_string(SETUP + "ui.page_materials('http://localhost')").run(timeout=15)
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
ui.page_materials('http://localhost')
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
def fake_api(base, method, path, payload=None):
    st.session_state.sent_query = payload['query']
    return {'trace_id': 'research-1', 'conclusion': '个股专属结果', 'compliance': {'status': 'PASS'}}
with patch.object(ui, 'api_request', side_effect=fake_api):
    ui.render_quick_ask('http://localhost')
"""
    app = AppTest.from_string(script).run(timeout=15)
    app.segmented_control[0].select('个股研究').run(timeout=15)
    next(field for field in app.text_input if field.label == '关注的对象或想了解的问题').set_value('分析测试股票')
    next(button for button in app.button if button.label == '开始分析').click().run(timeout=15)
    assert not app.exception
    assert app.session_state['sent_query'] == '个股研究：分析测试股票'


def test_details_uses_selected_result_evidence_not_current_materials():
    script = SETUP + """
st.session_state.facts = [{'fact_id':'e1','entity':'其他对象','field':'news','value':'不应串入'}]
st.session_state.advice = {'trace_id':'a1', 'evidence':['e1'], 'compliance':{'status':'REVIEW'},
    'facts':[{'fact_id':'e1','entity':'原对象','field':'news','value':'原始资料'},
             {'fact_id':'e2','entity':'未引用对象','field':'news','value':'未引用资料'}],
    'agent_results':[{'agent_id':'security','opinion':'保留正反两种观点','status':'degraded'}],
    'task_plan':{'nodes':[{'agent_id':'security','status':'completed'}]}}
ui.render_analysis_details()
"""
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    assert [tab.label for tab in app.tabs] == ['分析观点','数据依据','完成情况']
    assert '原始资料' in app.dataframe[0].value.to_string()
    assert '不应串入' not in app.dataframe[0].value.to_string()
    assert any('未被这次分析引用' in item.label for item in app.expander)
    assert not app.metric and not app.code


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
ui.page_materials('http://localhost')
""").run(timeout=15)
    app.multiselect[0].set_value(['one'])
    app.run(timeout=15)
    next(button for button in app.button if button.label == '移除所选资料').click().run(timeout=15)
    assert not app.exception
    assert [fact['fact_id'] for fact in app.session_state['facts']] == ['two']


def test_question_page_and_materials_page_are_separate():
    """问答页只负责提问；取数、补充和整理资料只在“查找资料”页出现。"""
    home = AppTest.from_string(SETUP + """
st.session_state.facts = [{'fact_id': 'f1', 'field': 'news', 'entity': '甲公司', 'value': '资料甲'}]
ui.page_home('http://localhost')
""").run(timeout=15)
    assert not home.exception
    assert not any(field.label == '股票、基金或查询条件' for field in home.text_input)
    assert not any(field.label == '在资料里查找' for field in home.text_input)
    assert not any(button.label == '查询并加入资料' for button in home.button)
    # 问答页仍显示资料概况，并给出跳转入口，而不是把资料页塞进来。
    assert any('1 条资料' in item.value for item in home.caption)
    assert any(button.label == '前往查找资料' for button in home.button)

    materials = AppTest.from_string(SETUP + "ui.page_materials('http://localhost')").run(timeout=15)
    assert not materials.exception
    assert materials.title[0].value == '查找资料'
    assert [tab.label for tab in materials.tabs] == ['查询资料', '补充资料', '已有资料']
    assert not materials.chat_input


def test_materials_entry_switches_navigation():
    app = AppTest.from_string(SETUP + """
def fake_api(base, method, path, payload=None, **kwargs):
    return {'id': 1, 'username': 'test-user'} if path == '/auth/me' else None
with patch.object(ui, 'api_request', side_effect=fake_api), \\
        patch.object(ui, 'register_browser_session'), patch.object(ui, 'enforce_session_timeout'):
    ui.main()
""").run(timeout=15)
    next(button for button in app.button if button.label == '前往查找资料').click().run(timeout=15)
    assert not app.exception
    assert app.radio[0].value == '查找资料'
    assert app.title[0].value == '查找资料'
    # 路由后看到的是资料页自己的控件，而不是问答页的输入框。
    assert any(button.label == '查询并加入资料' for button in app.button)
    assert not app.chat_input
