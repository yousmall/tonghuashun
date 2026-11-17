"""新增可视化组件的渲染回归：面板、走势、指数看板与投资价值比较。"""
import json
from datetime import date, timedelta

from streamlit.testing.v1 import AppTest

TODAY = date(2026, 6, 30)
DATES = [(TODAY - timedelta(days=9 - index)).isoformat() for index in range(10)]
VALUES = [10.0, 10.1, 10.3, 10.2, 10.4, 10.6, 10.5, 10.8, 11.0, 11.2]
MA5 = [{"date": day, "value": round(sum(VALUES[index:index + 5]) / 5, 4)}
       for index, day in enumerate(DATES) if index >= 4]

ANALYSIS = {
    "available": True, "window": 10, "latest_close": 11.2, "latest_date": DATES[-1],
    "start_date": DATES[0], "ma5": 10.9, "ma5_series": MA5, "ma5_slope_pct": 1.1,
    "price_vs_ma5_pct": 2.75, "ma10": 10.7, "ma20": None, "ma5_vs_ma20_pct": 1.87,
    "three_day": {"available": True, "change_pct": 5.66, "height": 0.6,
                  "consecutive_up_days": 3, "all_up": True,
                  "daily_changes_pct": [1.9, 1.85, 1.82], "start_date": DATES[-4], "end_date": DATES[-1]},
    "score": 72.0, "verdict": "上行", "neutralized": False, "confidence": 0.68,
    "signals": [{"key": "ma5_slope", "label": "五日均线斜率", "reading": "MA5 较前一交易日 +1.10%",
                 "direction": "up", "score": 0.44, "weight": 0.28},
                {"key": "three_day", "label": "连续三日上升高度", "reading": "近 3 个交易日累计 +5.66%",
                 "direction": "up", "score": 0.94, "weight": 0.20}],
    "notes": ["参与计算 10 个交易日。", "最近三个交易日上涨 5.66%，上升高度 +0.60。"],
}

TREND_RESULT = {
    "target": "600519", "asset_type": "股票", "metric_label": "收盘价", "status": "ok",
    "points": [{"date": day, "value": value} for day, value in zip(DATES, VALUES)],
    "ma5": MA5, "analysis": ANALYSIS,
}

BOARD = {
    "status": "ok", "fetched_at": "2026-06-30T08:00:00Z", "notes": [],
    "items": [{"name": name, "code": code, "status": "ok", "latest_close": 3200.0 + index,
               "change_pct": 0.5, "trade_date": DATES[-1], "ma5": 3150.0, "three_day_pct": 1.2,
               "three_day_height": 38.0, "verdict": "上行", "score": 68.0, "confidence": 0.7,
               "analysis": ANALYSIS, "points": VALUES, "dates": DATES}
              for index, (name, code) in enumerate([
                  ("上证指数", "000001.SH"), ("深证成指", "399001.SZ"), ("创业板指", "399006.SZ")])],
}

DECISION = {
    "status": "ok", "method": "相对档位 + 绝对区间", "weights": {"valuation": 0.25, "growth": 0.25},
    "dimensions": [{"key": "valuation", "label": "估值", "weight": 0.25, "unit": "倍",
                    "field": "pe_ttm", "field_label": "市盈率 TTM", "direction": "lower_better"},
                   {"key": "growth", "label": "成长", "weight": 0.25, "unit": "%",
                    "field": "revenue_growth", "field_label": "营收同比", "direction": "higher_better"}],
    "candidates": [{"target": "600519", "name": "公司甲", "code": "600519.SH", "rank": 1,
                    "score": 78.0, "dimension_scores": {"valuation": 85, "growth": 70},
                    "metrics": {"pe_ttm": 12}, "strengths": ["估值 85 分"], "weaknesses": [],
                    "coverage": 0.5},
                   {"target": "000858", "name": "公司乙", "code": "000858.SZ", "rank": 2,
                    "score": 52.0, "dimension_scores": {"valuation": 45, "growth": 60},
                    "metrics": {"pe_ttm": 30}, "strengths": [], "weaknesses": ["估值 45 分"],
                    "coverage": 0.5}],
    "preferred": "600519", "preferred_name": "公司甲",
    "conclusion": "公司甲综合得分 78.0，排名第 1，在现有资料范围内相对更具投资价值。",
    "caveats": ["得分是相对档位，不是预期收益。"], "missing": ["资金（缺少资料）"],
}

