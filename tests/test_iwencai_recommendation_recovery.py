"""Real gateway shapes: quota responses, index scope and column metadata."""
import json
from datetime import datetime, timezone

import httpx
import pytest

from backend.app.data_provider.iwencai import IwencaiSkillHubProvider
from backend.app.services.provider_errors import ProviderCallError, failure_summary
from backend.app.services.research import derive_scoring_facts
from backend.app.models import OrchestrationRequest
from backend.app.semantic import SemanticService
from tests.test_stock_recommendation import Provider, service, request, understanding


@pytest.mark.asyncio
async def test_live_risk_columns_preserve_interval_semantics_and_do_not_invent_risk_metrics():
    provider = IwencaiSkillHubProvider('test-only')
    try:
        facts = provider._normalize({'columns': [
            {'key': '涨跌幅[20250930-20260930]', 'unit': '%', 'timestamp': '20260930'},
            {'key': '成交额平均值[20260902-20260930]', 'unit': '元', 'timestamp': '20260930'},
        ], 'datas': [{'股票代码': '603338', '最新涨跌幅': -1.2,
                     '涨跌幅[20250930-20260930]': 35.4,
                     '成交额平均值[20260902-20260930]': 10000000,
                     '股票简称 LIKE %st%': False, '停牌[20260930]': False}]}, entity_hint='603338')
        assert [f.value for f in facts if f.field == 'change'] == [-1.2]
        interval = next(f for f in facts if f.field == 'interval_change')
        assert interval.period == '2025-09-30/2026-09-30' and interval.unit == 'percent'
        average = next(f for f in facts if f.field == 'interval_avg_turnover')
        assert average.period == '2026-09-02/2026-09-30' and average.unit == 'CNY'
        assert average.observation_date.isoformat() == '2026-09-30'
        assert next(f for f in facts if f.field == 'name_contains_st').value is False
        assert next(f for f in facts if f.field == 'is_suspended').value is False
        # Annual return is not drawdown; a date range alone does not prove 20 sessions.
        assert not any(f.field in {'is_st', 'max_drawdown_1y', 'avg_turnover_20d', 'turnover_value'} for f in facts)
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_empty_gateway_table_does_not_promote_timing_or_credentials_to_facts():
    provider = IwencaiSkillHubProvider('test-only')
    try:
        facts = provider._normalize({'QTime': 2495, 'query': '603338 近一年最大回撤',
            'columns': [{'key': '基金代码'}, {'key': '最大回撤率[20250930-20260930]'}],
            'datas': [], 'row_count': 0, 'token': 'private-never-promote'}, entity_hint='603338')
        assert facts == []
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_explicit_risk_metrics_and_daily_dates_keep_their_existing_contract():
    provider = IwencaiSkillHubProvider('test-only')
    try:
        facts = provider._normalize({'datas': [{'股票代码': '603338',
            '近20个交易日平均成交额[20260930]': 10000000,
            '近一年最大回撤[20260930]': 15, '是否ST[20260930]': False,
            '涨跌幅[20260930]': -1, '停牌原因': '无'}]}, entity_hint='603338')
        assert {'avg_turnover_20d', 'max_drawdown_1y', 'is_st', 'change'} <= {f.field for f in facts}
        assert not any(f.field in {'is_suspended', 'interval_change', 'interval_avg_turnover'} for f in facts)
        assert all(f.period == '2026-09-30' for f in facts if f.field != '停牌原因')
    finally:
        await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [200, 401, 429])
