from copy import deepcopy
from datetime import datetime, timezone

import httpx
import pytest

from backend.app.data_provider.iwencai import IwencaiSkillHubProvider
from backend.app.data_provider.industry_evidence import aggregate_industry_revenue
from backend.app.models import FactRecord
from backend.app.services.disclosure_reader import canonical_disclosure_url, read_disclosure
from backend.app.services.official_disclosures import resolve_official_mirror
from backend.app.services.provider_errors import ProviderCallError


def table():
    return {'row_count': 2, 'columns': [
        {'key': '营业收入[20260630]', 'unit': '万元'}, {'key': '营业收入[20250630]', 'unit': '元'}],
        'datas': [{'股票代码': code, '股票简称': code, '所属同花顺行业': ['食品饮料', '白酒', '白酒Ⅲ'],
                   '营业收入[20260630]': current, '营业收入[20250630]': previous}
                  for code, current, previous in [('600001.SH', 2, 10000), ('600002.SH', 1, 10000)]]}


def aggregate(payload):
    return aggregate_industry_revenue(payload, target='白酒|881273.TI|2026-06-30|2025-06-30', source_id='TEST')


def test_complete_industry_revenue_is_sum_ratio_with_all_source_inputs():
    facts = aggregate(table())
    result = facts[-1]
    assert result.field == 'industry_revenue_growth' and result.normalized_value == 50
    assert result.derived_from == [f.fact_id for f in facts[:-1]]
    assert len(result.derived_from) == 5
    assert result.entity_code == '881273.TI' and 'proxy' in result.derivation_rule


@pytest.mark.parametrize('fault', ['truncated', 'duplicate', 'wrong_industry', 'missing', 'unknown_unit', 'bool', 'nan'])
def test_partial_or_incompatible_constituents_never_generate_industry_growth(fault):
    payload = deepcopy(table())
    if fault == 'truncated':
        payload['row_count'] = 3
    elif fault == 'duplicate':
        payload['datas'][1]['股票代码'] = payload['datas'][0]['股票代码']
    elif fault == 'wrong_industry':
        payload['datas'][1]['所属同花顺行业'] = ['其他行业']
    elif fault == 'missing':
        payload['datas'][1].pop('营业收入[20250630]')
    elif fault == 'unknown_unit':
        payload['columns'][0]['unit'] = ''
    else:
        payload['datas'][1]['营业收入[20260630]'] = True if fault == 'bool' else 'nan'
    assert aggregate(payload) == []


@pytest.mark.asyncio
async def test_concise_target_query_keeps_currency_date_and_original_report_link():
    def handler(req):
        import json
        assert json.loads(req.content)['query'] == '600519 研报目标价'
        return httpx.Response(200, json={'columns': [{'key': '目标价', 'unit': '元'}],
            'datas': [{'股票代码': '600519.SH', '股票简称': '贵州茅台', '目标价': 1600,
                '公告日期': '20260920', '研报链接': 'https://news.10jqka.com.cn/report', '最新价': 1400}]})
    provider = IwencaiSkillHubProvider('test', transport=httpx.MockTransport(handler))
    try:
        facts = await provider.get_institutional_research('600519')
        result = next(f for f in facts if f.field == 'target_price')
        assert result.unit == 'CNY' and result.observation_date.isoformat() == '2026-09-20'
        assert result.source_url and result.period.startswith('REC-')
        assert not any(f.field == 'close_price' for f in facts)
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_search_rows_preserve_actual_issuer_instead_of_query_scope():
    provider = IwencaiSkillHubProvider('test')
    try:
        facts = provider._normalize({'datas': [
            {'title': '其他公司：年度报告', 'stock_infos': [{'name': '其他公司', 'code': '600002'}]},
            {'title': '贵州茅台：年度报告', 'stock_infos': [{'code': 'MOUTAI80'}, {'name': '贵州茅台', 'code': '600519'}]},
        ]}, entity_hint='600519', channel='announcement')
        assert [(f.entity, f.entity_code) for f in facts] == [('其他公司', '600002'), ('贵州茅台', '600519')]
    finally:
        await provider.aclose()


def disclosure():
    return FactRecord(fact_id='ANN-1', entity='贵州茅台', entity_code='600519', field='announcement',
        value='贵州茅台：贵州茅台2025年年度报告', period='REC-1', observation_date='2026-04-17',
        source_url='https://static.sse.com.cn/report.pdf', snapshot_time=datetime.now(timezone.utc), source_id='TEST', quality=.9)


