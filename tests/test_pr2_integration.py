"""PR #2与现有基金、自选、账号及研究上下文的集成边界。"""
from datetime import timedelta
from io import BytesIO
from time import perf_counter

import pytest
from PIL import Image
from streamlit.testing.v1 import AppTest

from backend.app.risk_questionnaire import local_today
from backend.app.services.context_link import carry_context_entity
from backend.app.services.local_consultation import LocalConsultationService, select_scope
from backend.app.services.index_board import fetch_index_board
from backend.app.services.provider_errors import ProviderCallError
from frontend.hot_sectors import describe, parse_sector_facts, volume_label
from frontend.risk_assessment import PERSONA_FILES, _persona_bytes, result_html
from tests.test_context_link import FACTS, fact, request, turn


@pytest.mark.parametrize("query",["000001的市净率呢","比亚迪的市净率呢","比较000858与600519","1600519的市净率呢","市场现在怎么样"])
def test_new_or_other_subject_cannot_be_replaced_by_old_stock(query):
    req = request(query,[turn('user','个股研究：600519的市盈率')])
    assert carry_context_entity(req,FACTS) is None


@pytest.mark.parametrize("previous",["比较600519与000858","000001的市净率","市场解读：市场概览"])
def test_ambiguous_new_or_cross_direction_turn_stops_history_fallback(previous):
    req = request("它的市净率呢",[turn('user','个股研究：600519的市盈率'),turn('user',previous)])
    assert carry_context_entity(req,FACTS) is None


def test_numeric_identity_does_not_match_inside_longer_code():
    req = request("它的市净率呢",[turn('user','个股研究：1600519的市盈率')])
    assert carry_context_entity(req,FACTS) is None


def test_fund_prefix_and_same_domain_terms_do_not_disable_follow_up():
    facts = [fact('fund','测试基金','fee_rate',.15,code='510300.OF')]
    req = request('基金筛选：这只基金的管理费率呢',[turn('user','基金筛选：510300的跟踪误差')])
    req = req.model_copy(update={'research_direction':'基金筛选','facts':facts})
    assert carry_context_entity(req,facts)=='测试基金'


@pytest.mark.asyncio
async def test_follow_up_service_uses_only_carried_stock_and_keeps_unknown_subject_missing():
    contexts = [turn('user','个股研究：600519的市盈率')]
    req = request('个股研究：它的市净率呢',contexts)
    start = perf_counter()
    prepared,_,advice = await LocalConsultationService(detailed=True).run(req,select_scope(req))
    assert perf_counter()-start < 3
    assert '贵州茅台' in advice.conclusion and advice.evidence
    assert {f.entity for f in prepared.facts} == {'贵州茅台'}
    other = req.model_copy(update={'query':'个股研究：000001的市净率呢'})
    _,_,missing = await LocalConsultationService(detailed=True).run(other,select_scope(other))
    assert not missing.evidence and missing.response_scope=='clarification'


def sector(field,value,*,unit=None,day=None,code='881121.TI'):
    return {'entity':'测试行业','entity_code':code,'field':field,'value':value,'unit':unit,
            'period':day or local_today().isoformat()}


def test_sector_units_identity_dates_and_non_finite_values_are_checked():
    today = local_today()
    facts = [sector('change',.025,unit='ratio'),sector('turnover_rate',.07,unit='ratio'),
             sector('turnover_value',2,unit='亿元'),
             sector('turnover_rate',99,unit='percent',day=(today-timedelta(days=1)).isoformat())]
    row = parse_sector_facts(facts)[0]
    assert row['code']=='881121.TI' and row['change']==2.5 and row['rate']==pytest.approx(7)
    assert row['turnover']==2e8 and row['date']==today.isoformat()
    assert describe(row)[:2]==('强势','高换手')
    assert '不能据此判断放量' in describe(row)[2]
    assert parse_sector_facts([sector('change',float('nan'),unit='percent')])==[]
    assert parse_sector_facts([sector('change',2,unit='percent',day=(today+timedelta(days=1)).isoformat())])==[]
    assert volume_label(None)=='数据不足'


