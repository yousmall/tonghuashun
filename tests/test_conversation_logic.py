"""Conversation-only summaries and a single cumulative, isolated logic chain."""
from copy import deepcopy
import json

import pytest
from streamlit.testing.v1 import AppTest

from frontend.conversation_logic import CHAIN_TITLE, chain_key, conversation_rounds


def advice(name='第一轮结论', **patch):
    return {'conclusion': name, 'risk_conclusion': '注意价格波动与本金亏损风险。',
            'compliance': {'status': 'PASS', 'risk_notice': '不保证收益。'},
            'facts': [], 'evidence': [], 'next_steps': ['下次研究现金流'],
            'scope_notice': '范围说明放入逻辑链', 'confidence': .8, **patch}


def messages():
    return [{'role': 'user', 'content': '第一轮问题', 'created_at': '2026-11-17T08:00:00Z'},
            {'role': 'assistant', 'content': '回答', 'payload': advice()}]


def run(script):
    app = AppTest.from_string(script).run(timeout=20)
    assert not app.exception, app.exception
    return app


@pytest.mark.parametrize('compact,mode,status', [
    (False, 'deep', 'PASS'), (True, 'deep', 'REVIEW'),
    (False, 'auto', 'PASS'), (False, 'instant', 'BLOCK'),
])
def test_all_reply_modes_have_only_two_conclusion_sections(compact, mode, status):
    payload = advice(research_mode=mode, response_scope='clarification',
                     compliance={'status': status, 'risk_notice': '不保证收益。'})
    original = deepcopy(payload)
    app = run('from frontend.result_views import render_advice\n'
              f'render_advice({payload!r}, compact={compact})\n')
    assert [m.value for m in app.markdown if m.value.startswith('**')] == ['**分析结论**', '**风险结论**']
    assert not app.expander and not app.dataframe and not app.metric
    assert not app.caption and not app.get('download_button') and not app.get('vega_lite_chart')
    text = '\n'.join(m.value for m in app.markdown)
    assert '价格波动' in text and '不保证收益' not in text
    assert '范围说明' not in text and '下次研究现金流' not in text
    if status == 'BLOCK':
        assert '风险检查未通过' in text
    assert payload == original


def test_rounds_retain_each_questions_payload_and_update_preview_in_place():
    data = messages()
    data[1]['phase'] = 'preview'
    data += [{'role': 'assistant', 'payload': advice('完整的第一轮'), 'phase': 'complete'},
             {'role': 'user', 'content': '第二轮问题', 'created_at': '2026-11-17T08:01:00Z'},
             {'role': 'assistant', 'payload': advice('第二轮结论')}]
    rounds = conversation_rounds(data)
    assert len(rounds) == 2
    assert rounds[0]['advice']['conclusion'] == '完整的第一轮'
    assert rounds[1]['advice']['conclusion'] == '第二轮结论'
    assert rounds[0]['id'] != rounds[1]['id']
    assert conversation_rounds(data[:2])[0]['id'] == rounds[0]['id']


def chain_script():
    return ('import streamlit as st\nfrom frontend.conversation_logic import render_conversation_logic_chain\n'
            f'st.session_state.setdefault("messages", {messages()!r})\n'
            "st.session_state.setdefault('cid', 'conversation-one')\n"
            "render_conversation_logic_chain(st.session_state.messages, conversation_id=st.session_state.cid, expanded=True)\n")


def test_followups_reuse_one_chain_and_can_revisit_prior_rounds():
    app = run(chain_script())
    key = chain_key('conversation-one')
    assert len([e for e in [*app.expander, *app.status] if e.label == CHAIN_TITLE]) == 1
    assert key in app.session_state
    first = conversation_rounds(messages())[0]['id']
    updated = messages() + [{'role': 'user', 'content': '第二轮问题'},
                            {'role': 'assistant', 'payload': advice('第二轮结论')}]
    app.session_state.messages = updated
    app.run(timeout=20)
    assert not app.exception
    assert len([e for e in [*app.expander, *app.status] if e.label == CHAIN_TITLE]) == 1
    assert len(app.selectbox[0].options) == 2
    assert app.selectbox[0].value == conversation_rounds(updated)[1]['id']
    app.selectbox[0].set_value(first).run(timeout=20)
    assert not app.exception and app.selectbox[0].value == first
    assert '第一轮问题' in '\n'.join(m.value for m in app.markdown)