ADVICE = {
    "trace_id": "t", "timings_ms": {"核对投资偏好": 12.5, "理解问题": 800.2, "自动取数": 1500.5,
                                    "专业研判": 2400.0, "事实核验": 30.1, "合规审核": 500.0,
                                    "total": 5300.0},
    "facts": [{"fact_id": f"f{index}", "entity": "公司甲", "field": "pe_ttm", "value": 12,
               "source_id": "IWENCAI_SKILLHUB"} for index in range(3)],
    "evidence": ["f0", "f1"],
    "model_calls": [{"purpose": "industry", "model": "deepseek-chat", "status": "completed",
                     "input_chars": 1000, "total_ms": 800}],
    "data_acquisition": {"mode": "live", "requested_capabilities": ["market", "news"],
                         "successful_capabilities": ["market"], "failed_capabilities": ["news"],
                         "recovery_rounds": 1, "message": "部分资料未取得"},
    "task_plan": {"nodes": [{"task_id": "t1", "agent_id": "industry", "status": "completed",
                             "depends_on": [], "timeout_seconds": 8}]},
    "compliance": {"status": "PASS"}, "confidence": 0.72,
    "risks": ["样本不足，需补充连续交易日"],
    "agent_results": [{"agent_id": "industry", "status": "completed", "opinion": "行业景气一般",
                       "score": 65, "rule_score": 65, "model_score": 70, "confidence": 0.6,
                       "risk_flags": []}],
    "cross_validation": {"issues": [{"severity": "warning", "message": "同项记录存在冲突"}]},
}


def run(script: str) -> AppTest:
    app = AppTest.from_string(script).run(timeout=20)
    assert not app.exception, app.exception
    return app


def test_insight_panel_renders_process_and_conclusion_charts():
    app = run("import json\nimport streamlit as st\n"
              "from frontend.insight_panel import render_process_panel, render_conclusion_panel\n"
              f"advice = json.loads({json.dumps(json.dumps(ADVICE))})\n"
              "render_process_panel(advice, key='t')\n"
              "render_conclusion_panel(advice, key='t')\n")
    assert len(app.get("vega_lite_chart")) >= 3
    labels = [metric.label for metric in app.metric]
    assert "完整分析耗时" in labels and "合规状态" in labels and "综合置信度" in labels
    assert any("取数状态" in caption.value for caption in app.caption)


def test_trend_panel_shows_ma5_three_day_and_signals():
    app = run("import json\nimport streamlit as st\n"
              "from frontend.trend_view import render_trend_panel\n"
              f"result = json.loads({json.dumps(json.dumps(TREND_RESULT))})\n"
              "render_trend_panel(result, key='t', unit_label=' 元')\n")
    labels = [metric.label for metric in app.metric]
    assert labels == ["五日均线 MA5", "收盘相对 MA5", "连续三日累计", "中和判断"]
    assert len(app.get("vega_lite_chart")) >= 1
    assert app.metric[3].value == "上行"
    assert any("最近 3 个交易日" in caption.value for caption in app.caption)


def test_trend_panel_without_analysis_still_draws_the_price_line():
    app = run("import json\nimport streamlit as st\n"
              "from frontend.trend_view import render_trend_panel\n"
              f"result = json.loads({json.dumps(json.dumps({'points': TREND_RESULT['points'], 'metric_label': '收盘价'}))})\n"
              "render_trend_panel(result, key='old')\n")
    assert len(app.get("vega_lite_chart")) == 1
    assert not app.metric


def test_index_board_cards_and_comparison_chart():
    app = run("import json\nimport streamlit as st\n"
              "from frontend.index_board import render_index_board\n"
              f"board = json.loads({json.dumps(json.dumps(BOARD))})\n"
              "def fake_api(base, method, path, payload=None, **kwargs):\n"
              "    return board\n"
              "render_index_board('http://test-only', fake_api, key='market')\n")
    assert [metric.label for metric in app.metric][:3] == ["上证指数", "深证成指", "创业板指"]
    assert len(app.get("vega_lite_chart")) >= 2


def test_decision_view_ranks_candidates_with_names():
    app = run("import json\nimport streamlit as st\n"
              "from frontend.decision_view import render_decision\n"
              f"decision = json.loads({json.dumps(json.dumps(DECISION))})\n"
              "render_decision(decision, key='t')\n")
    assert any("公司甲" in success.value for success in app.success)
    assert len(app.get("vega_lite_chart")) == 1
    assert app.dataframe
    visible = "\n".join(element.value for element in app.markdown)
    assert "投资价值比较与决策" in visible


MARKET_SECTIONS = {
    "direction": "market", "target": None, "status": "ok",
    "fetched_at": "2026-06-30T08:00:00Z",
    "sections": [{"key": "overview", "title": "主要指数", "status": "ok", "facts": [
        {"fact_id": "m1", "entity": "上证指数", "entity_code": "000001.SH", "field": "close_price",
         "value": 3200.0, "period": DATES[-1], "snapshot_time": "2026-06-30T08:00:00Z",
         "source_id": "TEST", "quality": .9},
        {"fact_id": "m2", "entity": "上证指数", "entity_code": "000001.SH", "field": "change",
         "value": 0.5, "period": DATES[-1], "snapshot_time": "2026-06-30T08:00:00Z",
         "source_id": "TEST", "quality": .9},
    ]}],
}


