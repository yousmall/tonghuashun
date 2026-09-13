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
    assert app.radio[0].options == ['投资问答', '自选研究', '持仓分析', '历史记录', '投资偏好']
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


def test_logic_chain_and_source_trace_keep_view_to_fact_relationship():
    advice = {
        'evidence': ['price', 'news'],
        'facts': [
            {'fact_id': 'price', 'entity': '测试公司', 'field': 'close_price', 'value': 10,
             'snapshot_time': '2026-09-11T01:00:00Z', 'source_id': 'IWENCAI_SKILLHUB'},
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
    assert stages[1]['value'] == '1/2 完成'
    rows = source_trace_rows(advice)
    assert len(rows) == 2
    assert rows[0]['支持分析'] == '个股研究'
    assert rows[1]['支持分析'] == '个股研究、行业分析'
    assert rows[0]['来源'] == '同花顺问财'
    assert all('fact_id' not in row for row in rows)
    assert all('不相关资料' not in str(row) for row in rows)


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
ui.render_materials_body('http://localhost')
""").run(timeout=15)
    app.multiselect[0].set_value(['one'])
    app.run(timeout=15)
    next(button for button in app.button if button.label == '移除所选资料').click().run(timeout=15)
    assert not app.exception
    assert [fact['fact_id'] for fact in app.session_state['facts']] == ['two']


def test_question_page_embeds_materials_panel():
    """原“查找资料”页已并入问答页：取数、补充和整理都在提问的同一页完成。"""
    home = AppTest.from_string(SETUP + """
st.session_state.facts = [{'fact_id': 'f1', 'field': 'news', 'entity': '甲公司', 'value': '资料甲'}]
ui.page_home('http://localhost')
""").run(timeout=15)
    assert not home.exception
    # 备料控件现在就在问答页内，不必先跳到另一个入口再跳回来。
    assert any(button.label == '查询并加入资料' for button in home.button)
    assert any(field.label == '股票、基金或查询条件' for field in home.text_input)
    assert any(field.label == '在资料里查找' for field in home.text_input)
    assert [tab.label for tab in home.tabs] == ['查询资料', '补充资料', '已有资料（1）']
    # 资料概况仍要一眼可见，否则用户不知道提问会参考什么。
    assert any('1 条研究资料' in item.value for item in home.caption)
    # 合并后不再保留独立入口，避免同一功能出现两条路径。
    assert '查找资料' not in NAVIGATION
    assert NAVIGATION[0] == '投资问答'


def test_materials_panel_sits_above_ask_controls():
    """备料面板排在提问控件之前：底部只留输入框和法律声明，不再堆第三块内容。

    ``st.chat_input`` 会被 Streamlit 收进独立的底部容器，因此它不出现在
    ``main.children`` 里；这里断言的是主内容区的相对顺序。
    """
    home = AppTest.from_string(SETUP + """
st.session_state.facts = [{'fact_id': 'f1', 'field': 'news', 'entity': '甲公司', 'value': '资料甲'}]
ui.page_home('http://localhost')
""").run(timeout=15)
    assert not home.exception
    order = [
        str(getattr(element, "label", None) or getattr(element, "value", None) or "")
        for element in home.main.children.values()
    ]
    materials_at = next(index for index, text in enumerate(order) if '研究资料（1）' in text)
    quick_ask_at = next(index for index, text in enumerate(order) if '按研究方向提问' in text)
    assert materials_at < quick_ask_at
    assert len(home.chat_input) == 1
