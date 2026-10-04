"""Public supplementation needs explicit adjustment and complete exchange proof."""
import json
from datetime import datetime, timezone

import httpx
import pytest

from backend.app.data_provider.public_recovery import (
    ANNUAL_NOTICES, PublicMarketRecovery, calendar_from_notices, parse_adjusted_history, parse_annual_notice,
    parse_risk_inventory)
from backend.app.data_provider.iwencai import IwencaiSkillHubProvider
from backend.app.services.provider_errors import ProviderCallError
from backend.app.services.recommendation_recovery import derive_history_metrics
from backend.app.models import AgentResult, TaskStatus
from backend.app.agents.coordinator import verify_facts
from tests.test_stock_recommendation import Provider, service, request

NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)
START, END = '2025-10-02', '2026-10-02'
RANGES = {
    2025: [('元旦', '1月1日'), ('春节', '1月28日至2月4日'), ('清明节', '4月4日至4月6日'),
           ('劳动节', '5月1日至5月5日'), ('端午节', '5月31日至6月2日'), ('国庆节、中秋节', '10月1日至10月8日')],
    2026: [('元旦', '1月1日至1月3日'), ('春节', '2月15日至2月23日'), ('清明节', '4月4日至4月6日'),
           ('劳动节', '5月1日至5月5日'), ('端午节', '6月19日至6月21日'),
           ('中秋节', '9月25日至9月27日'), ('国庆节', '10月1日至10月7日')],
}


def notice(year):
    import re
    sections = []
    for i, (name, dates) in enumerate(RANGES[year]):
        # Formatting fixture; weekday text is currently presentation metadata.
        dates = re.sub(r'(\d+日)', r'\1（星期日）', dates)
        sections.append(f'（{"一二三四五六七"[i]}）{name}：{dates}休市，另行公布交易测试安排。')
    return f'<h1>关于{year}年部分节假日休市安排的通知</h1><p>一、休市安排</p>{"".join(sections)}二、清算交收'


def sources():
    return {year: [(url, notice(year)) for url in urls] for year, urls in ANNUAL_NOTICES.items()}


def calendar():
    return calendar_from_notices(sources(), start=START, end=END, now=NOW)


def qfq(rows=None):
    return {'code': 0, 'data': {'sh600001': {'qt': {'sh600001': ['1', '测试公司', '600001']},
            'qfqday': rows or [['2026-09-30', '100', '80', '101', '79', '1']]}}}


def parse(payload):
    return parse_adjusted_history(payload, symbol='sh600001', start=START, end=END,
                                  now=NOW, url='https://web.ifzq.gtimg.cn/test')


def test_exchange_calendar_includes_all_six_originals_and_exact_window_count():
    facts = calendar()
    days = {f.period for f in facts if f.field == 'market_session'}
    assert len(days) == 241
    assert {'2025-10-09', '2026-09-24', '2026-09-28', '2026-09-30'} <= days
    assert not {'2025-10-08', '2026-09-25', '2026-10-01', '2026-02-23'} & days
    roots = [f for f in facts if f.field == 'exchange_calendar_notice']
    assert len(roots) == 6 and len({f.source_url for f in roots}) == 6
    assert next(f for f in facts if f.field == 'market_session_count').value == 241
    assert all(set(f.derived_from) == {r.fact_id for r in roots} for f in facts if f.derived_from)


@pytest.mark.parametrize('mutation,expected', [
    ('missing_exchange', 'CALENDAR_COVERAGE_INCOMPLETE'), ('missing_year', 'CALENDAR_COVERAGE_INCOMPLETE'),
    ('missing_holiday', 'CALENDAR_NOTICE_INVALID'), ('different_dates', 'CALENDAR_EXCHANGE_CONFLICT'),
    ('wrong_year', 'CALENDAR_NOTICE_INVALID')])
