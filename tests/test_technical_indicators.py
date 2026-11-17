"""Formula, data-contract, API and front-end regressions; all data is synthetic."""
import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from backend.app.data_provider.iwencai import IwencaiSkillHubProvider, _history_facts
from backend.app.models import FactRecord
from backend.app.services.technical_indicators import calculate_indicators, history_bars
from backend.app.services.trend_analytics import analyze_trend


def bars(values=None, count=60):
    values = values if values is not None else [10 + i / 10 + (i % 5) / 3 for i in range(count)]
    return [{'date': date(2026, 6, 1) + timedelta(days=i), 'value': value,
             'high': value + 1, 'low': value - 1, 'volume': 100 + i * 10, 'volume_unit': '股'}
            for i, value in enumerate(values)]


def test_macd_matches_independent_pandas_ema_and_explicit_china_histogram():
    points = bars()
    result = calculate_indicators(points)['macd']
    close = pd.Series([p['value'] for p in points])
    dif = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    assert result.available
    assert result.latest['dif'] == pytest.approx(dif.iloc[-1], abs=1e-6)
    assert result.latest['dea'] == pytest.approx(dea.iloc[-1], abs=1e-6)
    assert result.latest['histogram'] == pytest.approx(2 * (dif.iloc[-1] - dea.iloc[-1]), abs=1e-6)
    assert all(p.values['dif'] is None for p in result.series[:33])
    assert not calculate_indicators(points[:33])['macd'].available


def test_rsi_matches_wilder_reference_example_and_flat_boundaries():
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
              45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00]
    rsi = calculate_indicators(bars(closes))['rsi']
    assert rsi.series[14].values['rsi'] == pytest.approx(70.464135, abs=1e-6)
    assert rsi.latest['rsi'] == pytest.approx(66.249619, abs=1e-6)
    for values, expected in [([10] * 15, 50), (list(range(10, 25)), 100), (list(range(25, 10, -1)), 0)]:
        assert calculate_indicators(bars(values))['rsi'].latest['rsi'] == expected


def test_boll_uses_population_sigma_and_correct_warmup():
    result = calculate_indicators(bars(list(range(2, 22))))['boll']
    assert result.latest['middle'] == 11.5
    assert result.latest['upper'] == pytest.approx(11.5 + 2 * (33.25 ** .5), abs=1e-6)
    assert result.latest['lower'] == pytest.approx(11.5 - 2 * (33.25 ** .5), abs=1e-6)
    assert all(point.values['middle'] is None for point in result.series[:19])


def test_kdj_atr_flat_and_gap_true_range():
    flat = calculate_indicators(bars([10] * 20))
    assert flat['kdj'].latest == {'k': 50, 'd': 50, 'j': 50}
    assert flat['atr'].latest == {'atr': 2}
    points = bars([10] * 14 + [20, 20])
    result = calculate_indicators(points)['atr']
    assert result.series[14].values['atr'] == pytest.approx(37 / 14, abs=1e-6)
    assert result.latest['atr'] == pytest.approx(((37 / 14) * 13 + 2) / 14, abs=1e-6)
    assert all(point.values['atr'] is None for point in result.series[:14])


def test_kdj_zero_range_and_j_is_not_clipped():
    points = bars([10] * 9)
    for point in points:
        point['high'] = point['low'] = 10
    assert calculate_indicators(points)['kdj'].latest == {'k': 50, 'd': 50, 'j': 50}
    points = bars([10] * 8 + [20] * 8)
    for point in points:
        point['high'] = point['low'] = point['value']
    result = calculate_indicators(points)['kdj']
    assert any((point.values['j'] or 0) > 100 for point in result.series)


def test_missing_high_low_rewarms_without_stale_latest_reading():
    points = bars()
    points[-1]['high'] = None
    result = calculate_indicators(points)
    assert not result['kdj'].available and not result['atr'].available
    assert result['kdj'].latest == {} and result['atr'].latest_date is None
    assert result['macd'].available and result['rsi'].available and result['boll'].available
    points[30]['high'] = None
    points[-1]['high'] = points[-1]['value'] + 1
    result = calculate_indicators(points)
    assert result['kdj'].available and result['atr'].available
    assert '重新预热' in result['atr'].message


def test_obv_uses_signed_volume_keeps_flat_day_and_zero_volume():
    points = bars([10, 11, 11, 9])
    for point, volume in zip(points, [100, 200, 300, 400]):
        point['volume'] = volume
    result = calculate_indicators(points)['obv']
    assert [p.values['obv'] for p in result.series] == [0, 200, 200, -200]
    points[1]['volume'] = 0
    assert calculate_indicators(points)['obv'].latest['obv'] == -400
    points[1]['volume'] = None
    assert not calculate_indicators(points)['obv'].available


