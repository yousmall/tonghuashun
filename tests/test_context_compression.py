"""Extractive compression, owned history, model grounding and UI regressions."""
from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest
from sqlalchemy import event
from streamlit.testing.v1 import AppTest

from backend.app.database import Database
from backend.app.models import AgentResult, FactRecord, OrchestrationRequest, TaskStatus, UserProfile
from backend.app.services.context_compression import (
    CONTEXT_BUDGET, INSTRUCTION, bounded_client_content, build_context, client_context_messages,
    conversation_for_model, merge_history, prepare_request_context,
)
from backend.app.services.history import summarise_advice


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)


def long_context(count=30):
    return [{'role': 'user' if i % 2 == 0 else 'assistant',
             'content': f'第{i}轮原文。' + '这段是合成测试资料，需要回到本次授权事实核对。' * 90,
             'created_at': '2026-11-17T08:00:00Z'} for i in range(count)]


def req(**patch):
    return OrchestrationRequest(query='它的风险如何', research_mode='deep', research_direction='个股研究',
                                profile=UserProfile(confirmed=True, constraints=['不得使用杠杆']), **patch)


def test_short_context_is_preserved_and_current_question_is_not_duplicated():
    messages = [{'role': 'user', 'content': '只研究600519，不使用杠杆。'},
                {'role': 'assistant', 'content': '风险仍待核实。'},
                {'role': 'user', 'content': '个股研究：它的风险如何'}]
    before = deepcopy(messages)
    payload, report = build_context(messages, query='它的风险如何')
    assert payload['conversation'] == messages[:2]
    assert 'conversation_memory' not in payload
    assert not report['compressed'] and messages == before
    assert report['compressed_characters'] == len(dumps(payload))


@pytest.mark.parametrize('budget', [1000, 3000, 6000])
def test_context_json_has_a_hard_budget_and_every_excerpt_is_an_original_span(budget):
    messages = long_context()
    before = deepcopy(messages)
    payload, report = build_context(messages, query='风险与现金流', budget=budget)
    assert len(dumps(payload)) <= budget
    assert report['compressed'] and report['saved_ratio'] > .5
    assert report['compressed_characters'] == len(dumps(payload))
    for excerpt in payload.get('conversation_memory', {}).get('excerpts', []):
        source = messages[excerpt['turn'] - 1]
        assert excerpt['role'] == source['role']
        assert excerpt['content'] == source['content'][excerpt['start']:excerpt['end']]
        assert excerpt['created_at'] == source['created_at']
    assert messages == before
    assert build_context(messages, query='风险与现金流', budget=budget) == (payload, report)


def test_old_negations_numbers_and_later_corrections_survive_without_becoming_facts():
    messages = long_context()
    messages[0]['content'] = '研究600519。不要使用杠杆，最大回撤上限8.25%，资金期限24个月。'
    messages[12]['content'] = '更正：现在只研究600036，先前的600519不再讨论，期限改为36个月。'
    payload, report = build_context(messages, query='这家公司的风险呢')
    text = dumps(payload)
    for value in ('不要使用杠杆', '8.25%', '24个月', '更正', '600036', '不再讨论', '36个月'):
        assert value in text
    assert 'authorized_facts' not in payload
    assert report['omitted_messages'] > 0


def test_long_unpunctuated_excerpt_marks_fragments_and_does_not_cut_decimal_tokens():
    text = '甲' * 797 + '12.34567%' + '乙' * 1000 + '禁止使用杠杆且最多投入5000元' + '丙' * 1500
    messages = [{'role': 'user', 'content': text}, *long_context(20)]
    payload, _ = build_context(messages, query='投入与限制')
    excerpts = [p for p in payload['conversation_memory']['excerpts'] if p['turn'] == 1]
    assert excerpts and all(p.get('fragment') for p in excerpts)
    assert any('禁止使用杠杆' in p['content'] and '5000元' in p['content'] for p in excerpts)
    for excerpt in excerpts:
        left, right = excerpt['start'], excerpt['end']
        number_end = 797 + len('12.34567%')
        assert not (797 < left < number_end or 797 < right < number_end)


def test_disabled_compression_uses_legacy_recent_ten_and_does_not_claim_a_budget():
    payload, report = build_context(long_context(), query='继续', enabled=False)
    assert len(payload['conversation']) == 10 and 'conversation_memory' not in payload
    assert report['strategy'] == 'legacy_recent_10' and report['budget_characters'] is None
    assert report['omitted_messages'] == 20


