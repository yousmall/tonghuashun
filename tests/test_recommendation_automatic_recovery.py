"""Recovery uses complete, dated evidence and stays automatic behind the same route."""
import io
import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from reportlab.pdfgen import canvas

from backend.app.models import FactRecord
from backend.app.data_provider.iwencai import IwencaiSkillHubProvider
from backend.app.data_provider.recovery_series import parse_recovery_series
from backend.app.services.recommendation_recovery import derive_history_metrics, derive_explicit_state, history_window
from backend.app.services.disclosure_reader import allowed_disclosure_url, read_disclosure, extract_pdf
from backend.app.services.provider_errors import ProviderCallError
from test_stock_recommendation import Provider, request, understanding, service

NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)
START, END = history_window(NOW)


def fact(field, value, *, entity='示例600001', code='600001', period='2026-10-02', unit=None, key=None):
    return FactRecord(fact_id=key or f'{entity}:{field}:{period}', entity=entity, entity_code=code,
        field=field, value=value, unit=unit, period=period, snapshot_time=NOW, source_id='TEST', quality=.95)


def series(start=START, end=END):
    start, end = date.fromisoformat(start), date.fromisoformat(end)
    dates = [start + timedelta(days=i) for i in range((end - start).days + 1)
             if (start + timedelta(days=i)).weekday() < 5]
    facts = []
    for i, day in enumerate(dates):
        period = day.isoformat()
        facts.extend([
            fact('market_session', 1, entity='中国A股交易日历', code=None, period=period),
            fact('adjusted_close_history', 100 if i < 50 else 80, period=period, unit='CNY:qfq'),
            fact('daily_turnover_history', 0 if i == len(dates) - 1 else 100000000, period=period, unit='CNY'),
            fact('industry_turnover_history', i + 1, entity='制造业', code='881001.TI', period=period, unit='percent'),
        ])
    facts.append(fact('market_session_count', len(dates), entity='中国A股交易日历', code=None,
                      period=f'{start}/{end}'))
    return facts


def derive(facts):
    return derive_history_metrics(facts, code='600001', name='示例600001', industry='制造业',
                                 now=NOW, start=START, end=END)


def test_full_series_derives_reproducible_metrics_with_every_parent():
    inputs = series()
    outputs = {f.field: f for f in derive(inputs)}
    assert outputs['max_drawdown_1y'].value == 20
    assert outputs['avg_turnover_20d'].value == 95000000
    assert outputs['industry_turnover_percentile'].value == 100
    assert all(f.derived_from and f.derivation_rule and f.snapshot_time == NOW for f in outputs.values())
    assert set(outputs['max_drawdown_1y'].derived_from) <= {f.fact_id for f in inputs}


@pytest.mark.parametrize('mutation', ['short', 'missing_price', 'mixed_code', 'unadjusted', 'conflict'])
def test_incomplete_or_wrong_price_series_never_becomes_one_year_drawdown(mutation):
    inputs = series()
    price = next(f for f in inputs if f.field == 'adjusted_close_history')
    if mutation == 'short':
        inputs = inputs[-240:]
    elif mutation == 'missing_price':
        inputs.remove(price)
    elif mutation == 'mixed_code':
        inputs[inputs.index(price)] = price.model_copy(update={'entity_code': '600002'})
    elif mutation == 'unadjusted':
        inputs = [f.model_copy(update={'unit': 'CNY'}) if f.field == 'adjusted_close_history' else f for f in inputs]
    else:
        inputs.append(price.model_copy(update={'fact_id': 'conflict', 'value': 111}))
    assert 'max_drawdown_1y' not in {f.field for f in derive(inputs)}


def test_missing_turnover_day_not_filled_with_zero_and_industry_scope_not_mixed():
    inputs = series()
    amount = next(f for f in reversed(inputs) if f.field == 'daily_turnover_history')
    inputs.remove(amount)
    industry = next(f for f in inputs if f.field == 'industry_turnover_history')
    inputs[inputs.index(industry)] = industry.model_copy(update={'entity_code': '881002.TI'})
    fields = {f.field for f in derive(inputs)}
    assert 'avg_turnover_20d' not in fields and 'industry_turnover_percentile' not in fields