@pytest.mark.parametrize('response_format', ['json', 'text'])
async def test_vendor_quota_message_is_distinct_and_stops_same_skill_batch(status, response_format):
    calls = []
    def handler(req):
        calls.append(req.headers['x-claw-skill-id'])
        if calls[-1] == 'hithink-market-query':
            message = '您今天的次数已用完，建议您升级权益'
            return httpx.Response(status, text=message) if response_format == 'text' else \
                httpx.Response(status, json={'message': message, 'token': 'private-token-never-log'})
        return httpx.Response(200, json={'datas': [{'股票代码': '600001', '市盈率': 20}]})
    provider = IwencaiSkillHubProvider('test-only', transport=httpx.MockTransport(handler), max_retries=2)
    try:
        for method in (provider.get_quote, provider.get_stock_risk_metrics, provider.get_quote):
            with pytest.raises(ProviderCallError) as exc:
                await method('600001')
            assert failure_summary(exc.value) == {'code': 'PROVIDER_QUOTA_EXHAUSTED',
                                                  'status_code': status, 'retryable': False}
            assert '密钥' not in str(exc.value) and 'private-token' not in str(exc.value)
        assert await provider.get_financial_metrics('600001')
        assert calls == ['hithink-market-query', 'hithink-finance-query']
        assert provider._failure_count == 0
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_exhausted_quota_is_rechecked_after_short_cooldown():
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(401, json={'message': '您今天的次数已用完'}) if len(calls) == 1 else \
            httpx.Response(200, json={'datas': [{'股票代码': '600001', '最新价': 10}]})
    provider = IwencaiSkillHubProvider('test-only', transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderCallError):
            await provider.get_quote('600001')
        provider._quota_cooldowns['hithink-market-query'] = (datetime.min.replace(tzinfo=timezone.utc), 401)
        assert await provider.get_quote('600001')
        assert len(calls) == 2
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_industry_query_retains_one_index_convention_and_explicit_currency_dates():
    def handler(req):
        query = json.loads(req.content)['query']
        assert req.headers['x-claw-skill-id'] == 'hithink-industry-query'
        assert '行业板块' in query and '评分' not in query
        return httpx.Response(200, json={'columns': [
            {'key': '主力净买入额[20260930]', 'unit': '元', 'timestamp': '20260930'},
            {'key': '成交额[20260930]', 'unit': '元', 'timestamp': '20260930'},
            {'key': '主力净买入额占成交额比例[20260930]', 'unit': '%', 'timestamp': '20260930'},
        ], 'datas': [
            {'指数代码': '850339.SL', '指数简称': '其他化学制品', '市盈率': 82.64},
            {'指数代码': '884034.TI', '指数简称': '其他化学制品', '市盈率': 35.55,
             '主力净买入额[20260930]': -200, '成交额[20260930]': 1000,
             '主力净买入额占成交额比例[20260930]': -20},
        ]})
    provider = IwencaiSkillHubProvider('test-only', transport=httpx.MockTransport(handler))
    try:
        facts = await provider.get_industry_rank('其他化学制品')
        assert {f.entity_code for f in facts} == {'884034.TI'}
        monetary = [f for f in facts if f.field in {'capital_flow', 'turnover_value'}]
        assert all(f.unit == 'CNY' and f.period == '2026-09-30' for f in monetary)
        ratio = next(f for f in facts if f.field == 'capital_flow_ratio')
        assert ratio.unit == 'percent' and ratio.normalized_value == -20
        derived = derive_scoring_facts(facts, now=datetime.now(timezone.utc))
        assert next(f for f in derived if f.field == 'capital_flow_score').value == 40
        candidate = OrchestrationRequest(query='研究', profile={'confirmed': True}, facts=facts)
        scoped = service(Provider())._scope_candidate(candidate, '600001', '示例600001', '其他化学制品', [])
        assert all(f in scoped.facts for f in facts)
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_conflicting_column_units_do_not_produce_flow_score():
    provider = IwencaiSkillHubProvider('test-only')
    try:
        facts = provider._normalize({'columns': [
            {'key': '主力净买入额', 'unit': '元', 'timestamp': '20260930'},
            {'key': '成交额', 'unit': '元', 'timestamp': '20260930'},
        ], 'datas': [{'指数简称': '行业甲', 'unit': '万元', '主力净买入额': 20, '成交额': 100}]}, entity_hint='行业甲')
        monetary = [f for f in facts if f.field in {'capital_flow', 'turnover_value'}]
        assert len(monetary) == 2 and all(f.unit == 'conflicting' for f in monetary)
        assert not any(f.field == 'capital_flow_score' for f in derive_scoring_facts(facts, now=datetime.now(timezone.utc)))
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_direct_m2_metric_retains_china_scope_period_and_unit():
    def handler(req):
        assert json.loads(req.content)['query'] == '中国最新M2同比增长率'
        return httpx.Response(200, json={'datas': [
            {'指标': 'M2同比增长率', '中国M2同比增长率': 7.5, '时间': '20260831', '单位': '%', '国家': '中国'},
            {'指标': 'M2同比增长率', 'M2同比增长率': 4.5, '时间': '20260831', '单位': '%', '国家': '美国'},
        ]})
    provider = IwencaiSkillHubProvider('test-only', transport=httpx.MockTransport(handler))
    try:
        facts = await provider.get_macro_data('中国最新M2同比增长率')
        china = [f for f in facts if f.entity == '中国宏观经济']
        assert len(china) == 1 and china[0].field == 'm2_growth' and china[0].normalized_value == 7.5
        assert china[0].period == '20260831'
        assert any(f.entity.startswith('美国:') for f in facts)
        derived = derive_scoring_facts(facts, now=datetime.now(timezone.utc))
        assert next(f for f in derived if f.entity == '中国宏观经济' and f.field == 'liquidity_score').value == 47.5
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_recommendation_reports_quota_and_precise_profile_gaps_without_retry():
    class ExhaustedProvider(Provider):
        async def get_stock_risk_metrics(self, code):
            self.calls.append(('risk', code))
            raise ProviderCallError('quota context', code='PROVIDER_QUOTA_EXHAUSTED', status_code=401)
    provider = ExhaustedProvider()
    _, audit, advice = await service(provider).run(request(), understanding())
    assert not advice.stock_recommendation.recommendations
    assert '查询额度已用完' in advice.conclusion
    assert sum(method == 'risk' for method, _ in provider.calls) == 2
    assert all(entry.missing_fields['profile_fit'] == ['max_drawdown_1y', 'avg_turnover_20d', 'is_st', 'trading_status']
               for entry in advice.stock_recommendation.candidates)
    assert all('额度已用完' in entry.reasons[0] for entry in advice.stock_recommendation.candidates)
    assert all(not error['retryable'] for error in audit.capability_errors.values())