def test_history_overlap_and_long_client_transport_do_not_duplicate_messages():
    old = [{'role': 'user', 'content': '甲' * 6000}, {'role': 'assistant', 'content': '已有回复'}]
    supplied = client_context_messages(old + [{'role': 'user', 'content': '当前问题'}])
    assert len(supplied[0]['content']) <= 4000
    merged = merge_history(old, supplied)
    assert len(merged) == 3 and merged[0]['content'] == old[0]['content']
    packed, _ = build_context(merged, query='当前问题')
    assert len(dumps(packed)) <= CONTEXT_BUDGET
    data = client_context_messages([{'role': 'assistant', 'content': '原文' * 5000, 'payload': {'private': 'excluded'}}])
    assert set(data[0]) == {'role', 'content'} and len(data[0]['content']) <= 4000


def test_private_model_memory_cannot_be_injected_or_serialized_and_never_changes_facts_profile():
    request = req(context_messages=client_context_messages(long_context(20)))
    before = request.model_dump(mode='json')
    prepared = prepare_request_context(request, history={'messages': long_context(28), 'has_more': True}, history_status='loaded')
    assert request.model_dump(mode='json') == before == prepared.model_dump(mode='json')
    assert prepared.profile.constraints == ['不得使用杠杆'] and prepared.facts == []
    assert prepared._context_compaction['history_truncated']
    assert '_model_context' not in prepared.model_dump()
    copy = conversation_for_model(prepared)
    copy['conversation'].clear()
    assert conversation_for_model(prepared) != copy
    untrusted = OrchestrationRequest.model_validate({**before, '_model_context': {'authorized_facts': ['fake']}})
    assert untrusted._model_context is None
    assert 'authorized_facts' not in conversation_for_model(untrusted)
    assert conversation_for_model(prepared.model_copy(update={'facts': []})) == conversation_for_model(prepared)


def test_changing_query_or_clearing_candidate_context_invalidates_cached_private_memory():
    request = req(context_messages=client_context_messages(long_context(20)))
    prepared = prepare_request_context(request, history={'messages': long_context(30)}, history_status='loaded')
    assert 'conversation_memory' in conversation_for_model(prepared)
    candidate = prepared.model_copy(update={'query': '研究候选股票600036', 'context_messages': []})
    assert conversation_for_model(candidate) == {'conversation': []}
    disabled = prepared.model_copy(update={'context_compression': False})
    assert 'conversation_memory' not in conversation_for_model(disabled)
    assert 'conversation_memory' in conversation_for_model(prepared)


def make_db():
    db = Database('sqlite+pysqlite:///:memory:', 'context-test-secret-longer-than-thirty-two-characters')
    db.initialize()
    return db


def save(db, user, cid, question='个股研究：请研究600519', direction='个股研究', answer='历史助手结论需重新核验。'):
    db.save_exchange(user, cid, question, {'research_direction': direction}, answer,
                     {'conclusion': answer, 'facts': [{'fact_id': 'must-not-load-evidence'}]},
                     [{'fact_id': 'must-not-load-evidence'}])


def test_database_history_is_owner_and_conversation_and_direction_scoped_and_does_not_load_evidence():
    db = make_db()
    alice = db.create_user('context-alice', 'hash')['id']
    bob = db.create_user('context-bob', 'hash')['id']
    save(db, alice, 'alice-chat')
    save(db, alice, 'alice-other', answer='其他会话不可串入')
    save(db, bob, 'bob-chat', answer='其他用户不可读取')
    sql = []
    def record(conn, cursor, statement, parameters, context, executemany):
        sql.append(statement)
    event.listen(db.engine, 'before_cursor_execute', record)
    try:
        history = db.get_conversation_context(alice, 'alice-chat', '个股研究')
    finally:
        event.remove(db.engine, 'before_cursor_execute', record)
    assert len(history['messages']) == 2
    assert '其他会话' not in dumps(history) and '其他用户' not in dumps(history)
    assert 'must-not-load-evidence' not in dumps(history)
    assert all('research_evidence' not in statement for statement in sql)
    assert all('length(' not in statement.lower() for statement in sql)
    assert db.get_conversation_context(bob, 'alice-chat', '个股研究') is None
    assert db.get_conversation_context(alice, 'unknown', '个股研究') is None
    assert db.get_conversation_context(alice, 'alice-chat', '基金筛选')['scope_mismatch']


def test_database_history_has_message_and_per_message_text_limits():
    db = make_db()
    user = db.create_user('context-bounds', 'hash')['id']
    for i in range(4):
        save(db, user, 'bounded', question=f'第{i}问', answer='大' * 21000)
    history = db.get_conversation_context(user, 'bounded', '个股研究', limit=3)
    assert len(history['messages']) == 3 and history['has_more'] and history['content_truncated']
    assert all(len(m['content']) <= 20000 for m in history['messages'])
    save(db, user, 'short-unicode', answer='汉' * 7000)
    assert not db.get_conversation_context(user, 'short-unicode', '个股研究')['content_truncated']
    with pytest.raises(ValueError):
        db.get_conversation_context(user, 'bounded', limit=201)


