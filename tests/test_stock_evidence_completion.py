"""Evidence completion rejects partial inventories and ambiguous disclosures."""
import json
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
import pytest

from backend.app.agents.coordinator import verify_facts
from backend.app.data_provider.industry_evidence import aggregate_industry_revenue
from backend.app.data_provider.iwencai import IwencaiSkillHubProvider
from backend.app.data_provider.public_recovery import PublicMarketRecovery, parse_bse_adjusted_history
from backend.app.models import AgentResult, FactRecord, TaskStatus
from backend.app.services.disclosure_evidence import derive_disclosure_assessments
from backend.app.services.disclosure_reader import read_disclosure
from backend.app.services.research import derive_scoring_facts
from tests.test_stock_recommendation import Provider, service, request, understanding

NOW = datetime.now(timezone.utc)
START, END = '2025-10-03', '2026-10-03'


def bse_payload():
    return {'rc':0,'data':{'code':'920001','market':0,'name':'测试北交所公司',
        'klines':['2026-09-30,10,11,12,9,123,1000000,0,0,0,1']}}


def bse_url(**updates):
    parameters = {'secid':'0.920001','klt':'101','fqt':'1','beg':'20251003','end':'20261003'}
    parameters.update(updates)
    return 'https://push2his.eastmoney.com/api/qt/stock/kline/get?' + urlencode(parameters)


def parsed_bse(payload=None, url=None):
    return parse_bse_adjusted_history(bse_payload() if payload is None else payload,
        code='920001',start=START,end=END,now=NOW,url=url or bse_url())


def test_bse_api_contract_and_returned_identity_preserve_adjustment_and_amount_units():
    facts = parsed_bse()
    assert [f.field for f in facts] == ['price_adjustment_contract','adjusted_close_history','daily_turnover_history']
    assert facts[1].unit == 'CNY:qfq' and facts[1].value == 11
    assert facts[2].unit == 'CNY' and facts[2].value == 1000000
    assert facts[1].derived_from == facts[2].derived_from == [facts[0].fact_id]


@pytest.mark.parametrize('mutation',['plain','symbol','identity','market','duplicate','negative','nan','ohlc','short','bool_rc'])
def test_bse_history_cannot_accept_wrong_contract_identity_or_malformed_rows(mutation):
    payload,url = bse_payload(),bse_url()
    if mutation == 'plain': url = bse_url(fqt='0')
    elif mutation == 'symbol': url = bse_url(secid='0.920002')
    elif mutation == 'identity': payload['data']['code'] = '874687'
    elif mutation == 'market': payload['data']['market'] = 1
    elif mutation == 'duplicate': payload['data']['klines'] *= 2
    elif mutation == 'bool_rc': payload['rc'] = False
    else:
        values = payload['data']['klines'][0].split(',')
        if mutation == 'negative': values[6] = '-1'
        elif mutation == 'nan': values[2] = 'nan'
        elif mutation == 'ohlc': values[3] = '8'
        elif mutation == 'short': values.pop()
        payload['data']['klines'] = [','.join(values)]
    assert parsed_bse(payload,url) == []


@pytest.mark.asyncio
async def test_bse_history_uses_its_own_endpoint_without_skill_credentials():
    requests = []
    def handler(req):
        requests.append(req)
        assert 'authorization' not in req.headers
        assert req.url.params['secid']=='0.920001' and req.url.params['fqt']=='1'
        return httpx.Response(200,json=bse_payload())
    provider = PublicMarketRecovery(transport=httpx.MockTransport(handler),now=lambda:NOW)
    try:
        assert len(await provider.get_adjusted_stock_history('920001',START,END)) == 3
        assert len(requests)==1
    finally:
        await provider.aclose()


def industry_payload():
    return {'row_count':2,'columns':[{'key':'营业收入[20260630]','unit':'万元'},
        {'key':'营业收入[20250630]','unit':'元'}], 'datas':[
        {'股票代码':'600001.SH','股票简称':'成分股甲','所属同花顺行业':['化妆品'],
         '营业收入[20260630]':11,'营业收入[20250630]':100000},
        {'股票代码':'000001.SZ','股票简称':'成分股乙','所属同花顺行业':['化妆品'],
         '营业收入[20260630]':22,'营业收入[20250630]':200000}]}


