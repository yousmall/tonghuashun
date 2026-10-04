"""Agent-directed recovery uses controlled provider/model responses, never live services."""
import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app import main
from backend.app.agents.coordinator import cross_validate_results
from backend.app.agents.llm_agents import HybridInvestmentAgent, LLMConfig, OpenAICompatibleLLM
from backend.app.agents.rule_agents import make_rule_agents
from backend.app.data_provider.iwencai import IwencaiSkillHubProvider
from backend.app.models import AgentResult, DataAcquisitionResult, Intent, ResearchCapability, TaskStatus
from backend.app.services.history import _compact_acquisition
from backend.app.services.provider_errors import ProviderCallError
from backend.app.services.research import AutomatedResearchPipeline
from backend.app.services.research_recovery import recover_research
from tests.test_research_recovery import facts_for, request, advice_for, Provider


def complete_request():
    return request([*facts_for('market'), *facts_for('industry'), *facts_for('security')])


def agent_advice(req, capabilities=('company_operations',)):
    advice = advice_for(req)
    advice.agent_results = [AgentResult(agent_id='security', status='degraded',
        opinion='仍缺少经营数据', confidence=.3, facts_used=['security-fundamental_score'],
        data_requirements=list(capabilities))]
    return advice


@pytest.mark.asyncio
async def test_agent_new_need_is_fetched_even_with_complete_score_dimensions():
    provider = Provider()
    async def operations(scope):
        provider.calls.append(scope)
        return [facts_for('security')[0].model_copy(update={
            'fact_id': 'operations', 'field': '主营业务构成', 'value': '制造业收入占比80%'})]
    provider.get_company_operations = operations
    req = complete_request()
    output, audit = await recover_research(AutomatedResearchPipeline(provider), req,
        Intent.SECURITY_RESEARCH, agent_advice(req), DataAcquisitionResult(), target='示例科技')
    assert provider.calls == ['示例科技']
    assert audit.recovery_agent_requirements == {'security': [ResearchCapability.COMPANY_OPERATIONS]}
    assert audit.recovery_capabilities == ['company_operations']
    assert audit.recovery_phases == ['after_analysis']
    assert any(f.fact_id == 'operations' and f.produced_by.startswith('get_company_operations@')
               for f in output.facts)


@pytest.mark.asyncio
async def test_preflight_does_not_exhaust_agent_recovery_and_each_phase_runs_once():
    provider = Provider()
    pipeline = AutomatedResearchPipeline(provider)
    req = request([*facts_for('market', omitted=('liquidity_score',)),
                   *facts_for('industry'), *facts_for('security')])
    output, audit = await recover_research(pipeline, req, Intent.SECURITY_RESEARCH,
                                          None, DataAcquisitionResult())
    assert audit.recovery_rounds == 1
    async def operations(scope):
        provider.calls.append(scope)
        return [facts_for('security')[0].model_copy(update={'fact_id': 'business', 'field': '主营构成'})]
    provider.get_company_operations = operations
    output, audit = await recover_research(pipeline, output, Intent.SECURITY_RESEARCH,
                                          agent_advice(output), audit, target='示例科技')
    assert audit.recovery_rounds == 2
    assert audit.recovery_phases == ['before_analysis', 'after_analysis']
    assert [attempt['capabilities'] for attempt in audit.recovery_attempts] == [['macro'], ['company_operations']]
    assert _compact_acquisition(audit.model_dump(mode='json'))['recovery_attempts'] == audit.model_dump(mode='json')['recovery_attempts']
    before = list(provider.calls)
    for advice in (None, agent_advice(output)):
        await recover_research(pipeline, output, Intent.SECURITY_RESEARCH, advice, audit)
    assert provider.calls == before
    DataAcquisitionResult.model_validate(audit.model_dump())


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['empty', 'timeout', 'forbidden'])
async def test_failed_agent_fetch_remains_review_and_does_not_loop(case):
    provider = Provider()
    async def operations(scope):
        provider.calls.append(scope)
        if case == 'forbidden':
            raise ProviderCallError('private', code='CAPABILITY_FORBIDDEN', status_code=403)
        if case == 'timeout':
            import asyncio
            await asyncio.sleep(.05)
        return []
    provider.get_company_operations = operations
    pipeline = AutomatedResearchPipeline(provider, call_timeout_seconds=.01)
    req = complete_request()
    advice = agent_advice(req)
    output, audit = await recover_research(pipeline, req, Intent.SECURITY_RESEARCH,
                                          advice, DataAcquisitionResult())
    assert output is req and audit.recovery_rounds == 1
    assert cross_validate_results(advice.agent_results, output.facts).status == 'REVIEW'
    assert 'private' not in audit.model_dump_json()
    if case != 'empty':
        assert audit.recovery_errors['company_operations']['code'] == (
            'RECOVERY_TIMEOUT' if case == 'timeout' else 'CAPABILITY_FORBIDDEN')
    await recover_research(pipeline, output, Intent.SECURITY_RESEARCH, advice, audit)
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_agent_cannot_expand_role_or_bypass_prior_permission_failure():
    provider = Provider()
    async def operations(scope):
        raise AssertionError('forbidden capability called')
    provider.get_company_operations = operations
    req = complete_request()
    advice = agent_advice(req)
    audit = DataAcquisitionResult(capability_errors={'company_operations': {
        'code': 'CAPABILITY_FORBIDDEN', 'retryable': False}})
    await recover_research(AutomatedResearchPipeline(provider), req, Intent.SECURITY_RESEARCH, advice, audit)
    advice.agent_results[0].agent_id = 'market'
    await recover_research(AutomatedResearchPipeline(provider), req, Intent.SECURITY_RESEARCH,
                          advice, DataAcquisitionResult())
    assert not provider.calls
    with pytest.raises(ValidationError):
        AgentResult(agent_id='security', status='degraded', opinion='test', confidence=0,
                    data_requirements=['https://arbitrary.test/api'])