@pytest.mark.parametrize('mutation', ['no_total', 'wrong_total', 'missing_calendar_day'])
def test_calendar_requires_source_total_and_every_declared_session(mutation):
    inputs = series()
    declaration = next(f for f in inputs if f.field == 'market_session_count')
    if mutation == 'no_total':
        inputs.remove(declaration)
    elif mutation == 'wrong_total':
        inputs[inputs.index(declaration)] = declaration.model_copy(update={'value': 240})
    else:
        inputs.remove(next(f for f in inputs if f.field == 'market_session'))
    assert not derive(inputs)


def test_dated_explicit_state_and_counts_only():
    sources = [fact('advancing_count', 400, entity='中国宏观经济', code=None),
               fact('market_total_count', 1000, entity='中国宏观经济', code=None),
               fact('is_suspended', False), fact('listing_status', '上市')]
    results = {f.field: f for f in derive_explicit_state(sources, now=NOW)}
    assert results['market_advancing_ratio'].normalized_value == 40
    assert results['trading_status'].value == '正常交易'
    sources[-1] = sources[-1].model_copy(update={'period': '2026-10-01'})
    assert 'trading_status' not in {f.field for f in derive_explicit_state(sources, now=NOW)}
    sources[1] = sources[1].model_copy(update={'value': 200})
    assert 'market_advancing_ratio' not in {f.field for f in derive_explicit_state(sources, now=NOW)}


def test_strict_series_parser_handles_zero_currency_dates_and_code():
    raw = {'columns': [{'key': '前复权收盘价', 'unit': '元'}, {'key': '成交额', 'unit': '万元'}],
           'datas': [{'股票代码': '600001.SH', '交易日期': '20260930', '前复权收盘价': 80, '成交额': 0},
                     {'股票代码': '600002.SH', '交易日期': '20260930', '前复权收盘价': 10, '成交额': 40}]}
    parsed = parse_recovery_series(raw, target='600001', source_id='TEST', kind='stock_series')
    assert {(f.field, f.value, f.unit) for f in parsed} == {
        ('adjusted_close_history', 80, 'CNY:qfq'), ('daily_turnover_history', 0, 'CNY')}
    assert all(f.period == '2026-09-30' for f in parsed)
    raw['datas'][0]['单位'] = '%'
    assert not parse_recovery_series(raw, target='600001', source_id='TEST', kind='stock_series')


def test_calendar_parser_requires_explicit_trading_dates():
    parsed = parse_recovery_series({'datas': [{'交易日期': '20260930'}, {'交易日': '20260929'},
        {'日期': '20260928'}, {'交易日期': '20260927'}, {'交易日期': '20260925', '是否交易日': False}]},
        target='中国A股交易日历', source_id='TEST', kind='calendar_series')
    assert {f.period for f in parsed} == {'2026-09-29', '2026-09-30'}


@pytest.mark.asyncio
async def test_stock_series_provider_pages_and_keeps_fixed_skill():
    calls = []
    def handler(req):
        payload = json.loads(req.content)
        calls.append((payload, req.headers['x-claw-skill-id']))
        page = int(payload['page'])
        rows = [{'股票代码': '600001.SH', '交易日期': f'202609{day:02}', '前复权收盘价': 80,
                 '成交额': 100} for day in ((21, 22) if page == 1 else (23, 24) if page == 2 else ())]
        return httpx.Response(200, json={'datas': rows, 'columns': [
            {'key': '前复权收盘价', 'unit': '元'}, {'key': '成交额', 'unit': '万元'}]})
    provider = IwencaiSkillHubProvider('secret', transport=httpx.MockTransport(handler), max_retries=0)
    try:
        facts = await provider.get_stock_daily_history('600001', START, END)
    finally:
        await provider.aclose()
    assert len(facts) == 8 and len(calls) == 3
    assert {skill for _, skill in calls} == {'hithink-market-query'}
    assert all('前复权' in payload['query'] and START in payload['query'] for payload, _ in calls)


@pytest.mark.parametrize('url', ['http://static.cninfo.com.cn/a.pdf', 'https://127.0.0.1/a.pdf',
    'https://static.cninfo.com.cn.evil.example/a.pdf', 'https://user@static.cninfo.com.cn/a.pdf',
    'https://static.cninfo.com.cn:444/a.pdf', 'https://example.com/a.pdf'])
def test_document_reader_rejects_untrusted_destinations(url):
    assert not allowed_disclosure_url(url)


def document():
    source = fact('announcement', '年度报告', code=None)
    return source.model_copy(update={'source_url': 'https://static.cninfo.com.cn/finalpage/report.pdf'})


