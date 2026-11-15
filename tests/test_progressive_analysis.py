"""Progressive results, account isolation and real cache reuse (offline)."""
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from copy import deepcopy
from time import monotonic

import pytest
import streamlit as st

from frontend import streamlit_app as ui
from frontend.api_client import ApiResult
from backend.app import main
from backend.app.models import FactRecord
from backend.app.services.derived_score_cache import DerivedScoreCache
from backend.app.services.model_telemetry import analysis_telemetry
from backend.app.services.research import derive_scoring_facts
from scripts.verify_consultation_latency import synthetic_request


class State(dict):
    def __getattr__(self, key):
        return self[key]

    def __setattr__(self, key, value):
        self[key] = value


class Queue:
    def __init__(self):
        self.calls = []
        self.future = Future()

    def submit(self, *args):
        self.calls.append(deepcopy(args[2:-1]))
        return self.future


@pytest.fixture
def staged(monkeypatch):
    state = State(profile={"confirmed": True, "version": 1}, facts=[], portfolio=[],
        auth_token="owner-token", conversation_id="origin", conversation=[],
        active_research_direction="个股研究", research_mode="progressive", research_sessions={})
    preview, deep = Queue(), Queue()
    monkeypatch.setattr(st, "session_state", state)
    monkeypatch.setattr(ui, "profile_ready", lambda: True)
    monkeypatch.setattr(ui, "backend_http_client", lambda base: object())
    monkeypatch.setattr(ui, "preview_executor", lambda: preview)
    monkeypatch.setattr(ui, "analysis_executor", lambda: deep)
    monkeypatch.setattr(ui, "save_research_session", lambda: None)
    monkeypatch.setattr(ui, "invalidate_recent_conversations", lambda *a: None)
    monkeypatch.setattr(ui, "current_browser_session_id", lambda: None)
    ui.submit_chat_analysis("http://test", "个股研究", "请分析600519")
    return state, preview, deep


def answer(text):
    return ApiResult(data={"conclusion": text, "facts": [], "response_scope": "snapshot",
                           "compliance": {"status": "REVIEW"}})


def test_preview_is_visible_while_deep_pending_and_final_replaces_it(staged):
    state, preview, deep = staged
    assert preview.calls[0][3]["research_mode"] == "lite"
    assert preview.calls[0][3]["preview_only"] is True
    assert preview.calls[0][3]["auto_fetch"] is False
    assert deep.calls[0][3]["research_mode"] == "deep"
    assert deep.calls[0][3]["auto_fetch"] is True
    preview.future.set_result(answer("概要"))
    assert ui.finish_chat_analyses()
    assert len(state.conversation) == 2 and state.conversation[-1]["phase"] == "preview"
    assert state.analysis_jobs and not deep.future.done()
    assert not ui.finish_chat_analyses()  # polling never duplicates a preview
    deep.future.set_result(answer("完整报告"))
    ui.finish_chat_analyses()
    assert len(state.conversation) == 2
    assert state.conversation[-1]["content"] == "完整报告"
    assert state.conversation[-1]["phase"] == "final" and not state.analysis_jobs


def test_deep_failure_preserves_labelled_preview(staged):
    state, preview, deep = staged
    preview.future.set_result(answer("概要"))
    ui.finish_chat_analyses()
    deep.future.set_result(ApiResult(status=504, detail="完整分析尚未完成"))
    ui.finish_chat_analyses()
    assert state.advice["conclusion"] == "概要"
    assert state.conversation[-1]["phase"] == "preview_failed"
    assert not state.analysis_jobs


def test_final_can_finish_before_preview_without_late_overwrite(staged):
    state, preview, deep = staged
    deep.future.set_result(answer("完整报告"))
    ui.finish_chat_analyses()
    assert preview.future.cancelled()
    assert state.conversation[-1]["content"] == "完整报告"
    assert not ui.finish_chat_analyses()