@pytest.mark.parametrize('unit', [None, '元', '手'])
def test_obv_rejects_unknown_turnover_or_mixed_units(unit):
    points = bars()
    points[10]['volume_unit'] = unit
    assert not calculate_indicators(points)['obv'].available


def test_normalization_sorts_deduplicates_rejects_conflicts_and_invalid_hlc():
    points = bars(count=5)
    duplicate = dict(points[1], high=points[1]['high'] + 1)
    invalid = dict(points[3], low=points[3]['value'] + 1)
    clean = history_bars(list(reversed(points)) + [duplicate, invalid])
    assert [row['date'] for row in clean] == sorted(p['date'] for p in points)
    assert 'high' not in clean[1]
    assert 'low' not in clean[3]
    assert len(history_bars(points + [dict(points[2], value=99)])) == 4
    points[0].update(volume=1, volume_unit='万股')
    assert history_bars(points)[0]['volume'] == 10000


def test_indicators_are_causal_and_support_generators():
    points = bars()
    full = calculate_indicators(points)
    prefix = calculate_indicators(points[:45])
    for name in full:
        assert full[name].series[:45] == prefix[name].series
    assert analyze_trend(iter(points)).indicators['macd'].available
    assert set(analyze_trend([]).indicators) == {'macd', 'rsi', 'kdj', 'boll', 'atr', 'obv'}


def test_ohlcv_parser_joins_dates_and_preserves_units_and_zero():
    payload = {'data': [
        {'股票代码': '600519.SH', '交易日期': '2026-06-01', '收盘价': 10, '最高价': 11,
         '最低价': 9, '成交量(万股)': 0, '成交额': 99999},
        {'股票代码': '600519.SH', '收盘价[20260602]': 12, '最高价[20260602]': 13,
         '最低价[20260602]': 11, '成交量[20260602](股)': '10,000'},
        {'股票代码': '000001.SZ', '收盘价[20260602]': 90},
        {'股票代码': '600519.SH', '收盘价[20990603]': 90},
    ]}
    facts = _history_facts(payload, entity_hint='600519', source_id='TEST', metric='ohlcv', limit=60)
    result = history_bars(facts)
    assert len(result) == 2
    assert [p['value'] for p in result] == [10, 12]
    assert [p['volume'] for p in result] == [0, 10000]
    assert all(p['volume_unit'] == '股' for p in result)
    assert {f.field for f in facts} == {'close_price', 'high_price', 'low_price', 'volume'}
    assert calculate_indicators(result)['obv'].available


@pytest.mark.asyncio
async def test_provider_technical_history_requests_one_dated_same_basis_batch():
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={'data': [{'股票代码': '600519.SH', '日期': '2026-06-01',
                                                  '收盘价': 10, '最高价': 11, '最低价': 9, '成交量(股)': 0}]})
    provider = IwencaiSkillHubProvider('test-secret', base_url='https://iwencai.example',
                                      max_retries=0, transport=httpx.MockTransport(handler))
    try:
        facts = await provider.get_technical_history('600519', '股票', limit=60)
    finally:
        await provider.aclose()
    assert len(calls) == 1 and '60' in calls[0]['query']
    assert '最高价' in calls[0]['query'] and '不复权' in calls[0]['query']
    assert history_bars(facts)[0]['volume'] == 0


def test_history_api_serializes_six_indicators_and_auxiliary_prices(monkeypatch):
    import backend.app.main as main
    class Provider:
        async def get_price_history(self, *args, **kwargs):
            raise AssertionError('the technical loader should be used')
        async def get_technical_history(self, target, asset_type, *, limit):
            assert limit == 60
            return [FactRecord(fact_id=f'{i}-{field}', entity=target, field=field, value=point[key],
                               unit='股' if field == 'volume' else None, period=point['date'].isoformat(),
                               snapshot_time=datetime.now(timezone.utc), source_id='TEST', quality=.9)
                    for i, point in enumerate(bars())
                    for key, field in [('value', 'close_price'), ('high', 'high_price'),
                                       ('low', 'low_price'), ('volume', 'volume')]]
    monkeypatch.setattr(main, 'data_provider', Provider())
    main.app.dependency_overrides[main.authenticated_user] = lambda: {'id': 1}
    try:
        response = TestClient(main.app).post('/api/v1/data/price-history',
                    json={'target': '600519', 'asset_type': '股票', 'limit': 60})
    finally:
        main.app.dependency_overrides.pop(main.authenticated_user, None)
    assert response.status_code == 200
    body = response.json()
    assert len(body['points']) == 60 and body['points'][-1]['high'] > body['points'][-1]['value']
    assert len(body['analysis']['indicators']) == 6
    assert all(item['available'] for item in body['analysis']['indicators'].values())
    assert 'fact_id' not in response.text and 'test-secret' not in response.text