def test_calendar_fails_closed_on_partial_or_conflicting_exchange_publications(mutation, expected):
    records = sources()
    url, text = records[2026][0]
    if mutation == 'missing_exchange': records[2026].pop()
    elif mutation == 'missing_year': records.pop(2025)
    elif mutation == 'missing_holiday': records[2026][0] = (url, text.replace('中秋节', '不存在的假期'))
    elif mutation == 'different_dates': records[2026][0] = (url, text.replace('9月25日', '9月24日'))
    else: records[2026][0] = (url, notice(2025))
    with pytest.raises(ProviderCallError) as error:
        calendar_from_notices(records, start=START, end=END, now=NOW)
    assert error.value.code == expected


@pytest.mark.parametrize('mutation', ['plain_day', 'wrong_code', 'wrong_symbol', 'broken_ohlc', 'nan', 'conflict'])
def test_prices_require_explicit_qfq_identity_and_nonconflicting_valid_prices(mutation):
    payload = qfq()
    stock = payload['data']['sh600001']
    if mutation == 'plain_day': stock['day'] = stock.pop('qfqday')
    elif mutation == 'wrong_code': stock['qt']['sh600001'][2] = '600002'
    elif mutation == 'wrong_symbol': payload['data']['sh600002'] = payload['data'].pop('sh600001')
    elif mutation == 'broken_ohlc': stock['qfqday'][0][3] = '70'
    elif mutation == 'nan': stock['qfqday'][0][2] = 'nan'
    else: stock['qfqday'].append(['2026-09-30', '100', '81', '101', '79', '1'])
    assert parse(payload) == []


@pytest.mark.asyncio
async def test_complete_public_calendar_and_qfq_chain_passes_fact_verifier_and_derives_drawdown():
    facts = calendar()
    dates = sorted(f.period for f in facts if f.field == 'market_session')
    prices = parse(qfq([[day, '100', '100' if i < 100 else '80', '101', '79', '1'] for i, day in enumerate(dates)]))
    prices = [f.model_copy(update={'entity': '测试公司'}) for f in prices]
    facts += prices
    derived = derive_history_metrics(facts, code='600001', name='测试公司', industry='行业', now=NOW, start=START, end=END)
    assert [(f.field, f.value) for f in derived] == [('max_drawdown_1y', 20)]
    result = AgentResult(agent_id='security', status=TaskStatus.COMPLETED, opinion='测试', confidence=.8,
                         facts_used=[derived[0].fact_id])
    verified = await verify_facts([result], facts + derived, now=NOW)
    assert verified[0].status is TaskStatus.COMPLETED
    scoped = service(Provider())._scope_candidate(request().model_copy(update={'facts': facts + derived}),
                                                  '600001', '测试公司', '行业', [])
    assert {f.fact_id for f in facts} <= {f.fact_id for f in scoped.facts}
    # An omitted exchange original breaks the transitive proof chain.
    missing = next(f for f in facts if f.field == 'exchange_calendar_notice')
    verified = await verify_facts([result], [f for f in facts + derived if f.fact_id != missing.fact_id], now=NOW)
    assert verified[0].status is not TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_public_client_handles_official_cookie_redirect_and_never_sends_bearer_token():
    calls = []
    def handler(req):
        calls.append(req)
        assert 'authorization' not in req.headers
        if len(calls) == 1:
            return httpx.Response(302, headers={'location': '/next', 'set-cookie': 'public-cookie=ok; Path=/'})
        assert 'public-cookie=ok' in req.headers.get('cookie', '')
        return httpx.Response(200, text=notice(2026))
    recovery = PublicMarketRecovery(transport=httpx.MockTransport(handler), now=lambda: NOW)
    p = IwencaiSkillHubProvider('private-key', public_recovery=recovery)
    try:
        text = await recovery._get('https://www.bse.cn/original')
        assert parse_annual_notice(text, 2026)[1]
        assert len(calls) == 2
    finally:
        await p.aclose()
    assert recovery.client.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize('redirect', ['http://www.bse.cn/next', 'https://external.example/next', 'https://user:pass@www.bse.cn/next', 'https://www.bse.cn:invalid/next'])
