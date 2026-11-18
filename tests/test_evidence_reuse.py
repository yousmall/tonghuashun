"""Exact reuse preserves source identity, lineage, freshness, and rejection gates."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from io import BytesIO

import httpx
import pytest
import pytest_asyncio
from reportlab.pdfgen import canvas

from backend.app.agents.llm_agents import LLMConfig, OpenAICompatibleLLM
from backend.app.models import FactRecord, OrchestrationRequest
from backend.app.semantic import SemanticService, StockEvidenceReview
from backend.app.services import disclosure_reader as reader
from backend.app.services.model_response_cache import ModelResponseCache
from backend.app.services.model_telemetry import analysis_telemetry


def source(**patch):
    values = dict(fact_id="DOC-1", entity="示例公司", entity_code="600001", field="announcement",
        value="示例公司年度报告", period="2025", snapshot_time=datetime.now(timezone.utc),
        source_id="IWENCAI_SKILLHUB", source_url="https://static.cninfo.com.cn/report.pdf", quality=.9)
    return FactRecord(**{**values, **patch})


@pytest_asyncio.fixture
async def document_cache(monkeypatch):
    downloads = ModelResponseCache(ttl_seconds=0, max_entries=16)
    extracts = ModelResponseCache(ttl_seconds=600, max_entries=32)
    monkeypatch.setattr(reader, "_downloads", downloads)
    monkeypatch.setattr(reader, "_extractions", extracts)
    yield
    await reader.close_document_caches()


def test_pdf_worker_can_extract_using_windows_reload_selector_loop():
    if not hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        pytest.skip("Windows Uvicorn reload uses a selector event loop")
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(20, 100, "Example Company 600001")
    pdf.save()

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        found = runner.run(reader._extract_pdf(buffer.getvalue(), focus="financial"))
    assert "600001" in found["identity"]
    assert found["pages"][0]["page"] == 1


@pytest.mark.asyncio
async def test_pdf_revalidates_bytes_and_rebuilds_lineage_for_each_source(document_cache, monkeypatch):
    extracted, downloads = [], []
    content = [b"%PDF-version-1"]
    async def extract(data):
        extracted.append(data)
        return {"identity": "示例公司 600001", "pages": [{"page": 1, "text": data.decode()}]}
    def transport(req):
        downloads.append(str(req.url))
        return httpx.Response(200, content=content[0])
    monkeypatch.setattr(reader, "_extract_pdf", extract)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        first = source()
        a = await reader.read_disclosure(first, code="600001", name="示例公司", client=client)
        second = first.model_copy(update={"fact_id": "DOC-2", "snapshot_time": first.snapshot_time + timedelta(seconds=1)})
        b = await reader.read_disclosure(second, code="600001", name="示例公司", client=client)
        assert len(downloads) == 2 and len(extracted) == 1
        assert a[0].value == b[0].value and a[0].fact_id != b[0].fact_id
        assert b[0].derived_from == ["DOC-2"] and b[0].snapshot_time == second.snapshot_time
        a[0].value = "caller mutation"
        content[0] = b"%PDF-version-2"
        c = await reader.read_disclosure(second, code="600001", name="示例公司", client=client)
        assert len(extracted) == 2 and c[0].value == "%PDF-version-2"
        wrong = second.model_copy(update={"entity": "另一公司", "entity_code": "600002"})
        assert await reader.read_disclosure(wrong, code="600002", name="另一公司", client=client) == []
        assert len(extracted) == 2  # Identity is checked even on content-cache hits.
        assert await reader.read_disclosure(second.model_copy(update={"source_url": "https://evil.test/a.pdf"}),
            code="600001", name="示例公司", client=client) == []
        assert len(downloads) == 4


@pytest.mark.asyncio
async def test_concurrent_pdf_reads_coalesce_but_one_cancellation_keeps_other_reader(document_cache, monkeypatch):
    calls = []
    started, release = asyncio.Event(), asyncio.Event()
    async def download(url, client=None):
        calls.append(url)
        started.set()
        await release.wait()
        return b"%PDF-shared"
    async def extract(data):
        return {"identity": "示例公司 600001", "pages": [{"page": 1, "text": "原文"}]}
    monkeypatch.setattr(reader, "_download_pdf", download)
    monkeypatch.setattr(reader, "_extract_pdf", extract)
    a = asyncio.create_task(reader.read_disclosure(source(), code="600001", name="示例公司"))
    await started.wait()
    b = asyncio.create_task(reader.read_disclosure(source(fact_id="DOC-2"), code="600001", name="示例公司"))
    await asyncio.sleep(0)
    a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await a
    release.set()
    result = await b
    assert len(calls) == 1 and result[0].derived_from == ["DOC-2"]
    await reader.read_disclosure(source(), code="600001", name="示例公司")
    assert len(calls) == 2  # Sequential reads still retrieve current publisher bytes.


@pytest.mark.asyncio
async def test_empty_pdf_extraction_is_not_cached(document_cache, monkeypatch):
    calls = []
    async def empty(data):
        calls.append(data)
        return {}
    monkeypatch.setattr(reader, "_extract_pdf", empty)
    assert await reader.extract_pdf(b"%PDF-empty") == {}
    assert await reader.extract_pdf(b"%PDF-empty") == {}
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("shared_reader", [False, True])
async def test_evidence_review_releases_expired_loader_only_after_last_reader(shared_reader):
    started, released, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []
    async def respond(req):
        calls.append(req)
        started.set()
        try:
            await finish.wait()
            return httpx.Response(200, json={"choices": [{"message": {"content":
                json.dumps({"confidence": .9, "assessments": [], "matches": []})}}]})
        finally:
            released.set()
    llm = OpenAICompatibleLLM(LLMConfig("https://model.test", "fake", "test", max_retries=0),
                              transport=httpx.MockTransport(respond))
    payload = {"required_schema": StockEvidenceReview.model_json_schema(), "authorized_facts": [],
               "targets": [], "conditions": []}
    token = analysis_telemetry.set({"calls": [], "cache_scope": "test-account", "mode": "deep"})
    try:
        first = asyncio.create_task(llm.complete_json(system="test", payload=payload))
        await asyncio.wait_for(started.wait(), timeout=1)
        second = asyncio.create_task(llm.complete_json(system="test", payload=payload)) if shared_reader else None
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        if second is not None:
            assert not released.is_set()
            finish.set()
            assert (await second)["confidence"] == .9
            assert len(calls) == 1
        else:
            await asyncio.wait_for(released.wait(), timeout=1)
            assert not llm._responses.pending and not llm._responses.entries
            finish.set()
            assert (await llm.complete_json(system="test", payload=payload))["confidence"] == .9
            assert len(calls) == 2
    finally:
        analysis_telemetry.reset(token)
        await llm.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, "reference", "quote", "label", "entity", "incomplete_rubric"])
async def test_only_quote_validated_stock_assessments_are_cached(invalid):
    calls = []
    def respond(req):
        calls.append(req)
        payload = json.loads(req.content)["messages"][1]["content"]
        payload = json.loads(payload)
        fact = payload["authorized_facts"][0]
        item = {"criterion": "event", "label": "favorable", "evidence_id": fact["fact_id"], "quote": "完成股份回购"}
        entity = fact["entity"]
        if invalid == "reference": item["evidence_id"] = "UNAUTHORIZED"
        if invalid == "quote": item["quote"] = "虚构内容"
        if invalid == "label": item["label"] = "always_safe"
        if invalid == "entity": entity = "另一公司"
        raw = {"confidence": .9, "assessments": [{"entity": entity, "dimension": "event", "complete": True,
            "items": [] if invalid == "incomplete_rubric" else [item]}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(raw)}}]})
    llm = OpenAICompatibleLLM(LLMConfig("https://model.test", "fake", "test", max_retries=0),
                              transport=httpx.MockTransport(respond))
    semantic = SemanticService(llm)
    request = OrchestrationRequest(query="评估事件", profile={"confirmed": True, "risk_level": "R3"})
    fact = source(value="公司完成股份回购。")
    targets = [{"entity": fact.entity, "dimension": "event"}]
    token = analysis_telemetry.set({"calls": [], "cache_scope": "test-account", "mode": "deep"})
    try:
        first = await semantic.assess_stock_evidence(request, [fact], targets, [])
        second = await semantic.assess_stock_evidence(request, [fact], targets, [])
        assert first == second and len(calls) == (1 if invalid is None else 2)
        if invalid is None:
            for changed in [fact.model_copy(update={"value": "公司完成股份回购，并发布更新。"}),
                            fact.model_copy(update={"source_url": "https://static.cninfo.com.cn/revised.pdf"}),
                            fact.model_copy(update={"snapshot_time": fact.snapshot_time + timedelta(seconds=1)})]:
                await semantic.assess_stock_evidence(request, [changed], targets, [])
            await semantic.assess_stock_evidence(request.model_copy(update={
                "profile": request.profile.model_copy(update={"risk_level": "R2"})}), [fact], targets, [])
            assert len(calls) == 5
    finally:
        analysis_telemetry.reset(token)
        await llm.aclose()