def frontend_result(points):
    return {'points': [{**point, 'date': point['date'].isoformat()} for point in points],
            'metric_label': '收盘价', 'ma5': [], 'analysis': analyze_trend(points).model_dump(mode='json')}


def test_frontend_switches_all_categories_displays_six_cards_and_downloads():
    result = frontend_result(bars())
    script = ('import json\nfrom frontend.trend_view import render_trend_panel\n'
              f'result = json.loads({json.dumps(json.dumps(result))})\n'
              "render_trend_panel(result, key='technical-test')\n")
    app = AppTest.from_string(script).run(timeout=20)
    assert not app.exception
    assert len(app.metric) == 10
    assert len(app.get('vega_lite_chart')) >= 2
    for category in ('动量', '波动率', '量能', '趋势'):
        app.segmented_control[0].set_value(category).run(timeout=20)
        assert not app.exception
        assert len(app.metric) == 10
        assert len(app.get('vega_lite_chart')) >= 2
    app.session_state['technical_data_technical-test_macd'] = True
    app.run(timeout=20)
    assert not app.exception
    assert len(app.dataframe) == 1
    assert len(app.get('download_button')) == 1


def test_frontend_close_only_keeps_missing_metrics_explicit():
    points = [{'date': p['date'], 'value': p['value']} for p in bars()]
    result = frontend_result(points)
    script = ('import json\nfrom frontend.trend_view import render_trend_panel\n'
              f'result = json.loads({json.dumps(json.dumps(result))})\n'
              "render_trend_panel(result, key='missing-test')\n")
    app = AppTest.from_string(script).run(timeout=20)
    app.segmented_control[0].set_value('量能').run(timeout=20)
    assert not app.exception
    assert next(m for m in app.metric if m.label.startswith('OBV')).value == '—'
    assert any('成交量' in item.value for item in app.info)


def test_research_page_indicator_panel_is_on_demand_and_reuses_history():
    result = frontend_result(bars())
    result.update(target='600519', asset_type='股票', status='ok')
    script = ('import json\nimport streamlit as st\nfrom frontend.research_board import _render_asset_technical\n'
              f'result = json.loads({json.dumps(json.dumps(result))})\n'
              "st.session_state.setdefault('calls', [])\n"
              "def api(base, method, path, payload):\n"
              "    st.session_state.calls.append(payload)\n    return result\n"
              "_render_asset_technical('http://test-only', '600519', '股票', api)\n")
    app = AppTest.from_string(script).run(timeout=20)
    assert app.session_state['calls'] == []
    # AppTest has no native expander toggle simulation; pin its open state in the test script.
    script = script.replace("_render_asset_technical('http", "st.session_state['asset_technical_股票'] = True\n_render_asset_technical('http")
    app = AppTest.from_string(script).run(timeout=20)
    assert not app.exception and app.session_state['calls'] == []
    next(b for b in app.button if b.label == '加载 / 刷新技术指标').click().run(timeout=20)
    assert not app.exception
    assert app.session_state['calls'] == [{'target': '600519', 'asset_type': '股票', 'limit': 60}]
    assert len(app.metric) == 10
    app.segmented_control[0].set_value('波动率').run(timeout=20)
    assert not app.exception and len(app.session_state['calls']) == 1


@pytest.mark.asyncio
async def test_index_board_joins_auxiliary_fields_instead_of_treating_them_as_closes():
    from backend.app.services.index_board import fetch_index_board
    class Provider:
        async def query(self, *args, **kwargs):
            return []
        async def get_price_history(self, *args, **kwargs):
            raise AssertionError('technical loader should be preferred')
        async def get_technical_history(self, target, asset_type, *, limit):
            assert limit == 60
            return [FactRecord(fact_id=f'{i}-{field}', entity=target, field=field, value=point[key],
                               unit='股' if field == 'volume' else None, period=point['date'].isoformat(),
                               snapshot_time=datetime.now(timezone.utc), source_id='TEST', quality=.9)
                    for i, point in enumerate(bars())
                    for key, field in [('value', 'close_price'), ('high', 'high_price'),
                                       ('low', 'low_price'), ('volume', 'volume')]]
    board = await fetch_index_board(Provider())
    assert board.status == 'ok'
    assert all(len(item.points) == 60 for item in board.items)
    assert all(all(ind.available for ind in item.analysis.indicators.values()) for item in board.items)