async def test_public_client_rejects_untrusted_redirects(redirect):
    recovery = PublicMarketRecovery(transport=httpx.MockTransport(lambda req:
        httpx.Response(302, headers={'location': redirect})))
    try:
        with pytest.raises(ProviderCallError) as error:
            await recovery._get('https://www.bse.cn/original')
        # httpx itself rejects an invalid port while preparing the redirect,
        # before the adapter can inspect its target; neither path follows it.
        expected = 'PUBLIC_SOURCE_UNAVAILABLE' if ':invalid/' in redirect else 'PUBLIC_SOURCE_REDIRECT_REJECTED'
        assert error.value.code == expected
    finally:
        await recovery.aclose()


@pytest.mark.asyncio
async def test_calendar_parallel_requests_share_cache_and_unsupported_year_is_explicit():
    calls = []
    def handler(req):
        calls.append(req)
        year = next(y for y, urls in ANNUAL_NOTICES.items() if str(req.url) in urls)
        return httpx.Response(200, text=notice(year))
    recovery = PublicMarketRecovery(transport=httpx.MockTransport(handler), now=lambda: NOW)
    try:
        import asyncio
        results = await asyncio.gather(*(recovery.get_exchange_calendar(START, END) for _ in range(4)))
        assert len(calls) == 6 and all(len(result) == 248 for result in results)
        with pytest.raises(ProviderCallError) as error:
            await recovery.get_exchange_calendar('2026-10-02', '2027-10-02')
        assert error.value.code == 'CALENDAR_YEAR_UNSUPPORTED'
    finally:
        await recovery.aclose()


def test_recovery_dispatch_stays_profile_gated_and_does_not_bypass_vendor_rejection():
    class WithPublicRecovery(Provider):
        public_recovery = object()
        async def get_exchange_calendar(self, *args): return []
        async def get_adjusted_stock_history(self, *args): return []
    from backend.app.models import DataAcquisitionResult
    svc = service(WithPublicRecovery())
    req = request()
    calls = svc._recovery_calls(req, '600001', '示例600001', '制造业', DataAcquisitionResult())
    assert {'get_exchange_calendar', 'get_adjusted_stock_history'} <= {call.method for call in calls}
    rejected = DataAcquisitionResult(capability_errors={'quote:600001': {'code': 'AUTHENTICATION_REJECTED'}})
    calls = svc._recovery_calls(req, '600001', '示例600001', '制造业', rejected)
    assert not {'get_exchange_calendar', 'get_adjusted_stock_history'} & {call.method for call in calls}


def inventory_row(code='600002', market=1):
    return {'f12': code, 'f13': market, 'f124': int(NOW.timestamp())}


def board(rows=None, total=None):
    rows = [inventory_row()] if rows is None else rows
    return {'rc': 0, 'data': {'total': len(rows) if total is None else total, 'diff': rows}}


@pytest.mark.parametrize('mutation', ['no_rows', 'short_page', 'duplicate', 'changed_total',
                                     'wrong_market', 'bse', 'stale', 'future', 'wrong_payload', 'boolean_rc'])
def test_risk_classification_cannot_clear_a_stock_from_incomplete_or_out_of_scope_board(mutation):
    pages = [board()]
    row = pages[0]['data']['diff'][0]
    if mutation == 'no_rows': pages = [board(rows=[], total=1)]
    elif mutation == 'short_page': pages[0]['data']['total'] = 2
    elif mutation == 'duplicate': pages[0]['data']['diff'].append(dict(row))
    elif mutation == 'changed_total': pages.append(board([inventory_row('000001', 0)], total=2))
    elif mutation == 'wrong_market': row['f13'] = 0
    elif mutation == 'bse': row['f12'] = '920038'
    elif mutation == 'stale': row['f124'] = int(NOW.timestamp()) - 17*86400
    elif mutation == 'future': row['f124'] = int(NOW.timestamp()) + 86400
    elif mutation == 'boolean_rc': pages[0]['rc'] = False
    else: pages = [None]
    with pytest.raises(ProviderCallError):
        parse_risk_inventory(pages, now=NOW, url='https://push2.eastmoney.com/api/qt/clist/get')


