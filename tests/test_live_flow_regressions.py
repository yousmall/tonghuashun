"""真实流程发现的证据状态与证券误匹配回归；不访问外部服务。"""
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from backend.app.agents.coordinator import cross_validate_results
from backend.app.data_provider.iwencai import IwencaiSkillHubProvider
from backend.app.models import AgentResult, FactRecord, TaskStatus
from backend.app.services.research import AutomatedResearchPipeline, DataCall


def fact(field="pe_ttm", value=7):
    return FactRecord(fact_id="check-evidence",entity="招商银行",field=field,value=value,
                      snapshot_time=datetime.now(timezone.utc),source_id="TEST",quality=.9)


def test_degraded_analysis_with_valid_citations_still_requires_review():
    evidence=fact()
    result=AgentResult(agent_id="industry",status=TaskStatus.DEGRADED,opinion="部分可用，但景气度尚缺",
                       confidence=.3,facts_used=[evidence.fact_id],details={"missing_fields":["prosperity_score"]})
    checked=cross_validate_results([result],[evidence])
    assert checked.status=="REVIEW"
    assert "INCOMPLETE_ANALYSIS" in {issue.code for issue in checked.issues}


@pytest.mark.parametrize("channel",[None,"news"])
def test_explicit_code_discards_other_security_table_but_preserves_news_context(channel):
    provider=IwencaiSkillHubProvider("test-only")
    result=provider._normalize({"data":[{"股票代码":"110098.SH","股票简称":"南药转债","最新价":130}]},
                               entity_hint="南银转债113050",channel=channel)
    assert bool(result)==(channel=="news")
    assert not any(item.field=="provider_response" for item in result)


def test_explicit_multiple_codes_keep_only_requested_security_rows():
    provider=IwencaiSkillHubProvider("test-only")
    result=provider._normalize({"data":[
        {"基金代码":"510300.SH","基金简称":"沪深300ETF","单位净值":4},
        {"基金代码":"510500.SH","基金简称":"中证500ETF","单位净值":6},
        {"基金代码":"588000.SH","基金简称":"科创50ETF","单位净值":1}]},
        entity_hint="比较510300与510500")
    assert {item.entity_code for item in result}=={"510300.SH","510500.SH"}


@pytest.mark.asyncio
async def test_scalar_provider_metadata_is_not_reported_as_successful_evidence():
    async def get_quote(target):return [fact("provider_response",2495)]
    pipeline=AutomatedResearchPipeline(SimpleNamespace(get_quote=get_quote))
    result=await pipeline._execute(DataCall(label="quote",method="get_quote",args=("600036",),key="quote@600036"))
    assert result==[]
