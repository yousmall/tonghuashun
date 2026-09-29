"""回答图片、PDF 下载、对比和连续登录的回归。"""
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path

from PIL import Image
import pytest
from streamlit.testing.v1 import AppTest

from frontend.answer_report import answer_images, chart_groups, build_answer_pdf
from frontend.streamlit_app import answer_export_payload


def advice():
    return {'conclusion': '这两个标的各有特点，尚需核实最新资料。', 'risk_conclusion': '价格可能下跌。',
            'compliance': {'status': 'REVIEW', 'risk_notice': '不保证收益。', 'required_disclosures': ['请注意信息时效。']},
            'risks': ['风险甲', '风险乙'], 'next_steps': ['核对最新公告'], 'data_acquisition': {'mode': 'unavailable'},
            'evidence': ['a', 'b'], 'facts': [
                {'fact_id': 'a', 'entity': '公司甲', 'field': 'change', 'value': 2, 'period': '2026-09-24', 'snapshot_time': '2026-09-24T08:00:00Z', 'source_id': 'TEST'},
                {'fact_id': 'b', 'entity': '公司乙', 'field': 'change', 'value': -1, 'period': '2026-09-24', 'snapshot_time': '2026-09-24T08:00:00Z', 'source_id': 'TEST'}]}


def test_images_only_chart_cited_numbers_with_matching_dates_and_no_conflicts():
    data = advice()
    assert len(chart_groups(data)) == 1
    data['facts'][1]['period'] = '2026-09-23'
    assert not chart_groups(data)
    data['facts'][1]['period'] = (date.today()+timedelta(days=1)).isoformat()
    assert not chart_groups(data)
    data = advice()
    data['evidence'] = ['a']
    assert not chart_groups(data)
    data = advice()
    data['facts'].append({**data['facts'][0], 'fact_id':'conflict', 'value':10})
    data['evidence'].append('conflict')
    assert not chart_groups(data)
    data['facts'][-1]['value'] = 2
    assert len(chart_groups(data)) == 1


def test_missing_data_still_has_risk_illustration_without_fake_chart():
    data = advice()
    data['facts'], data['evidence'] = [], []
    images = answer_images(data)
    assert len(images) == 1
    assert Image.open(BytesIO(images[0][1])).size == (1080, 240)


def test_pdf_retains_warnings_sources_and_escaped_user_text():
    data = advice()
    data['conclusion'] += '\n' + '完整保留的长篇分析。'*300
    images = answer_images(data)
    report = answer_export_payload(data, images, '请解释 <风险> & 收益')
    text = '\n'.join(value for _, values in report['sections'] for value in values)
    assert all(term in text for term in ['价格可能下跌', '风险甲', '风险乙', '未能取得最新市场数据', '信息时效'])
    assert len(report['sources']) == 2
    assert report['sources'][0]['报告期'] == '2026-09-24'
    exported = build_answer_pdf(report)
    assert exported.read(5) == b'%PDF-'
    assert len(exported.getvalue()) > 10000


def test_every_chat_answer_has_distinct_pdf_download_and_images():
    import json
    script = 'import json\nreport=json.loads(' + repr(json.dumps(advice())) + ')\n' + """
import streamlit as st
from unittest.mock import patch
from frontend import streamlit_app as ui
ui.init_session()
st.session_state.auth_token = 'test-only'
st.session_state.profile['confirmed'] = True
st.session_state.conversation = [
    {'role':'user','content':'问题甲'},
    {'role':'assistant','content':'结果','payload':report},
    {'role':'user','content':'问题乙'},
    {'role':'assistant','content':'结果','payload':report}]
with patch.object(ui,'api_request',return_value=None):
    ui.page_questions('http://test-only')
"""
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    assert len(app.get('download_button')) == 2
    assert not app.get('image')
    assert len(app.get('vega_lite_chart')) == 1


WATCHLIST_SETUP = """
import streamlit as st
from unittest.mock import patch
from frontend import streamlit_app as ui
ui.init_session()
st.session_state.auth_token='test-only'
st.session_state.profile['confirmed']=True
st.session_state.setdefault('watchlist', [])
if not st.session_state.watchlist:
    st.session_state.watchlist=[{'id':1,'target':'公司甲','asset_type':'股票'},
                               {'id':2,'target':'公司乙','asset_type':'股票'},
                               {'id':3,'target':'基金甲','asset_type':'基金'}]
def analysis(base,query):
    st.session_state.comparison_query=query
    return {'conclusion':'对比结果'}
with patch.object(ui,'run_analysis',side_effect=analysis), patch.object(ui,'api_request',return_value=None):
    ui.page_watchlist('http://test-only')
"""


def test_mixed_comparison_is_explained_without_stock_analysis():
    app = AppTest.from_string(WATCHLIST_SETUP).run(timeout=15)
    app.multiselect[0].set_value([1,3]).run(timeout=15)
    next(button for button in app.button if button.label=='生成对比研究').click().run(timeout=15)
    assert not app.exception
    assert any('相同类型' in warning.value for warning in app.warning)
    assert 'comparison_query' not in app.session_state


def test_same_type_comparison_requests_matrix_and_deleted_selections_are_removed():
    app = AppTest.from_string(WATCHLIST_SETUP).run(timeout=15)
    app.multiselect[0].set_value([1,2]).run(timeout=15)
    next(button for button in app.button if button.label=='生成对比研究').click().run(timeout=15)
    assert not app.exception
    assert app.session_state['watchlist_comparison_request']['targets'] == ['公司甲','公司乙']
    assert 'comparison_query' not in app.session_state  # 先核对资料，用户再选择解读。
    app.session_state['watchlist']=[{'id':1,'target':'公司甲','asset_type':'股票'},
                                  {'id':3,'target':'基金甲','asset_type':'基金'}]
    app.run(timeout=15)
    assert not app.exception
    assert app.multiselect[0].value == [1]
    assert 'watchlist_comparison_request' not in app.session_state


def test_repeated_login_logout_keeps_one_login_form_and_clears_stale_service_state():
    script = """
import streamlit as st
from unittest.mock import patch
from frontend import streamlit_app as ui

def request(base,method,path,payload=None,**kwargs):
    if path=='/auth/login':
        return {'access_token':'test-only','session_beacon_token':'test-only','user':{'username':'test-user','role':'user'}}
    return None
with patch.object(ui,'api_request',side_effect=request), patch.object(ui,'prefetch_research_board_data'), patch.object(ui,'register_browser_session'), patch.object(ui,'enforce_session_timeout'):
    ui.main()
"""
    app = AppTest.from_string(script).run(timeout=15)
    for _ in range(3):
        assert not app.exception
        assert len(app.get('form')) == 1  # the registration form is created only when its tab opens
        assert len([button for button in app.button if button.label=='登录']) == 1
        app.session_state['service_unavailable']=True
        app.session_state['pending_navigation']='投资问答'
        app.text_input(key='login_username').input('test-user')
        app.text_input(key='login_password').input('test-password')
        next(button for button in app.button if button.label=='登录').click().run(timeout=15)
        assert not app.exception
        assert app.session_state['navigation']=='主页'
        assert not app.get('form')
        assert not any(button.label=='登录' for button in app.button)
        next(button for button in app.button if button.label=='退出登录').click().run(timeout=15)
        assert not app.exception