@pytest.mark.asyncio
async def test_full_classified_board_derives_positive_and_negative_state_with_shared_proof():
    calls = []
    def handler(req):
        calls.append(req)
        assert 'authorization' not in req.headers
        assert req.url.params['fs'] == 'm:0 f:4,m:1 f:4'
        return httpx.Response(200, json=board())
    recovery = PublicMarketRecovery(transport=httpx.MockTransport(handler), now=lambda: NOW)
    try:
        absent = await recovery.get_stock_risk_state('600001')
        present = await recovery.get_stock_risk_state('600002')
        assert len(calls) == 1
        assert absent[-1].value is False and present[-1].value is True
        assert absent[-1].derived_from == present[-1].derived_from == [absent[0].fact_id]
        assert await recovery.get_stock_risk_state('920038') == []
        assert len(calls) == 1
        scoped = service(Provider())._scope_candidate(request().model_copy(update={'facts': absent}),
                                                       '600001', '测试公司', '制造业', [])
        assert absent[0].fact_id in {f.fact_id for f in scoped.facts}
        result = AgentResult(agent_id='security', status=TaskStatus.COMPLETED, opinion='测试',
                             confidence=.8, facts_used=[absent[-1].fact_id])
        assert (await verify_facts([result], scoped.facts, now=NOW))[0].status is TaskStatus.COMPLETED
    finally:
        await recovery.aclose()


@pytest.mark.asyncio
async def test_paged_risk_inventory_requires_last_page_and_suppresses_repeated_failed_calls():
    calls = []
    def handler(req):
        calls.append(req)
        if req.url.params['pn'] == '1':
            return httpx.Response(200, json=board([inventory_row(f'600{i:03}') for i in range(100)], total=101))
        return httpx.Response(200, json=board([inventory_row('000001', 0)], total=101))
    recovery = PublicMarketRecovery(transport=httpx.MockTransport(handler), now=lambda: NOW)
    try:
        facts = await recovery.get_stock_risk_state('600999')
        assert len(calls) == 2 and facts[-1].value is False
        assert facts[0].value['declared_count'] == 101
    finally:
        await recovery.aclose()
    calls.clear()
    def unavailable(req):
        calls.append(req)
        return httpx.Response(502)
    recovery = PublicMarketRecovery(transport=httpx.MockTransport(unavailable), now=lambda: NOW)
    try:
        for code in ['600001', '600002']:
            with pytest.raises(ProviderCallError) as error:
                await recovery.get_stock_risk_state(code)
            assert error.value.status_code == 502
        assert len(calls) == 1
    finally:
        await recovery.aclose()


def test_calendar_requires_three_distinct_registered_exchange_sources():
    records = sources()
    records[2026][1] = records[2026][0]
    with pytest.raises(ProviderCallError):
        calendar_from_notices(records, start=START, end=END, now=NOW)


@pytest.mark.parametrize('field,value,unit', [('max_drawdown_1y', 31, 'percent'),
    ('avg_turnover_20d', 1, 'CNY'), ('is_st', True, None), ('trading_status', '停牌', None)])
def test_known_hard_profile_failure_is_not_hidden_by_other_missing_metrics(field, value, unit):
    from backend.app.models import FactRecord
    svc = service(Provider())
    current = svc.pipeline.now()
    fact = FactRecord(fact_id='verified-negative', entity='测试公司', entity_code='600001',
        field=field, value=value, unit=unit, snapshot_time=current, quality=.95, source_id='TEST')
    result, reason, evidence = svc._fit(request().model_copy(update={'facts': [fact]}), '测试公司', [])
    assert result == 'no' and evidence == [fact.fact_id]


def test_unknown_or_unconfirmed_unit_cannot_turn_a_missing_profile_metric_into_exclusion():
    from backend.app.models import FactRecord
    svc = service(Provider())
    fact = FactRecord(fact_id='invalid-negative', entity='测试公司', entity_code='600001',
        field='max_drawdown_1y', value=50, snapshot_time=svc.pipeline.now(), quality=.95, source_id='TEST')
    assert svc._fit(request().model_copy(update={'facts': [fact]}), '测试公司', [])[0] == 'unknown'
