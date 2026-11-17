"""One investment logic chain per conversation, retaining each question's evidence."""
from __future__ import annotations

from hashlib import sha256
from typing import Any, Callable

import streamlit as st
from frontend.persistent_panel import persistent_expander

CHAIN_TITLE = '投资逻辑链 · 逐级展开与数据溯源'


def conversation_rounds(messages: list[dict]) -> list[dict[str, Any]]:
    rounds, occurrences = [], {}
    for message in messages:
        if message.get('role') == 'user':
            identity = f"{message.get('id') or ''}|{message.get('created_at') or ''}|{message.get('content') or ''}"
            occurrences[identity] = occurrences.get(identity, 0) + 1
            key = sha256(f'{identity}|{occurrences[identity]}'.encode()).hexdigest()[:16]
            rounds.append({'id': key, 'question': message.get('content') or '未记录问题',
                           'advice': None, 'phase': None, 'content': ''})
        elif message.get('role') == 'assistant':
            if not rounds:
                rounds.append({'id': 'earlier-answer', 'question': '历史咨询', 'advice': None,
                               'phase': None, 'content': ''})
            current = rounds[-1]
            if isinstance(message.get('payload'), dict):
                current['advice'] = message['payload']
            current['phase'] = message.get('phase')
            current['content'] = message.get('content') or ''
    return rounds


def chain_key(conversation_id: str) -> str:
    return 'conversation_logic_' + sha256(str(conversation_id).encode()).hexdigest()[:16]


def render_conversation_logic_chain(messages: list[dict], *, conversation_id: str,
                                    expanded: bool = False,
                                    on_research: Callable[[str], None] | None = None,
                                    api_base: str | None = None, api_request: Callable | None = None) -> None:
    """A stable container and round selector; never borrow another conversation."""
    from frontend.result_views import render_advice_details
    from frontend.logic_drilldown import render_logic_drilldown

    rounds = conversation_rounds(messages)
    key = chain_key(conversation_id)
    with persistent_expander(CHAIN_TITLE, key=key, expanded=expanded,
                     icon=':material/account_tree:') as panel:
        if not panel.open:
            return
        st.caption('本对话共用一个投资逻辑链；每次提问追加一轮依据，新建对话后重新开始。')
        if api_base and api_request:
            from frontend.rag_view import render_rag_manager
            render_rag_manager(api_base, api_request, key=key)
        if not rounds:
            st.info('本对话尚未发起咨询。提问后，分析过程、引用资料、图表和完整报告会保存在这里。')
            return
        by_id = {item['id']: item for item in rounds}
        ids = list(by_id)
        selected_key = f'{key}_round'
        seen_key = f'{key}_latest'
        if st.session_state.get(seen_key) != ids[-1] or st.session_state.get(selected_key) not in by_id:
            st.session_state[selected_key] = ids[-1]
        st.session_state[seen_key] = ids[-1]
        numbering = {item['id']: index for index, item in enumerate(rounds, 1)}
        selected = st.selectbox('查看本对话的咨询轮次', ids, key=selected_key,
                                format_func=lambda value: f"第{numbering[value]}轮 · {by_id[value]['question'][:90]}")
        if selected not in by_id:
            return
        item = by_id[selected]
        st.markdown('**本轮问题**')
        st.write(item['question'])
        advice = item['advice']
        if advice is None:
            st.info(item['content'] or '本轮问题已加入逻辑链，等待分析结果。')
            return
        if item.get('phase') == 'preview':
            st.caption('已有资料概要；详细分析完成后会更新本轮依据。')
        elif item.get('phase') == 'preview_failed':
            st.caption('详细分析未完成，本轮目前保留已有资料概要。')
        if (on_research is not None and selected == ids[-1]
                and advice.get('response_scope') == 'clarification'
                and advice.get('intent') == 'security_research'
                and advice.get('research_mode') in {'instant', 'lite'}):
            if st.button('补查资料并分析', key=f'{key}_research_{selected}', type='primary'):
                on_research(item['question'])
                st.rerun()
        render_logic_drilldown(advice, key=f'{key}_{selected}_drill')
        with persistent_expander('完整分析、图表与报告', key=f'{key}_{selected}_details') as details:
            if details.open:
                render_advice_details(advice, export_key=f'{key}_{selected}', question=item['question'])


def render_single_answer_chain(advice: dict, *, question: str, key: str) -> None:
    render_conversation_logic_chain(
        [{'role': 'user', 'content': question}, {'role': 'assistant', 'payload': advice}],
        conversation_id=f'single_{key}',
    )