def industry_facts(payload=None):
    return aggregate_industry_revenue(industry_payload() if payload is None else payload,
        target='化妆品|884001.TI|2026-06-30|2025-06-30',source_id='IWENCAI_SKILLHUB')


@pytest.mark.parametrize('mutation',['truncated','wrong_industry','duplicate','missing','unknown_unit'])
def test_industry_proxy_requires_complete_same_period_membership_and_units(mutation):
    payload=industry_payload()
    if mutation=='truncated': payload['row_count']=3
    elif mutation=='wrong_industry': payload['datas'][1]['所属同花顺行业']=['其他行业']
    elif mutation=='duplicate': payload['datas'][1]['股票代码']='600001.SH'
    elif mutation=='missing': payload['datas'][1].pop('营业收入[20250630]')
    else: payload['columns'][0]['unit']='unknown'
    assert industry_facts(payload)==[]


@pytest.mark.asyncio
async def test_industry_constituent_proof_survives_candidate_scope_and_fact_verification():
    facts=industry_facts()
    assert facts[-1].value == pytest.approx(10)
    svc=service(Provider())
    scoped=svc._scope_candidate(request().model_copy(update={'facts':facts}),'600999','候选公司','化妆品',[])
    assert {f.fact_id for f in facts} <= {f.fact_id for f in scoped.facts}
    score=next(f for f in scoped.facts if f.entity=='化妆品' and f.field=='prosperity_score')
    result=AgentResult(agent_id='industry',status=TaskStatus.COMPLETED,opinion='行业营收代理',confidence=.8,facts_used=[score.fact_id])
    assert (await verify_facts([result],scoped.facts,now=NOW))[0].status is TaskStatus.COMPLETED
    parent=next(f for f in facts if f.field=='constituent_revenue')
    assert (await verify_facts([result],[f for f in scoped.facts if f.fact_id!=parent.fact_id],now=NOW))[0].status is not TaskStatus.COMPLETED


def excerpt(text, identity='doc',url='https://www.neeq.com.cn/disclosure/report.pdf'):
    return FactRecord(fact_id=identity,entity='示例公司',entity_code='920001',field='announcement_excerpt',
        value=text,period='REC-report2025',snapshot_time=NOW,source_id='OFFICIAL_DISCLOSURE_PDF_V1',
        source_url=url,quality=.9)


def test_full_governance_requires_three_explicit_statements_in_same_report():
    doc=excerpt('审计意见：无保留意见。报告期内，公司未受到任何行政处罚。报告期内，公司及时披露了定期报告。')
    facts=derive_disclosure_assessments([doc],entity='示例公司',code='920001',now=NOW)
    assert {f.field for f in facts}=={'audit_opinion_evidence','regulatory_status_evidence','disclosure_status_evidence','governance_assessment'}
    score=next(f for f in derive_scoring_facts([doc,*facts],now=NOW) if f.field=='governance_score')
    assert score.value==100
    assert all(item['quote'] in doc.value for item in facts[-1].value.values() if isinstance(item,dict))


@pytest.mark.parametrize('text',['我们不对其他信息发表审计意见。','报告期内没有搜索到处罚公告。',
    '如果报告期内，公司未受到任何行政处罚。','审计意见：无保留意见。审计意见：否定意见。'])
def test_absence_conditional_or_conflicting_audit_text_cannot_clear_governance(text):
    facts=derive_disclosure_assessments([excerpt(text)],entity='示例公司',code='920001',now=NOW)
    assert not any(f.field=='governance_assessment' for f in facts)
    if '如果' in text: assert facts==[]