def test_market_page_renders_the_index_board_with_a_fake_api():
    """市场解读页在概览数据之外挂载指数看板，且只用传入的 api_request。"""

    script = (
        "import json\nimport streamlit as st\n"
        "from unittest.mock import patch\n"
        "from concurrent.futures import Future\n"
        "from frontend import streamlit_app as ui\n"
        "from frontend import research_board as board\n"
        "class ImmediateExecutor:\n"
        "    def submit(self, function, *args):\n"
        "        future = Future(); future.set_result(function(*args)); return future\n"
        "ui.init_session()\n"
        "st.session_state.auth_token = 'test-only'\n"
        "st.session_state.profile['confirmed'] = True\n"
        f"section = json.loads({json.dumps(json.dumps(MARKET_SECTIONS))})\n"
        f"board_data = json.loads({json.dumps(json.dumps(BOARD))})\n"
        "def fake_fetch(base, direction, target, refresh=False):\n"
        "    return section\n"
        "def fake_api(base, method, path, payload=None, **kwargs):\n"
        "    return board_data if path == '/market/index-board' else []\n"
        "def fake_factory(client, token):\n"
        "    return lambda base, path, payload: lambda refresh: fake_api(base, 'POST', path, payload)\n"
        "with (\n"
        "    patch.object(board, 'board_prefetch_executor', return_value=ImmediateExecutor()),\n"
        "    patch('frontend.snapshots.board_prefetch_executor', return_value=ImmediateExecutor()),\n"
        "    patch('frontend.api_client.snapshot_fetch_factory', side_effect=fake_factory),\n"
        "):\n"
        "    board.render_research_board('http://test-only', '市场解读', fake_fetch, api_request=fake_api)\n"
    )
    app = run(script)
    labels = [metric.label for metric in app.metric]
    assert "上证指数" in labels
    assert len(app.get("vega_lite_chart")) >= 2
    assert not any("归一化只用于" in caption.value or "参与计算" in caption.value
                   or "走势判断由" in caption.value for caption in app.caption)


INDUSTRY_SECTIONS = {
    "direction": "industry", "target": "半导体", "status": "ok",
    "fetched_at": "2026-06-30T08:00:00Z",
    "sections": [{"key": "overview", "title": "行业表现", "status": "ok", "facts": [
        {"fact_id": "i1", "entity": "半导体", "entity_code": "881273.TI", "field": "change",
         "value": 2.4, "period": DATES[-1], "snapshot_time": "2026-06-30T08:00:00Z",
         "source_id": "TEST", "quality": .9},
        {"fact_id": "i2", "entity": "半导体", "entity_code": "881273.TI", "field": "capital_flow",
         "value": 1.2e8, "period": DATES[-1], "snapshot_time": "2026-06-30T08:00:00Z",
         "source_id": "TEST", "quality": .9},
    ]}],
}


def test_industry_page_loads_the_industry_trend_on_demand():
    """行业页的走势面板必须点击后才取数，且复用 price-history 的分析口径。"""

    trend = dict(TREND_RESULT, target="半导体", asset_type="行业", metric_label="行业指数")
    script = (
        "import json\nimport streamlit as st\n"
        "from unittest.mock import patch\n"
        "from concurrent.futures import Future\n"
        "from frontend import streamlit_app as ui\n"
        "from frontend import research_board as board\n"
        "class ImmediateExecutor:\n"
        "    def submit(self, function, *args):\n"
        "        future = Future(); future.set_result(function(*args)); return future\n"
        "ui.init_session()\n"
        "st.session_state.auth_token = 'test-only'\n"
        "st.session_state.profile['confirmed'] = True\n"
        "st.session_state.board_target_industry = '半导体'\n"
        f"section = json.loads({json.dumps(json.dumps(INDUSTRY_SECTIONS))})\n"
        f"trend = json.loads({json.dumps(json.dumps(trend))})\n"
        "st.session_state.calls = []\n"
        "def fake_fetch(base, direction, target, refresh=False):\n"
        "    return section\n"
        "def fake_api(base, method, path, payload=None, **kwargs):\n"
        "    if path == '/data/price-history':\n"
        "        st.session_state.calls.append(dict(payload))\n"
        "        return trend\n"
        "    return []\n"
        "with patch.object(board, 'board_prefetch_executor', return_value=ImmediateExecutor()):\n"
        "    board.render_research_board('http://test-only', '行业分析', fake_fetch, api_request=fake_api)\n"
    )
    app = run(script)
    assert app.session_state["calls"] == []
    next(button for button in app.button if button.label == "加载 / 刷新行业走势").click().run(timeout=20)
    assert not app.exception
    assert app.session_state["calls"] == [{"target": "半导体", "asset_type": "行业", "limit": 60}]
    assert "五日均线 MA5" in [metric.label for metric in app.metric]


def test_requirements_demo_page_renders_with_the_new_panel():
    from pathlib import Path

    demo = Path(__file__).resolve().parents[1] / "frontend" / "requirements_demo.py"
    if not (demo.parents[1] / "deliverables" / "requirements_acceptance.json").is_file():
        import pytest

        pytest.skip("需要先运行 scripts/verify_requirements.py 生成验收报告")
    app = AppTest.from_file(str(demo)).run(timeout=30)
    assert not app.exception
