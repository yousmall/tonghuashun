"""汇总报告与错误尺寸元数据回归；所有数据均为测试构造。"""
import base64
from io import BytesIO
import re
from zipfile import ZipFile, ZIP_DEFLATED

import pytest
from openpyxl import Workbook
from streamlit.testing.v1 import AppTest

from backend.app.models import FactRecord, OrchestrationRequest
from backend.app.models.trade_history import TradeHistoryUpload
from backend.app.services.instant_research import InstantResearchService
from backend.app.services.local_consultation import select_scope
from backend.app.services.trade_history import inspect_trade_workbook, analyze_trade_history


def report_upload(*, bad_dimensions=True, monthly_count=8, total_cost=15):
    book = Workbook()
    summary = book.active
    summary.title = "分析摘要"
    for row in [
        ["股票交易行为与投资偏好分析报告"],
        ["数据来源：同一用户，期间2025-09-01至2026-09-30"], [],
        ["① 交易风格", "测试短线交易偏好"], [],
        ["指标", "数值", "单位/说明"], ["总成交笔数", "10", "笔"],
        ["涉及股票数", "2", "只"], ["交易日数", "5", "天"],
        ["个股持仓时间中位数", "16", "天，首末交易日差"],
    ]:
        summary.append(row)
    behavior = book.create_sheet("交易行为")
    for row in [["交易行为特征明细"], [], ["月度成交活跃度"], ["月份", "成交笔数", "占比"],
                ["2025-09", monthly_count, "=B5/SUM(B5:B6)"], ["2026-09", 2, "=B6/SUM(B5:B6)"], [],
                ["交易板块分布"], ["板块", "成交金额(元)", "笔数"], ["沪市主板", 1000, 6], ["创业板", 500, 4]]:
        behavior.append(row)
    preference = book.create_sheet("投资偏好")
    for row in [["投资偏好分析"], [], ["题材偏好"], ["题材方向", "估算成交金额(元)", "占比"],
                ["测试科技", 1000, "=B5/SUM(B5:B6)"], ["其他", 500, "=B6/SUM(B5:B6)"], [],
                ["个股成交额排名"], ["股票", "成交金额(元)", "成交笔数"], ["测试甲", 1000, 6], ["测试乙", 500, 4]]:
        preference.append(row)
    cost = book.create_sheet("成本与说明")
    for row in [["交易成本与数据说明"], [], ["交易成本结构"], ["项目", "金额(元)", "说明"],
                ["佣金", 10, "双边"], ["印花税", 5, "卖出"], ["合计", total_cost], [],
                ["1. 持仓跨度使用首末交易日差。"]]:
        cost.append(row)
    stream = BytesIO()
    book.save(stream)
    raw = stream.getvalue()
    if bad_dimensions:
        fixed = BytesIO()
        with ZipFile(BytesIO(raw)) as source, ZipFile(fixed, "w", ZIP_DEFLATED) as target:
            for item in source.infolist():
                data = source.read(item.filename)
                if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", item.filename):
                    data = re.sub(rb'<dimension ref="[^"]+"', b'<dimension ref="A1:A1"', data)
                target.writestr(item, data)
        raw = fixed.getvalue()
    return TradeHistoryUpload(file_name="测试分析报告.xlsx", content_base64=base64.b64encode(raw).decode())


def test_report_reads_all_sheets_despite_incorrect_dimensions_and_keeps_metric_meanings():
    request = report_upload()
    inspection = inspect_trade_workbook(request)
    assert inspection["input_kind"] == "analysis_report"
    result = analyze_trade_history(request)
    assert result["trade_count"] == 10 and result["security_count"] == 2
    assert result["active_days"] == 5 and result["reported_median_holding_days"] == 16
    assert result["average_matched_holding_days"] is None and result["matched_sell_gross_pnl"] is None
    assert result["window_start"] == "2025-09-01" and result["window_end"] == "2026-09-30"
    assert any("395 天" in text for text in result["limitations"])
    assert len(result["monthly"]) == 2 and len(result["report_sections"]) == 5
    assert result["total_cost"] == 15
    assert all(check["matches"] for check in result["reconciliation"])
    assert result["report_metrics"][0]["row"] == 7
    assert result["report_metrics"][0]["origin"] == "reported"
    assert result["source_notes"][0]["sheet"] == "成本与说明"
    assert result["report_sections"][0]["rows"][0]["detail"] == "公式未执行"
    assert result["report_sections"][0]["rows"][0]["calculated_share"] == .8