@pytest.mark.asyncio
async def test_document_reader_preserves_exact_excerpt_and_link_and_rejects_wrong_company():
    source = document()
    async def extractor(data):
        assert data.startswith(b'%PDF-')
        return {'identity': '证券代码600001', 'pages': [{'page': 5, 'text': '监管处罚已披露。'}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b'%PDF-test'))) as client:
        facts = await read_disclosure(source, code='600001', name='示例600001', client=client, extractor=extractor)
        assert facts[0].value == '监管处罚已披露。' and facts[0].source_url == source.source_url
        assert facts[0].derived_from == [source.fact_id] and '第5页' in facts[0].source_field
        async def wrong(data):
            return {'identity': '证券代码600002', 'pages': [{'page': 1, 'text': '其他公司的报告'}]}
        assert not await read_disclosure(source, code='600001', name='示例600001', client=client, extractor=wrong)


@pytest.mark.asyncio
async def test_official_pdf_redirect_is_followed_without_rewriting_source_link():
    calls = []
    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(302, headers={'Location': 'https://www.bse.cn/disclosure/annual.pdf'})
        return httpx.Response(200, content=b'%PDF-test')
    async def extractor(data):
        return {'identity': '600001', 'pages': [{'page': 2, 'text': 'Unqualified opinion.'}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        facts = await read_disclosure(document(), code='600001', name='示例600001', client=client, extractor=extractor)
    assert len(calls) == 2 and facts[0].source_url == document().source_url
    assert all('authorization' not in req.headers for req in calls)


@pytest.mark.asyncio
async def test_document_redirect_cannot_reach_an_external_host():
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(302, headers={'Location': 'https://evil.example/report.pdf'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderCallError) as error:
            await read_disclosure(document(), code='600001', name='示例600001', client=client)
    assert error.value.code == 'DOCUMENT_REDIRECT_REJECTED' and len(calls) == 1


@pytest.mark.asyncio
async def test_pdf_worker_extracts_real_text_in_subprocess():
    buffer = io.BytesIO()
    writer = canvas.Canvas(buffer)
    writer.drawString(40, 700, 'Security code 600001. Qualified audit opinion.')
    writer.save()
    result = await extract_pdf(buffer.getvalue())
    assert '600001' in result['identity'] and 'Qualified audit opinion' in result['pages'][0]['text']


class RecoveryProvider(Provider):
    async def get_stock_risk_metrics(self, code):
        return [f for f in await super().get_stock_risk_metrics(code) if f.field not in {'max_drawdown_1y', 'avg_turnover_20d'}]
    async def get_stock_daily_history(self, code, start, end):
        self.calls.append(('stock_history', code))
        return [f.model_copy(update={'entity': '示例' + code, 'entity_code': code,
            'snapshot_time': self.clock, 'fact_id': code + ':' + f.fact_id}) for f in series(start, end)
            if f.field in {'adjusted_close_history', 'daily_turnover_history'}]
    async def get_market_calendar(self, start, end):
        self.calls.append(('calendar', start))
        return [f.model_copy(update={'snapshot_time': self.clock}) for f in series(start, end)
                if f.field in {'market_session', 'market_session_count'}]


@pytest.mark.asyncio
async def test_recommendation_automatically_recovers_missing_metrics_without_frontend_changes():
    provider = RecoveryProvider()
    svc = service(provider)
    prepared, audit, advice = await svc.run(request(), understanding())
    assert len(advice.stock_recommendation.recommendations) == 2, advice.model_dump()
    assert sum(method == 'calendar' for method, _ in provider.calls) == 1
    assert sum(method == 'stock_history' for method, _ in provider.calls) == 2
    assert any(f.field == 'max_drawdown_1y' and f.derived_from for f in prepared.facts)
    assert 'stock_history:600001' in audit.successful_capabilities


@pytest.mark.asyncio
async def test_unitless_direct_metric_triggers_history_recovery_instead_of_silent_acceptance():
    class Unitless(RecoveryProvider):
        async def get_stock_risk_metrics(self, code):
            facts = await super().get_stock_risk_metrics(code)
            return [*facts, self.fact('示例' + code, 'max_drawdown_1y', 10, code=code)]
    provider = Unitless()
    prepared, audit, advice = await service(provider).run(request(), understanding())
    assert len(advice.stock_recommendation.recommendations) == 2
    assert any(f.field == 'max_drawdown_1y' and f.unit == 'percent' and f.derived_from for f in prepared.facts)