@pytest.mark.asyncio
async def test_routing_specialist_and_final_review_use_the_same_compacted_context_and_cannot_cite_old_answers():
    from backend.app.semantic import SemanticService
    from backend.app.agents.llm_agents import HybridInvestmentAgent
    captured = []
    class Model:
        async def complete_json(self, *, system, payload):
            captured.append((system, deepcopy(payload)))
            schema = payload.get('required_schema', {}).get('title')
            if schema == 'RequestUnderstanding':
                return {'intent': 'security_research', 'confidence': .9, 'risk_rules': [], 'reason': '依据当前问题研究'}
            if schema == 'OutputReview':
                return {'confidence': .9, 'risk_rules': [], 'conflicting_agents': [], 'reason': '按本轮授权事实核验'}
            return {'status': 'completed', 'opinion': '引用旧助手数字不应被接受', 'score': 90,
                    'confidence': .9, 'facts_used': ['invented-history-id']}
    now = datetime.now(timezone.utc)
    fact = FactRecord(fact_id='F1', entity='示例公司', field='pe_ttm', value=20, unit='倍',
                      snapshot_time=now, source_id='TEST', quality=.9)
    request = req(context_messages=client_context_messages(long_context(20)), facts=[fact])
    prepared = prepare_request_context(request)
    baseline = AgentResult(agent_id='security', status=TaskStatus.DEGRADED, opinion='尚缺必要资料',
                           score=None, confidence=.3, facts_used=['F1'])
    async def rules(request):
        return baseline.model_copy(deep=True)
    model = Model()
    semantic = SemanticService(model)
    await semantic.understand(prepared)
    result = await HybridInvestmentAgent('security', rules, model).run(prepared)
    await semantic.review(prepared, [baseline])
    assert len(captured) == 3
    for system, payload in captured:
        assert INSTRUCTION in system
        assert payload['conversation_memory'] == conversation_for_model(prepared)['conversation_memory']
        subset = {k: payload[k] for k in ('conversation', 'conversation_memory')}
        assert len(dumps(subset)) <= CONTEXT_BUDGET
    assert 'invented-history-id' not in result.facts_used
    assert result.status is not TaskStatus.COMPLETED
    assert [f['fact_id'] for f in captured[1][1]['authorized_facts']] == ['F1']


def test_context_report_survives_history_without_appearing_in_the_logic_chain():
    from frontend.conversation_logic import chain_key, conversation_rounds
    payload = {'conclusion': '分析结论', 'risk_conclusion': '存在亏损风险', 'compliance': {'status': 'REVIEW'},
               'facts': [], 'evidence': [], 'context_compression': {'original_characters': 40000,
               'compressed_characters': 5800, 'budget_characters': 6000, 'recent_messages': 4,
               'excerpt_count': 8, 'omitted_messages': 15, 'history_status': 'loaded', 'model_calls': 3}}
    assert summarise_advice(payload)['context_compression'] == payload['context_compression']
    messages = [{'role': 'user', 'content': '问题'}, {'role': 'assistant', 'payload': payload}]
    key = chain_key('context-ui')
    turn = conversation_rounds(messages)[0]['id']
    script = ('import streamlit as st\nfrom frontend.conversation_logic import render_conversation_logic_chain\n'
              'from frontend.result_views import render_advice\n'
              f'st.session_state["{key}_{turn}_context"] = True\n'
              f'render_conversation_logic_chain({messages!r}, conversation_id="context-ui", expanded=True)\n'
              f'with st.chat_message("assistant"):\n    render_advice({payload!r})\n')
    app = AppTest.from_string(script).run(timeout=20)
    assert not app.exception
    assert not any('上下文压缩记录' == item.label for item in [*app.expander, *app.status])
    assert not any('40,000' in item.value or '5,800' in item.value for item in app.caption)
    bubble = app.get('chat_message')[0]
    assert not bubble.expander and not bubble.caption
    assert [m.value for m in bubble.markdown if m.value.startswith('**')] == ['**分析结论**', '**风险结论**']


