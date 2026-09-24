"""P0/P1 boundaries: request isolation, provenance, paged history, and staged output."""

import asyncio
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.agents.coordinator import CoordinatorAgent, basic_compliance_check, verify_facts
from backend.app.agents.rule_agents import make_rule_agents
from backend.app.data_provider.iwencai import IwencaiSkillHubProvider
from backend.app.database import Database, ProfileVersionConflict
from backend.app.main import app
from backend.app.models import FactRecord, OrchestrationRequest, UserProfile
from backend.app.services.history import summarise_advice


@pytest.mark.asyncio
async def test_model_metrics_are_request_local_and_cleared_for_early_review(semantic):
    coordinator = CoordinatorAgent(make_rule_agents(), verify_facts, basic_compliance_check, semantic=semantic)
    now = datetime.now(timezone.utc)

    def request(count: int) -> OrchestrationRequest:
        return OrchestrationRequest(
            query="请诊断我的持仓组合",
            profile=UserProfile(risk_level="R3", confirmed=True),
            facts=[FactRecord(
                fact_id=f"F-{count}-{index}", entity=f"基金{index}", field="weight", value=0.1,
                snapshot_time=now, source_id="TEST", quality=0.9,
            ) for index in range(count)],
        )

    one, many = {}, {}
    await asyncio.gather(
        coordinator.run(request(1), metrics_sink=one),
        coordinator.run(request(3), metrics_sink=many),
    )
    assert one["available"] == 1
    assert many["available"] == 3
    stale = {"selected": 99}
    await coordinator.run(
        OrchestrationRequest(query="请诊断我的持仓组合", profile=UserProfile(confirmed=False)),
        metrics_sink=stale,
    )
    assert stale == {}


@pytest.mark.asyncio
async def test_provenance_accepts_public_https_and_survives_history_summary():
    provider = IwencaiSkillHubProvider("test-secret", base_url="https://iwencai.example")
    url = "https://example.com/report/123"
    facts = provider._normalize({"datas": [{"标题": "年度报告", "url": url}]}, entity_hint="测试公司")
    await provider.aclose()
    assert facts and all(fact.source_url == url for fact in facts)
    assert all(fact.field != "url" for fact in facts)
    compact = summarise_advice({"facts": [fact.model_dump(mode="json") for fact in facts]})
    assert compact["facts"][0]["source_url"] == url
    for unsafe in ("javascript:alert(1)", "http://example.com/report", "https://localhost/report", "https://127.0.0.1/report"):
        with pytest.raises(ValidationError):
            FactRecord.model_validate({
                **facts[0].model_dump(), "source_url": unsafe,
            })


def test_history_is_user_scoped_searchable_renameable_and_paged():
    database = Database("sqlite+pysqlite:///:memory:", "test-secret-that-is-longer-than-thirty-two-characters")
    database.initialize()
    alice = int(database.create_user("history-alice", "hash")["id"])
    bob = int(database.create_user("history-bob", "hash")["id"])
    for index in range(25):
        database.save_exchange(alice, "alice-chat", f"问题{index}", {}, f"回答{index}", {})
    database.save_exchange(bob, "bob-chat", "私有问题", {}, "私有回答", {})
    assert database.rename_conversation(alice, "bob-chat", "越权") is False
    assert database.rename_conversation(alice, "alice-chat", "新的研究标题") is True
    assert [row["title"] for row in database.list_conversations(alice, search="研究")] == ["新的研究标题"]
    assert database.list_conversations(bob, search="研究") == []
    latest = database.get_conversation(alice, "alice-chat", limit=20)
    assert latest is not None and latest["has_more"] is True
    assert len(latest["messages"]) == 20
    older = database.get_conversation(alice, "alice-chat", before_id=latest["next_before_id"], limit=20)
    assert older is not None and len(older["messages"]) == 20
    assert older["messages"][-1]["id"] < latest["messages"][0]["id"]
    assert database.get_conversation(bob, "alice-chat") is None
    assert [row["id"] for row in database.list_conversations(alice, limit=1, offset=1)] == []


def test_confirmed_profile_version_cannot_move_backwards():
    database = Database("sqlite+pysqlite:///:memory:", "test-secret-that-is-longer-than-thirty-two-characters")
    database.initialize()
    user_id = int(database.create_user("version-user", "hash")["id"])
    database.save_profile(user_id, {"confirmed": True, "risk_level": "R3"}, 2, expected_version=1)
    with pytest.raises(ProfileVersionConflict):
        database.save_profile(user_id, {"confirmed": True, "risk_level": "R5"}, 2, expected_version=1)
    assert database.get_profile(user_id)["payload"]["risk_level"] == "R3"


def test_stream_only_emits_final_advice_after_progress():
    with TestClient(app) as client:
        response = client.post("/api/v1/portfolio/analyze/stream", json={
            "query": "请诊断我的持仓组合",
            "profile": {"risk_level": "R3", "confirmed": True},
            "auto_fetch": False,
        })
    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[-1]["type"] == "result"
    assert events[-1]["advice"]["compliance"]["status"] in {"PASS", "REVIEW", "BLOCK"}
    assert any(event.get("stage") == "事实核验" for event in events[:-1])
    assert all("advice" not in event for event in events[:-1])