@pytest.mark.asyncio
async def test_hybrid_agent_can_request_data_but_cannot_mark_pending_need_complete():
    req = complete_request()
    captured = []
    def handler(http_request):
        captured.append(json.loads(http_request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({
            'status': 'completed', 'opinion': '需要经营构成', 'confidence': .9,
            'facts_used': ['security-fundamental_score'],
            'data_requirements': ['company_operations'], 'score': 99})}}]})
    llm = OpenAICompatibleLLM(LLMConfig('https://model.test/v1', 'test', 'test', max_retries=0),
                            transport=httpx.MockTransport(handler))
    try:
        agent = HybridInvestmentAgent('security', make_rule_agents()['security'], llm)
        result = await agent.run(req)
        assert result.status == 'degraded' and result.confidence <= .4
        assert result.data_requirements == [ResearchCapability.COMPANY_OPERATIONS]
        assert result.score == result.rule_score == 55
        assert 'AGENT_DATA_REQUIRED' in {i.code for i in cross_validate_results([result], req.facts).issues}
        payload = json.loads(captured[0]['messages'][1]['content'])
        assert 'company_operations' in payload['required_output']['data_requirements']
    finally:
        await llm.aclose()


def test_endpoint_reanalyzes_agent_need_after_preflight_and_runs_all_gates(monkeypatch):
    provider = Provider()
    async def macro(scope):
        provider.calls.append(scope)
        return facts_for('market', omitted=('liquidity_score',) if len(provider.calls) == 1 else ())
    provider.get_macro_data = macro
    async def operations(scope):
        provider.calls.append(('operations', scope))
        return [facts_for('security')[0].model_copy(update={'fact_id': 'business', 'field': '主营构成'})]
    provider.get_company_operations = operations
    counts = {'agent': 0, 'verify': 0, 'review': 0}
    original_security = main.coordinator.agents['security']
    original_verify = main.coordinator.verifier
    original_review = main.coordinator.semantic.review
    async def security(req):
        counts['agent'] += 1
        result = await original_security(req)
        if not any(f.fact_id == 'business' for f in req.facts):
            result.data_requirements = [ResearchCapability.COMPANY_OPERATIONS]
            result.status = TaskStatus.DEGRADED
        else:
            result.facts_used.append('business')
        return result
    async def verify(*args, **kwargs):
        counts['verify'] += 1
        return await original_verify(*args, **kwargs)
    async def review(*args, **kwargs):
        counts['review'] += 1
        return await original_review(*args, **kwargs)
    monkeypatch.setitem(main.coordinator.agents, 'security', security)
    monkeypatch.setattr(main.coordinator, 'verifier', verify)
    monkeypatch.setattr(main.coordinator.semantic, 'review', review)
    monkeypatch.setattr(main, 'research_pipeline', AutomatedResearchPipeline(provider))
    req = request([*facts_for('market', omitted=('liquidity_score',)),
                   *facts_for('industry'), *facts_for('security')])
    response = TestClient(main.app).post('/api/v1/portfolio/analyze', json=req.model_dump(mode='json'))
    assert response.status_code == 200
    body = response.json()
    assert body['data_acquisition']['recovery_phases'] == ['before_analysis', 'after_analysis']
    assert body['data_acquisition']['recovery_reanalyzed']
    assert body['data_acquisition']['recovery_rounds'] == 2
    assert body['cross_validation']['status'] == body['compliance']['status'] == 'PASS'
    assert counts == {'agent': 2, 'verify': 2, 'review': 2}
    assert sum(isinstance(c, tuple) and c[0] == 'operations' for c in provider.calls) == 1
    assert 'business' in body['evidence']