def test_report_exposes_conflicting_totals_instead_of_silent_correction():
    result = analyze_trade_history(report_upload(monthly_count=7, total_cost=999))
    assert result["trade_count"] == 10
    assert result["total_cost"] == 15
    assert sum(not check["matches"] for check in result["reconciliation"]) == 2
    assert sum("数据不一致" in text for text in result["limitations"]) == 2


def test_report_ui_reads_previews_analyzes_and_saves_without_matching_transaction_columns():
    content = report_upload().content_base64
    script = f'''
import base64
from io import BytesIO
from unittest.mock import patch
import streamlit as st
from frontend import streamlit_app as ui
from frontend.trade_profile import render_trade_profile
from backend.app.models.trade_history import TradeHistoryUpload
from backend.app.services.trade_history import inspect_trade_workbook, analyze_trade_history
ui.init_session()
file = BytesIO(base64.b64decode({content!r}))
file.name = "测试分析报告.xlsx"
def api(base, method, path, payload):
    request = TradeHistoryUpload(**payload)
    if path.endswith('/inspect'):
        return inspect_trade_workbook(request)
    result = analyze_trade_history(request)
    if path.endswith('/analyze'):
        return result
    result['confirmed'] = True
    st.session_state.did_save = True
    return dict(st.session_state.profile, trading_analysis=result, version=2)
with patch('frontend.trade_profile.st.file_uploader', return_value=file):
    render_trade_profile('http://offline', api_request=api)
'''
    app = AppTest.from_string(script).run(timeout=15)
    next(b for b in app.button if b.label == "读取 Excel").click().run(timeout=15)
    assert not app.exception and app.metric[0].value == "10 笔"
    assert not app.selectbox and not app.date_input
    assert len(app.dataframe) == 6
    next(b for b in app.button if b.label == "分析交易画像").click().run(timeout=15)
    assert not app.exception
    next(b for b in app.button if b.label == "确认并保存交易画像").click().run(timeout=15)
    assert not app.exception and app.session_state['did_save']
    assert app.session_state['profile']['trading_analysis']['input_kind'] == 'analysis_report'
    assert not app.session_state['profile']['confirmed']


@pytest.mark.asyncio
@pytest.mark.parametrize('query', ['分析下茅台', '帮我看看茅台', '个股研究：分析下茅台', '看看600519.SH'])
async def test_colloquial_security_question_matches_alias_and_keeps_other_stock_out(query):
    from datetime import datetime, timezone
    rows = [FactRecord(fact_id=code, entity=name, entity_code=code+'.SH', field='pe_ttm', value=value,
                       unit='倍', source_id='SYNTHETIC_TEST_ONLY', quality=.95, snapshot_time=datetime.now(timezone.utc))
            for name, code, value in [('贵州茅台', '600519', 20), ('招商银行', '600036', 999)]]
    request = OrchestrationRequest(query=query, research_mode='instant', facts=rows,
                                   profile={'confirmed': True, 'risk_level': 'R3'})
    scope = select_scope(request)
    assert scope.intent.value == 'security_research'
    prepared, _, advice = await InstantResearchService().run(request, scope)
    assert advice.response_scope == 'evidence_review'
    assert '贵州茅台' in advice.conclusion and '招商银行' not in advice.conclusion
    assert {f.entity for f in prepared.facts} == {'贵州茅台'}
    assert {f.entity_code for f in prepared.facts if f.entity_code} == {'600519.SH'}