def test_both_stages_return_only_to_originating_conversation(staged):
    state, preview, deep = staged
    saved = {"conversation_id": "origin", "facts": [], "conversation": list(state.conversation)}
    state.research_sessions["个股研究"] = saved
    state.conversation_id, state.active_research_direction = "new", "行业分析"
    state.conversation = []
    preview.future.set_result(answer("概要"))
    ui.finish_chat_analyses()
    deep.future.set_result(answer("完整报告"))
    ui.finish_chat_analyses()
    assert not state.conversation
    assert len(saved["conversation"]) == 2
    assert saved["conversation"][-1]["content"] == "完整报告"


def test_logout_discards_both_stages_and_late_old_account_results(staged):
    state, preview, deep = staged
    deep.future.set_running_or_notify_cancel()
    ui.reset_user_session()
    deep.future.set_result(answer("旧账户报告"))
    assert preview.future.cancelled()
    assert not ui.finish_chat_analyses()
    assert "advice" not in state and "analysis_jobs" not in state


def test_only_current_target_board_is_reused_in_deep_mode(monkeypatch):
    state = State(facts=[], research_mode="deep", board_target_stock="600036", research_board_cache={})
    for target in ("600519", "600036"):
        state.research_board_cache[("base", "stock", target)] = {"result": {"sections": [
            {"status": "ok", "facts": [{"fact_id": target}]},
            {"status": "loading", "facts": [{"fact_id": "pending"}]}]}}
    monkeypatch.setattr(st, "session_state", state)
    assert ui.consultation_facts("base", "个股研究") == [{"fact_id": "600036"}]


def test_exact_input_skips_calculation_and_invalidates_changes_scope_and_expiry():
    cache = DerivedScoreCache(max_entries=2)
    now = datetime.now(timezone.utc)
    root = FactRecord(fact_id="roe", entity="甲", field="roe", value=10, unit="percent",
                      snapshot_time=now, source_id="TEST_SOURCE", quality=1)
    calls = []
    def calculate(facts, *, now):
        calls.append(1)
        return derive_scoring_facts(facts, now=now)
    token = analysis_telemetry.set({"cache_scope": "owner"})
    try:
        first = cache.derive([root], now, calculate)
        repeat = cache.derive([root], now + timedelta(seconds=5), calculate)
        assert len(calls) == 1 and first == repeat
        repeat[0].value = -1
        assert cache.derive([root], now, calculate)[0].value != -1
        cache.derive([root.model_copy(update={"value": 20})], now, calculate)
        assert len(calls) == 2
        analysis_telemetry.set({"cache_scope": "other"})
        cache.derive([root], now, calculate)
        assert len(calls) == 3
        assert not cache.derive([root], now + timedelta(days=91), calculate)
        assert len(calls) == 4 and len(cache.inputs) <= 2
    finally:
        analysis_telemetry.reset(token)


@pytest.mark.asyncio
async def test_transient_preview_checks_server_profile_but_does_not_save_history(monkeypatch):
    req = synthetic_request().model_copy(update={"research_mode": "lite", "preview_only": True})
    monkeypatch.setattr(main.database, "get_profile", lambda uid: {
        "version": req.profile.version, "payload": req.profile.model_dump(mode="json")})
    monkeypatch.setattr(main.database, "save_exchange", lambda *a: pytest.fail("Preview must not duplicate history"))
    with ThreadPoolExecutor(max_workers=1) as pool:
        class SessionPool:
            def submit(self, sid, func, *args):
                return pool.submit(func, *args)
        monkeypatch.setattr(main, "session_thread_pool", SessionPool())
        result = await main.analyze_portfolio(req, {"id": 7, "_session_id": "preview-test"})
    assert result.response_scope == "snapshot" and result.history_status == "not_requested"
    assert result.compliance.status.value == "REVIEW"