@pytest.mark.asyncio
async def test_official_mirror_requires_exact_code_title_and_publication_date():
    source = disclosure()
    # Official timestamps represent midnight in China, the previous UTC date.
    timestamp = int(datetime(2026, 4, 16, 16, tzinfo=timezone.utc).timestamp() * 1000)
    def handler(req):
        assert req.url.host == 'www.cninfo.com.cn' and 'authorization' not in req.headers
        return httpx.Response(200, json={'announcements': [
            {'secCode': '600002', 'secName': '贵州茅台', 'announcementTitle': '贵州茅台2025年年度报告', 'announcementTime': timestamp, 'adjunctUrl': 'finalpage/2026-04-17/111.PDF'},
            {'secCode': '600519', 'secName': '贵州茅台', 'announcementTitle': '贵州茅台2025年年度报告摘要', 'announcementTime': timestamp, 'adjunctUrl': 'finalpage/2026-04-17/222.PDF'},
            {'secCode': '600519', 'secName': '贵州茅台', 'announcementTitle': '贵州茅台2025年年度报告', 'announcementTime': timestamp, 'adjunctUrl': 'finalpage/2026-04-17/333.PDF'},
        ]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        mirror = await resolve_official_mirror(source, code='600519', name='贵州茅台', client=client)
    assert mirror.source_url == 'https://static.cninfo.com.cn/finalpage/2026-04-17/333.PDF'
    assert mirror.observation_date == source.observation_date


@pytest.mark.asyncio
async def test_http_200_challenge_page_is_not_pdf_body():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text='<script>challenge</script>'))) as client:
        with pytest.raises(ProviderCallError) as error:
            await read_disclosure(disclosure(), code='600519', name='贵州茅台', client=client)
    assert error.value.code == 'DOCUMENT_NOT_PDF'


def test_only_official_pdf_http_links_can_be_upgraded():
    assert canonical_disclosure_url('http://static.cninfo.com.cn/a.PDF') == 'https://static.cninfo.com.cn/a.PDF'
    for url in ('http://evil.example/a.pdf', 'http://user@static.cninfo.com.cn/a.pdf', 'http://static.cninfo.com.cn:8080/a.pdf'):
        assert canonical_disclosure_url(url) == url


@pytest.mark.asyncio
@pytest.mark.parametrize('repair_one_day', [False, True])
async def test_tier_suffix_aligns_only_same_tonghuashun_index_and_daily_dates_are_preserved(repair_one_day):
    import json
    import re
    from datetime import date, timedelta
    days = [(date(2025, 10, 9) + timedelta(days=i)).isoformat() for i in range(300)
            if (date(2025, 10, 9) + timedelta(days=i)).weekday() < 5][:201]
    calendar = [FactRecord(fact_id='DAY-' + day, entity='中国A股交易日历', field='market_session', value=1,
                period=day, snapshot_time=datetime.now(timezone.utc), source_id='CAL', quality=.9) for day in days]
    calendar.append(calendar[0].model_copy(update={'fact_id': 'COUNT', 'field': 'market_session_count',
        'value': len(days), 'period': '2025-10-03/2026-10-03'}))
    class Public:
        async def get_exchange_calendar(self, start, end):
            return calendar
        async def aclose(self):
            pass
    calls = []
    def handler(req):
        query = json.loads(req.content)['query']
        calls.append(query)
        assert query.startswith('白酒行业 ')
        row = {'指数代码': '881273.TI', '指数简称': '白酒'}
        columns = []
        for year, month, day in re.findall(r'(\d{4})年(\d+)月(\d+)日换手率', query):
            key = f'换手率[{year}{int(month):02}{int(day):02}]'
            if not (repair_one_day and days[0].replace('-', '') in key and len(calls) == 1):
                row[key] = 1
            columns.append({'key': key, 'unit': '%'})
        return httpx.Response(200, json={'columns': columns, 'datas': [row]})
    provider = IwencaiSkillHubProvider('test', transport=httpx.MockTransport(handler), public_recovery=Public())
    try:
        facts = await provider.get_industry_turnover_history('白酒Ⅲ', '2025-10-03', '2026-10-03')
        history = [f for f in facts if f.field == 'industry_turnover_history']
        assert len(history) == len(days) and len(calls) == (22 if repair_one_day else 21)
        assert {f.entity for f in history} == {'白酒Ⅲ'}
        assert all(f.normalized_value == 1 and f.entity_code == '881273.TI' for f in history)
        from backend.app.services.research_requirements import evidence_status
        from backend.app.services.research import DataCall
        call = DataCall('history', 'get_industry_turnover_history', ('白酒Ⅲ', '2025-10-03', '2026-10-03'), 'history',
            required_fields=('industry_turnover_history',), expected_entity='白酒Ⅲ')
        assert evidence_status(call, facts, datetime.now(timezone.utc))['status'] == 'complete'
        partial = evidence_status(call, facts[:-1], datetime.now(timezone.utc))
        assert partial['status'] == 'partial' and 'HISTORY_COVERAGE_INCOMPLETE' in partial['reason_codes']
    finally:
        await provider.aclose()