@pytest.mark.asyncio
@pytest.mark.parametrize('linked', [True, False])
async def test_qualitative_assessment_can_cite_linked_summary_but_cannot_invent_links(linked):
    provider = Provider()
    summary = provider.fact('中国宏观经济', 'news_summary', '政策明确提出扩大内需。', document=linked)
    class SummaryModel:
        async def complete_json(self, *, system, payload):
            return {'confidence': .9, 'matches': [], 'assessments': [{
                'entity': '中国宏观经济', 'dimension': 'policy', 'complete': True, 'items': [{
                    'criterion': 'policy', 'label': 'supportive', 'evidence_id': summary.fact_id,
                    'quote': '扩大内需'}]}]}
    runner = service(provider)
    runner.coordinator.semantic = SemanticService(SummaryModel())
    req = request().model_copy(update={'facts': [summary]})
    assessed = await runner._assess(req, [{'entity': '中国宏观经济', 'dimension': 'policy'}], [])
    scores = [f for f in assessed.facts if f.field == 'policy_score']
    assert bool(scores) == linked
    if linked:
        assert scores[0].value == 75
        assert summary.fact_id in scores[0].derived_from


@pytest.mark.asyncio
async def test_governance_fetch_targets_audit_and_regulatory_disclosures():
    def handler(req):
        payload = json.loads(req.content)
        assert req.headers['x-claw-skill-id'] == 'announcement-search'
        assert payload['channels'] == ['announcement']
        assert all(word in payload['query'] for word in ('600001', '年度报告', '审计意见', '信息披露', '监管'))
        assert '无处罚' not in payload['query']
        return httpx.Response(200, json={'data': [{'标题': '年度报告', '摘要': '审计意见',
            'url': 'https://example.com/annual-report', 'publish_date': '2026-09-30'}]})
    provider = IwencaiSkillHubProvider('test-only', transport=httpx.MockTransport(handler))
    try:
        facts = await provider.get_governance_disclosures('600001')
        assert any(f.field == 'announcement_summary' and f.source_url and f.period for f in facts)
    finally:
        await provider.aclose()