def test_partial_governance_preserves_quote_but_cannot_create_full_score():
    first=excerpt('审计意见：无保留意见。')
    second=excerpt('报告期内，公司未受到任何行政处罚。报告期内，公司及时披露了定期报告。',
                   'other','https://www.neeq.com.cn/disclosure/other.pdf')
    facts=derive_disclosure_assessments([first,second],entity='示例公司',code='920001',now=NOW)
    assert len(facts)==3 and not any(f.field=='governance_assessment' for f in facts)


def test_internal_control_audit_does_not_replace_financial_statement_audit():
    doc=excerpt('内部控制审计意见：无保留意见。')
    assert derive_disclosure_assessments([doc],entity='示例公司',code='920001',now=NOW)==[]


def test_report_checkboxes_require_selected_clearance_not_an_unselected_option():
    clear=excerpt('是否存在被调查处罚的事项 □是 √否。报告期内是否按规定披露定期报告 √是 □否。')
    evidence=derive_disclosure_assessments([clear],entity='示例公司',code='920001',now=NOW)
    assert {f.field for f in evidence}=={'regulatory_status_evidence','disclosure_status_evidence'}
    checked=excerpt('是否存在被调查处罚的事项 √是 □否。报告期内是否按规定披露定期报告 □是 √否。')
    assert derive_disclosure_assessments([checked],entity='示例公司',code='920001',now=NOW)==[]


@pytest.mark.asyncio
async def test_neeq_pdf_is_allowed_but_still_requires_company_identity():
    document=excerpt('年度报告').model_copy(update={'field':'announcement'})
    async def extractor(data): return {'identity':'其他公司 920002','pages':[{'page':1,'text':'审计意见：无保留意见'}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,content=b'%PDF-test'))) as client:
        assert await read_disclosure(document,code='920001',name='示例公司',client=client,extractor=extractor)==[]


@pytest.mark.asyncio
async def test_empty_structured_event_tables_fall_back_to_dated_announcement_evidence():
    provider=IwencaiSkillHubProvider('test-only')
    queries=[]
    async def query(text,**kwargs): queries.append(kwargs['skill_id']);return []
    doc=excerpt('半年报业绩变化').model_copy(update={'field':'announcement_summary'})
    async def search(text,**kwargs): assert kwargs['channel']=='announcement';return [doc]
    provider.query=query
    provider._comprehensive_search=search
    try:
        assert await provider.get_structured_events('920001') == [doc]
        assert queries==['hithink-event-query','hithink-event-query']
    finally: await provider.aclose()


@pytest.mark.asyncio
async def test_structured_metrics_without_document_links_still_fetch_event_originals():
    provider=IwencaiSkillHubProvider('test-only')
    metric=excerpt('forecast').model_copy(update={'field':'预告净利润上限','value':100,'source_url':None})
    quote=metric.model_copy(update={'field':'close_price','fact_id':'live-quote'})
    doc=excerpt('业绩预告原文').model_copy(update={'field':'announcement_summary'})
    async def query(*args,**kwargs): return [metric,quote]
    async def search(*args,**kwargs): return [doc]
    provider.query=query
    provider._comprehensive_search=search
    try:
        facts=await provider.get_structured_events('920001')
        assert doc in facts and metric in facts and not any(f.field=='close_price' for f in facts)
    finally: await provider.aclose()


def test_initial_screen_demands_year_history_without_excluding_a_market_board():
    conditions,query=service(Provider())._conditions(request(),understanding())
    assert conditions['min_listed_history_months']==12 and '上市时间超过1年' in query
    assert '北交所' not in query


@pytest.mark.asyncio
async def test_returned_recent_listing_is_excluded_before_expensive_candidate_recovery():
    class RecentListing(Provider):
        async def get_basic_info(self,code):
            facts=await super().get_basic_info(code)
            return [*facts,self.fact('示例'+code,'listing_date',self.clock.date().isoformat(),code=code)]
    provider=RecentListing()
    svc=service(provider)
    _,audit,advice=await svc.run(request(),understanding())
    assert not advice.stock_recommendation.recommendations
    assert all(c.status=='excluded' and '上市不足一年' in c.reasons[0] for c in advice.stock_recommendation.candidates)
    assert not any(label.startswith('risk_metrics:') for label in audit.requested_capabilities)