def test_sector_conflicts_and_mixed_dates_are_not_hidden_or_counted_as_active():
    facts = [sector('change',2,unit='percent'),sector('change',3,unit='percent')]
    assert parse_sector_facts(facts)==[]
    facts = [sector('change',2,unit='percent'),sector('turnover_rate',7,unit='percent',day='2000-01-01')]
    row = parse_sector_facts(facts)[0]
    assert row['rate'] is None and volume_label(row['rate'])=='数据不足'


def test_hot_sector_panel_does_not_wait_for_provider():
    script = '''
import streamlit as st
from unittest.mock import patch
from concurrent.futures import Future
from frontend.hot_sectors import render_hot_sectors
st.session_state.setdefault('pending',Future())
st.session_state.setdefault('loads',0)
class Executor:
    def submit(self,fn,*args):
        st.session_state.loads += 1
        return st.session_state.pending
def factory(*args):
    return lambda refresh=False: None
with patch('frontend.snapshots.board_prefetch_executor',return_value=Executor()):
    render_hot_sectors('test-only',lambda *args: None,factory=factory)
st.text_input('可继续提问')
'''
    app = AppTest.from_string(script).run(timeout=10)
    assert not app.exception and app.text_input[0].label=='可继续提问'
    assert app.session_state['loads']==1
    assert not any('未返回热门板块' in c.value for c in app.caption)
    app.session_state['pending'].set_result({'status':'ok','facts':[sector('change',2,unit='percent')]})
    app.run(timeout=10)
    assert not app.exception and app.session_state['loads']==1
    assert any('测试行业' in item.value for item in app.markdown)


def test_preset_click_removes_only_that_question_and_new_conversation_restores_it(monkeypatch):
    import streamlit as st
    from frontend import streamlit_app as ui
    def submit(base,direction,query):
        st.session_state.sent=query
    monkeypatch.setattr(ui,'submit_chat_analysis',submit)
    script = '''
import streamlit as st
from frontend import streamlit_app as ui
ui.init_session()
st.session_state.profile['confirmed']=True
ui.render_preset_questions('test-only','市场解读')
'''
    app = AppTest.from_string(script).run(timeout=15)
    original = list(app.pills[0].options)
    app.pills[0].set_value(original[0]).run(timeout=15)
    assert not app.exception and original[0] in app.session_state['sent']
    assert len(app.pills[0].options)==2
    app.session_state['conversation_id']='new-conversation'
    app.run(timeout=15)
    assert len(app.pills[0].options)==3


def test_logout_clears_incoming_feature_account_state():
    script = '''
import streamlit as st
from frontend import streamlit_app as ui
ui.init_session()
st.session_state.hot_sector_cache={'old-account':{}}
st.session_state.preset_asked={'old-account':['query']}
st.session_state.preset_question_市场解读='query'
ui.reset_user_session()
st.write('ok')
'''
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    assert all(key not in app.session_state for key in ['hot_sector_cache','preset_asked','preset_question_市场解读'])


@pytest.mark.parametrize('band',[1,2,3,4,5])
def test_all_five_risk_personas_remain_valid_images_and_match_level(band):
    payload = _persona_bytes(PERSONA_FILES[band])
    with Image.open(BytesIO(payload)) as image:
        assert image.format=='JPEG' and min(image.size)>100
        image.verify()
    html = result_html({'investor_category':f'C{band}'})
    assert 'risk-persona' in html and f'C{band}' in html


@pytest.mark.asyncio
async def test_index_failure_reason_is_specific_and_does_not_leak_raw_exception():
    class Provider:
        async def query(self,*args,**kwargs):
            raise ProviderCallError('private-example',code='AUTHENTICATION_REJECTED')
        async def get_price_history(self,name,*args,**kwargs):
            if name=='上证指数':
                raise ProviderCallError('private-example',code='PROVIDER_QUOTA_EXHAUSTED')
            if name=='深证成指':
                raise TimeoutError('private-example')
            return []
    result = await fetch_index_board(Provider())
    entries = {i.name:i for i in result.items}
    assert '额度' in entries['上证指数'].message
    assert '超时' in entries['深证成指'].message
    assert '未返回' in entries['创业板指'].message
    assert 'private-example' not in result.model_dump_json()
    assert any('密钥无效' in note for note in result.notes)