@pytest.mark.asyncio
async def test_overview_warms_calculation_without_exposing_derived_advice(monkeypatch):
    from backend.app.models.schemas import DataOverviewRequest, DataOverviewResponse, DataOverviewSection
    root = FactRecord(fact_id="pe", entity="甲", field="pe_ttm", value=10,
                      snapshot_time=datetime.now(timezone.utc), source_id="TEST_SOURCE", quality=1)
    response = DataOverviewResponse(direction="stock", target="600519", status="ok",
        fetched_at=datetime.now(timezone.utc), sections=[DataOverviewSection(key="overview", title="行情", status="ok", facts=[root])])
    async def get(*a):
        return response
    monkeypatch.setattr(main.overview_cache, "get", get)
    calls = []
    monkeypatch.setattr(main.research_pipeline, "_derive", lambda facts: calls.append((analysis_telemetry.get()["cache_scope"], facts)))
    result = await main.research_data_overview(DataOverviewRequest(direction="stock"), {"id": 7})
    assert calls == [("7", [root])]
    assert result.sections[0].facts == [root]


@pytest.mark.asyncio
async def test_prefetched_financial_batch_avoids_second_provider_call():
    from backend.app.services.market_overview import scoped_snapshot
    from backend.app.services.research import AutomatedResearchPipeline, DataCall
    from backend.app.models import Intent
    calls = []
    class Provider:
        async def get_financial_metrics(self, target):
            calls.append(target)
            return [FactRecord(fact_id="roe", entity=target, field="roe", value=10, unit="percent",
                snapshot_time=datetime.now(timezone.utc), source_id="TEST_SOURCE", quality=1)]
    provider = Provider()
    facts = await scoped_snapshot(provider, "get_financial_metrics", "600519")
    req = synthetic_request().model_copy(update={"facts": facts, "auto_fetch": True, "query": "600519"})
    _, acquisition = await AutomatedResearchPipeline(provider).prepare(req, Intent.SECURITY_RESEARCH,
        target="600519", planned_calls=[DataCall("financial", "get_financial_metrics", ("600519",), "get_financial_metrics@600519")])
    assert calls == ["600519"]
    assert acquisition.reused_capabilities == ["financial"]


def test_page_defaults_to_instant_and_delivers_one_final_answer():
    from streamlit.testing.v1 import AppTest
    script = """
from concurrent.futures import Future
from unittest.mock import patch
import streamlit as st
from frontend import streamlit_app as ui
ui.init_session()
st.session_state.profile['confirmed'] = True
st.session_state.auth_token = 'ui-test'
preview = st.session_state.setdefault('preview_test_future', Future())
deep = st.session_state.setdefault('deep_test_future', Future())
class Executor:
    def __init__(self, future):
        self.future = future
    def submit(self, *args):
        return self.future
with patch.object(ui, 'profile_ready', return_value=True), patch.object(ui, 'preview_executor', return_value=Executor(preview)), patch.object(ui, 'analysis_executor', return_value=Executor(deep)), patch.object(ui, 'backend_http_client', return_value=object()), patch.object(ui, 'render_research_board'), patch.object(ui, 'invalidate_recent_conversations'):
    ui.finish_chat_analyses()
    ui.page_questions('http://test')
"""
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    assert next(item for item in app.segmented_control if item.label == "研究模式").value == "instant"
    app.chat_input[0].set_value("分析市场").run(timeout=15)
    assert not app.exception
    app.session_state['preview_test_future'].set_result(answer("已核验证据研判"))
    app.run(timeout=15)
    assert not app.exception
    assert len(app.session_state['conversation']) == 2
    assert app.session_state['conversation'][-1]['content'] == "已核验证据研判"
    assert app.session_state['conversation'][-1]['phase'] == "final"
    assert not app.session_state['analysis_jobs']
    assert not app.session_state['deep_test_future'].done()
    assert not any("详细报告正在后台分析" in item.value for item in app.caption)