@pytest.mark.asyncio
async def test_recognized_stock_without_evidence_returns_target_and_retry_instruction():
    request = OrchestrationRequest(query='分析下茅台', research_mode='instant',
                                   profile={'confirmed': True, 'risk_level': 'R3'})
    _, _, advice = await InstantResearchService().run(request, select_scope(request))
    assert advice.response_scope == 'clarification' and not advice.evidence
    assert '600519' in advice.conclusion and '补查资料并分析' in advice.conclusion
    assert '证据研判支持已有资料概览' not in advice.conclusion


def test_summary_report_api_confirmation_keeps_questionnaire_and_version_rules(monkeypatch, request, risk_questionnaire_payload):
    from fastapi.testclient import TestClient
    from backend.app import main
    from backend.app.database import Database
    from backend.app.session_pool import SessionThreadPool
    database = Database("sqlite+pysqlite:///:memory:", "test-secret-that-is-longer-than-thirty-two-characters")
    database.initialize()
    monkeypatch.setattr(main, 'database', database)
    pool = SessionThreadPool()
    request.addfinalizer(pool.shutdown)
    monkeypatch.setattr(main, 'session_thread_pool', pool)
    payload = report_upload().model_dump(mode='json')
    with TestClient(main.app) as client:
        assert client.post('/api/v1/profile/trades/inspect', json=payload).status_code == 401
        login = client.post('/api/v1/auth/register', json={'username': 'report-test-only', 'password': 'Report-test-only-123'}).json()
        headers = {'Authorization': "Bearer " + login['access_token']}
        draft = client.post('/api/v1/profile/assess', headers=headers, json=risk_questionnaire_payload).json()['profile']
        profile = client.post('/api/v1/profile/confirm', headers=headers, json={'profile': draft}).json()
        payload['expected_version'] = profile['version']
        preview = client.post('/api/v1/profile/trades/inspect', headers=headers, json=payload)
        assert preview.status_code == 200 and preview.json()['input_kind'] == 'analysis_report'
        response = client.post('/api/v1/profile/trades/confirm', headers=headers, json=payload)
        assert response.status_code == 200, response.text
        saved = response.json()
        for field in ('risk_level', 'risk_answers', 'risk_score', 'valid_until', 'confirmed'):
            assert saved[field] == profile[field]
        assert saved['version'] == profile['version'] + 1
        assert saved['trading_analysis']['schema_version'] == 'stock-report-v1'
        assert saved['trading_analysis']['confirmed']
        assert client.post('/api/v1/profile/trades/confirm', headers=headers, json=payload).status_code == 409
        restored = client.get('/api/v1/profile', headers=headers).json()['profile']
        assert restored['trading_analysis'] == saved['trading_analysis']


def test_missing_evidence_action_retries_same_question_in_deep_mode():
    script = '''
from unittest.mock import patch
import streamlit as st
from frontend import streamlit_app as ui
ui.init_session()
st.session_state.profile.update(confirmed=True, risk_level='R3')
st.session_state.conversation = [
    {'role': 'user', 'content': '个股研究：分析下茅台'},
    {'role': 'assistant', 'content': '缺少资料', 'payload': {
        'response_scope': 'clarification', 'intent': 'security_research', 'research_mode': 'instant'}}]
from frontend.conversation_logic import chain_key
st.session_state[chain_key(st.session_state.conversation_id)] = True
def retry(base, direction, query):
    st.session_state.retry_request = {'query': query, 'mode': st.session_state.research_mode}
with patch.object(ui, 'render_page_header'), patch.object(ui, 'render_research_guide'), \
     patch.object(ui, 'render_research_board'), patch.object(ui, 'render_prepared_stock_report'), \
     patch.object(ui, 'render_advice'), patch.object(ui, 'restore_board_scroll'), \
     patch.object(ui, 'submit_chat_analysis', side_effect=retry):
    ui.render_research_page('http://offline', '个股研究')
'''
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    next(b for b in app.button if b.label == '补查资料并分析').click().run(timeout=15)
    assert not app.exception
    assert app.session_state['retry_request'] == {'query': '个股研究：分析下茅台', 'mode': 'deep'}