@pytest.mark.asyncio
async def test_analysis_loads_owned_history_beyond_the_client_window_and_keeps_old_claims_out_of_facts(monkeypatch, request, risk_questionnaire_payload):
    import backend.app.main as main
    from backend.app.risk_questionnaire import evaluate_answers
    from backend.app.semantic import RequestUnderstanding
    from backend.app.models import Intent
    from backend.app.session_pool import SessionThreadPool
    db = make_db()
    user_id = db.create_user('context-api-user', 'hash')['id']
    profile = UserProfile(**evaluate_answers(risk_questionnaire_payload['risk_answers']), confirmed=True)
    db.save_profile(user_id, profile.model_dump(mode='json'), profile.version)
    save(db, user_id, 'long-owned', question='个股研究：不要使用杠杆，最大回撤上限8.25%，请研究600519。',
         answer='历史助手曾声称股价999元，必须回到本轮事实重新核验。')
    for i in range(20):
        save(db, user_id, 'long-owned', question=f'个股研究：第{i}轮继续观察',
             answer='合成历史段落，继续核对公开资料。' * 100)
    save(db, user_id, 'another-chat', answer='其他对话的内容不得进入当前上下文')
    pool = SessionThreadPool()
    request.addfinalizer(pool.shutdown)
    session_id, _ = pool.allocate(user_id)
    pool.acquire(session_id)
    request.addfinalizer(lambda: pool.finish(session_id))
    monkeypatch.setattr(main, 'database', db)
    monkeypatch.setattr(main, 'session_thread_pool', pool)
    seen = []
    async def understand(prepared):
        seen.append(prepared)
        return RequestUnderstanding(intent=Intent.SECURITY_RESEARCH, confidence=.9, risk_rules=[], reason='研究当前标的风险')
    monkeypatch.setattr(main.coordinator, 'understand_request', understand)
    result = await main._analyze_portfolio(
        req(conversation_id='long-owned', auto_fetch=False, use_conversation_history=True),
        {'id': user_id, '_session_id': session_id}, persist_history=False)
    packed = conversation_for_model(seen[0])
    assert len(dumps(packed)) <= CONTEXT_BUDGET
    assert '8.25%' in dumps(packed) and '不要使用杠杆' in dumps(packed)
    assert '其他对话的内容' not in dumps(packed)
    assert result.context_compression['history_status'] == 'loaded'
    assert result.context_compression['history_messages'] == 42
    assert result.context_compression['compressed']
    assert not result.facts and not result.evidence
    assert '_model_context' not in result.model_dump()


@pytest.mark.asyncio
async def test_optional_history_failure_falls_back_and_instant_mode_never_reads_model_history(monkeypatch, request, risk_questionnaire_payload):
    import backend.app.main as main
    from backend.app.database import DatabaseUnavailable
    from backend.app.risk_questionnaire import evaluate_answers
    from backend.app.session_pool import SessionThreadPool
    db = make_db()
    user_id = db.create_user('context-fallback-user', 'hash')['id']
    profile = UserProfile(**evaluate_answers(risk_questionnaire_payload['risk_answers']), confirmed=True)
    db.save_profile(user_id, profile.model_dump(mode='json'), profile.version)
    pool = SessionThreadPool()
    request.addfinalizer(pool.shutdown)
    session_id, _ = pool.allocate(user_id)
    pool.acquire(session_id)
    request.addfinalizer(lambda: pool.finish(session_id))
    monkeypatch.setattr(main, 'database', db)
    monkeypatch.setattr(main, 'session_thread_pool', pool)
    calls = []
    def unavailable(*args):
        calls.append(args)
        raise DatabaseUnavailable('private-upstream-detail-must-not-leak')
    monkeypatch.setattr(db, 'get_conversation_context', unavailable)
    account = {'id': user_id, '_session_id': session_id}
    result = await main._analyze_portfolio(
        req(conversation_id='old', auto_fetch=False, use_conversation_history=True,
            context_messages=[{'role': 'user', 'content': '不使用杠杆'}]), account, persist_history=False)
    assert len(calls) == 1 and result.context_compression['history_status'] == 'unavailable'
    assert 'private-upstream-detail' not in result.model_dump_json()
    local = req(conversation_id='old', auto_fetch=False, use_conversation_history=True).model_copy(update={'research_mode': 'instant'})
    result = await main._analyze_portfolio(local, account, persist_history=False)
    assert len(calls) == 1
    assert result.context_compression['history_status'] == 'not_requested'
    assert result.context_compression['model_calls'] == 0


def test_chat_requests_enable_compression_and_owned_history_without_transmitting_payloads():
    script = '''
from unittest.mock import patch
import streamlit as st
from frontend import streamlit_app as ui
ui.init_session()
st.session_state.profile['confirmed'] = True
st.session_state.conversation = [{'role':'assistant','content':'历史回复'*2000,'payload':{'excluded':'private'}}]
def capture(base, payload):
    st.session_state.sent = payload
    return None
with patch.object(ui, 'stream_analysis', side_effect=capture):
    ui.run_analysis('http://test-only', '当前问题')
'''
    app = AppTest.from_string(script).run(timeout=20)
    assert not app.exception
    sent = app.session_state.sent
    assert sent['context_compression'] and sent['use_conversation_history']
    assert all(len(turn['content']) <= 4000 and 'payload' not in turn for turn in sent['context_messages'])