def test_new_conversation_starts_empty_and_restoring_old_one_restores_its_chain():
    app = run(chain_script())
    app.session_state.cid = 'conversation-two'
    app.session_state.messages = []
    app.run(timeout=20)
    assert not app.exception
    assert chain_key('conversation-two') in app.session_state
    assert not app.selectbox
    text = '\n'.join(m.value for m in app.markdown)
    assert '第一轮问题' not in text and '第一轮结论' not in text
    assert any('尚未发起咨询' in item.value for item in app.info)
    app.session_state.cid = 'conversation-one'
    app.session_state.messages = messages()
    app.run(timeout=20)
    assert not app.exception and len(app.selectbox[0].options) == 1
    assert '第一轮问题' in '\n'.join(m.value for m in app.markdown)


def test_pending_question_is_added_to_existing_chain_before_answer_arrives():
    app = run(chain_script())
    app.session_state.messages = messages() + [{'role': 'user', 'content': '待完成的第二轮'}]
    app.run(timeout=20)
    assert not app.exception and len(app.selectbox[0].options) == 2
    assert any('等待分析结果' in item.value for item in app.info)
    assert '第一轮结论' not in '\n'.join(m.value for m in app.markdown)


def test_chat_bubbles_stay_simple_while_chain_holds_details_and_export():
    data = messages()
    round_id = conversation_rounds(data)[0]['id']
    key = chain_key('chat-layout')
    script = ('import streamlit as st\nfrom frontend.result_views import render_advice\n'
              'from frontend.conversation_logic import render_conversation_logic_chain\n'
              f'messages = {data!r}\n'
              f"st.session_state['{key}_{round_id}_details'] = True\n"
              "render_conversation_logic_chain(messages, conversation_id='chat-layout', expanded=True)\n"
              "with st.chat_message('assistant'):\n    render_advice(messages[1]['payload'])\n")
    app = run(script)
    assert len([e for e in [*app.expander, *app.status] if e.label == CHAIN_TITLE]) == 1
    assert len(app.get('download_button')) == 1
    bubble = app.get('chat_message')[0]
    assert not bubble.expander and not bubble.dataframe and not bubble.caption
    assert [m.value for m in bubble.markdown if m.value.startswith('**')] == ['**分析结论**', '**风险结论**']
    assert any('范围说明放入逻辑链' in item.value for item in app.caption)


def test_research_page_renders_only_one_chain_outside_the_chat_bubbles():
    script = ('from unittest.mock import patch\nimport streamlit as st\nfrom frontend import streamlit_app as ui\n'
              'ui.init_session()\nst.session_state.profile.update(confirmed=True, risk_level="R3")\n'
              f'st.session_state.conversation = {messages()!r}\n'
              "with patch.object(ui, 'render_page_header'), patch.object(ui, 'render_research_guide'), \\\n"
              "     patch.object(ui, 'render_research_board'), patch.object(ui, 'render_prepared_stock_report'), \\\n"
              "     patch.object(ui, 'restore_board_scroll'):\n"
              "    ui.render_research_page('http://offline', '个股研究')\n")
    app = run(script)
    assert len([e for e in [*app.expander, *app.status] if e.label == CHAIN_TITLE]) == 1
    assistants = [b for b in app.get('chat_message') if b.name == 'assistant']
    assert assistants
    assert all(not b.expander and not b.status and not b.dataframe and not b.caption for b in assistants)


def test_panel_open_state_survives_widget_cleanup_and_explicit_close():
    script="""
import streamlit as st
from frontend.persistent_panel import persistent_expander,_remember_open
if st.session_state.get('hide_once'):
    st.session_state.hide_once=False
    st.stop()
def hide():st.session_state.hide_once=True
def close():
    st.session_state['conversation_logic_panel']=False
    _remember_open('conversation_logic_panel','conversation_logic_panel_open_state')
st.button('Hide panel once',on_click=hide)
st.button('Close panel',on_click=close)
def open_panel():st.session_state['conversation_logic_panel']=True
st.button('Open panel',on_click=open_panel)
with persistent_expander('panel',key='conversation_logic_panel',expanded=True) as panel:
    if panel.open:st.caption('panel contents')
"""
    app=AppTest.from_string(script).run()
    assert not app.exception and any(c.value=='panel contents' for c in app.caption)
    app.button[0].click().run()
    assert not app.caption
    app.run()
    assert not app.exception and any(c.value=='panel contents' for c in app.caption)
    app.button[1].click().run()
    assert not app.exception and not app.caption
    app.button[2].click().run()
    assert not app.exception and any(c.value=='panel contents' for c in app.caption)