@pytest.mark.asyncio
async def test_agent_requirement_reaches_actual_iwencai_adapter_protocol():
    calls = []
    now = datetime.now(timezone.utc).strftime('%Y%m%d')
    def handler(req):
        calls.append((req.url.path, req.headers['X-Claw-Skill-Id'], json.loads(req.content)))
        return httpx.Response(200, json={'data': {'datas': [{
            '股票代码': '600001', '股票简称': '示例科技', f'净资产收益率ROE[{now}]': 15}]}})
    provider = IwencaiSkillHubProvider('test-only', transport=httpx.MockTransport(handler),
                                      max_retries=0)
    try:
        req = complete_request()
        output, audit = await recover_research(AutomatedResearchPipeline(provider), req,
            Intent.SECURITY_RESEARCH, agent_advice(req, ('financial',)), DataAcquisitionResult(),
            target='示例科技')
        financial_calls = [c for c in calls if c[1] == 'hithink-finance-query']
        assert len(financial_calls) == 1
        assert financial_calls[0][0].endswith('/v1/query2data')
        assert '示例科技' in financial_calls[0][2]['query'] and 'ROE' in financial_calls[0][2]['query']
        assert 'financial' in audit.recovery_successful_capabilities
        assert any(f.source_id == 'IWENCAI_SKILLHUB' and f.field == 'roe' for f in output.facts)
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_permission_failure_cannot_be_retried_under_another_agent_call_label():
    provider = Provider()
    requested = []
    async def details(scope):
        requested.append('details')
        raise ProviderCallError('private', code='CAPABILITY_FORBIDDEN', status_code=403)
    async def announcements(scope):
        requested.append('announcements')
        raise AssertionError('same unauthorized announcement skill called again')
    provider.get_stock_disclosure_details = details
    provider.get_announcements = announcements
    pipeline = AutomatedResearchPipeline(provider)
    req = complete_request()
    _, audit = await recover_research(pipeline, req, Intent.SECURITY_RESEARCH,
                                     None, DataAcquisitionResult(), target='示例科技')
    assert requested == ['details']
    assert audit.recovery_attempts[0]['blocked_families']['announcement']['retryable'] is False
    await recover_research(pipeline, req, Intent.SECURITY_RESEARCH,
                           agent_advice(req, ('announcement',)), audit, target='示例科技')
    assert requested == ['details']


@pytest.mark.asyncio
async def test_stock_candidates_recover_agent_need_before_entering_recommendations():
    from tests.test_stock_recommendation import Provider as StockProvider, service, request as stock_request, understanding
    provider = StockProvider()
    async def operations(code):
        provider.calls.append(('operations', code))
        return [provider.fact('示例' + code, '主营构成', '制造业务', code=code)]
    provider.get_company_operations = operations
    runner = service(provider)
    original = runner.coordinator.agents['security']
    runs = []
    async def security(req):
        result = await original(req)
        name = result.details.get('security')
        runs.append(name)
        found = [f for f in req.facts if f.entity == name and f.field == '主营构成']
        if not found:
            result.data_requirements = [ResearchCapability.COMPANY_OPERATIONS]
            result.status = TaskStatus.DEGRADED
        else:
            result.facts_used.extend(f.fact_id for f in found)
        return result
    runner.coordinator.agents['security'] = security
    _, audit, advice = await runner.run(stock_request(), understanding())
    assert advice.compliance.status == 'PASS'
    assert len(advice.stock_recommendation.recommendations) == 2
    assert sorted(code for method, code in provider.calls if method == 'operations') == ['600001', '600002']
    assert len(runs) == 4
    assert audit.recovery_reanalyzed and audit.recovery_phases == ['after_analysis']
    attempts = audit.recovery_attempts[0]['candidate_attempts']
    assert {a['scope'] for a in attempts} == {'600001', '600002'}


@pytest.mark.asyncio
async def test_malformed_model_missing_fields_cannot_crash_post_analysis_recovery():
    provider = Provider()
    req = complete_request()
    advice = agent_advice(req, ())
    advice.agent_results[0].details = {'missing_fields': [{'untrusted': 'field'}]}
    audit = DataAcquisitionResult(recovery_rounds=1, recovery_phase='before_analysis')
    output, result = await recover_research(AutomatedResearchPipeline(provider), req,
                                           Intent.SECURITY_RESEARCH, advice, audit)
    assert output is req and result is audit
    assert not provider.calls
