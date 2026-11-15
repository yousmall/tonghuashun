"""追问省略标的时的上下文补齐：只沿用已加载资料里的标的，不跨领域串联。"""

from datetime import datetime, timezone

from backend.app.models import FactRecord, OrchestrationRequest
from backend.app.services.context_link import carry_context_entity


def fact(fact_id: str, entity: str, field: str, value, *, code: str | None = None) -> FactRecord:
    return FactRecord(
        fact_id=fact_id, entity=entity, field=field, value=value,
        source_id="IWENCAI_SKILLHUB", snapshot_time=datetime.now(timezone.utc),
        quality=.95, entity_code=code,
    )


def turn(role: str, content: str) -> dict:
    return {"role": role, "content": content, "created_at": "2026-11-15T08:00:00Z"}


FACTS = [
    fact("f1", "贵州茅台", "pe_ttm", 21.84, code="600519.SH"),
    fact("f2", "贵州茅台", "pb", 6.62, code="600519.SH"),
    fact("f3", "五粮液", "pb", 3.10, code="000858.SZ"),
]


def request(query: str, context: list[dict]) -> OrchestrationRequest:
    return OrchestrationRequest(
        query=query, research_direction="个股研究", profile={"user_id": "test", "confirmed": True, "risk_level": "R3"},
        facts=FACTS, context_messages=context,
    )


def test_follow_up_without_entity_reuses_previous_stock():
    context = [turn("user", "个股研究：600519的市盈率是多少"), turn("assistant", "贵州茅台的市盈率 TTM 约 21.8 倍。")]
    assert carry_context_entity(request("它的市净率是多少", context), FACTS) == "贵州茅台"


def test_follow_up_keeps_working_across_multiple_turns():
    context = [
        turn("user", "个股研究：600519的市盈率是多少"),
        turn("assistant", "贵州茅台的市盈率 TTM 约 21.8 倍。"),
        turn("user", "个股研究：它的市净率是多少"),
        turn("assistant", "贵州茅台的市净率约 6.6 倍。"),
    ]
    assert carry_context_entity(request("那它的ROE呢", context), FACTS) == "贵州茅台"


def test_other_scope_question_does_not_reuse_stock():
    context = [turn("user", "个股研究：600519的市盈率是多少")]
    assert carry_context_entity(request("现在市场怎么样", context), FACTS) is None


def test_no_context_means_no_guess():
    assert carry_context_entity(request("它的市净率是多少", []), FACTS) is None


def test_previous_turn_naming_another_stock_wins():
    context = [turn("user", "个股研究：000858的市净率是多少")]
    assert carry_context_entity(request("它的市净率呢", context), FACTS) == "五粮液"
