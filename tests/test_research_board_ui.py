"""真实布局回归：数据区与聊天分离、缓存、刷新、独立方向选择。"""
import json
from datetime import datetime, timezone

import pytest
from streamlit.testing.v1 import AppTest


def payload(direction, target=None):
    timestamp = datetime.now(timezone.utc).isoformat()
    def fact(name, code, field, value, period="2026-09-24"):
        return {"fact_id": name+field, "entity": name, "entity_code": code, "field": field,
                "value": value, "period": period, "snapshot_time": timestamp, "source_id": "TEST", "quality": .9}
    rows = []
    for index, name in enumerate(["测试甲", "测试乙"]):
        for field, value in {"close_price": 10+index, "change": 1+index, "market_cap": 1e9, "pe_ttm": 20,
                             "fund_nav": 1.2, "fund_size": 1e9*(index+1), "fee_rate": .5,
                             "conversion_premium_rate": 5+index, "roe": 7.5, "industry": "银行"}.items():
            rows.append(fact(name, "60000"+str(index+1), field, value))
    sections = [{"key": "overview", "title": "概览", "status": "ok", "facts": rows}]
    if direction == "stock":
        sections.append({"key": "detail", "title": "财务指标", "status": "ok", "facts": rows[:10]})
    sections.append({"key": "history", "title": "历史价格", "status": "ok", "facts": [
        fact("测试甲", "600001", "close_price", 10, "2026-09-23"), fact("测试甲", "600001", "close_price", 11)]})
    return {"direction": direction, "target": target, "status": "ok", "fetched_at": timestamp, "sections": sections}


def script(data):
    return """
from unittest.mock import patch
import json
import streamlit as st
from frontend import streamlit_app as ui
from frontend import research_board as board
from concurrent.futures import Future
class ImmediateExecutor:
    def submit(self, function, *args):
        future=Future()
        future.set_result(function(*args))
        return future
ui.init_session()
st.session_state.auth_token='test-only'
st.session_state.auth_user={'username':'test-only'}
st.session_state.profile['confirmed']=True
st.session_state.profile_restored=True
st.session_state.setdefault('navigation','投资问答')
st.session_state.setdefault('board_calls',[])
""" + "data = json.loads(" + repr(json.dumps(data, ensure_ascii=False)) + ")\n" + """
st.session_state.setdefault('research_direction',data['page'])
def fake_fetch(base,direction,target):
    st.session_state.board_calls.append({'direction':direction,'target':target})
    result=data['results'][direction].copy()
    result['target']=target
    return result
with patch.object(board,'board_prefetch_executor',return_value=ImmediateExecutor()), patch.object(ui,'create_board_fetch',return_value=fake_fetch), patch.object(ui,'api_request',return_value=[]), patch.object(ui,'register_browser_session'), patch.object(ui,'enforce_session_timeout'):
    ui.main()
"""


@pytest.mark.parametrize('page,direction',[('市场解读','market'),('行业分析','industry'),('个股研究','stock'),('基金筛选','fund'),('可转债分析','convertible')])
def test_empty_research_page_renders_related_data_without_an_extra_question_input(page,direction):
    data={'page':page,'results':{direction:payload(direction)}}
    app=AppTest.from_string(script(data)).run(timeout=15)
    assert not app.exception
    assert app.metric
    assert app.dataframe
    assert app.get('vega_lite_chart')
    assert len(app.chat_input)==1
    assert not app.text_input and not app.text_area
    assert app.session_state['facts']==[]
    assert app.session_state['conversation']==[]
    assert len(app.session_state['board_calls'])==1


def test_board_cache_refresh_and_direction_selection_are_independent():
    data={'page':'个股研究','results':{d:payload(d) for d in ['stock','market','industry','fund','convertible']}}
    app=AppTest.from_string(script(data)).run(timeout=15)
    assert not app.exception
    app.run(timeout=15)
    assert len(app.session_state['board_calls'])==1
    next(button for button in app.button if button.label=='刷新数据').click().run(timeout=15)
    assert not app.exception
    assert len(app.session_state['board_calls'])==2
    app.selectbox[0].select('600036').run(timeout=15)
    assert not app.exception
    assert app.session_state['board_calls'][-1]=={'direction':'stock','target':'600036'}
    app.segmented_control[0].select('基金筛选').run(timeout=15)
    assert not app.exception
    assert app.session_state['board_calls'][-1]['direction']=='fund'
    app.segmented_control[0].select('个股研究').run(timeout=15)
    assert not app.exception
    assert app.selectbox[0].value=='600036'
    assert app.session_state['facts']==[]


def test_partial_data_keeps_market_news_when_index_data_is_missing():
    result=payload('market')
    result['status']='partial'
    result['sections']=[{'key':'overview','title':'主要指数','status':'unavailable','facts':[]},
                        {'key':'news','title':'市场资讯','status':'ok','facts':[{'entity':'资讯','field':'news','value':'仍然可看的真实结构资讯','period':'REC-test'}]}]
    app=AppTest.from_string(script({'page':'市场解读','results':{'market':result}})).run(timeout=15)
    assert not app.exception
    assert any('仍然可看的真实结构资讯' in item.value for item in app.markdown)
    assert not app.metric
    assert len(app.chat_input)==1
