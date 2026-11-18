"""研究页 -> FastAPI -> SQLite 的保存、重开和移除闭环，不访问外部服务。"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from backend.app import main
from backend.app.database import Database
from backend.app.models import FactRecord
from backend.app.services.overview_cache import OverviewCache


CASES = [
    ("市场解读", "market", "指数", "000001.SH", "上证指数"),
    ("行业分析", "industry", "行业", "881121.TI", "半导体"),
    ("个股研究", "stock", "股票", "600519.SH", "贵州茅台"),
    ("基金筛选", "fund", "基金", "510300.SH", "测试基金A"),
    ("可转债分析", "convertible", "可转债", "113001.SH", "测试转债"),
]


class Provider:
    def __init__(self, code, name):
        self.code, self.name = code, name

    def fact(self, field, value, period="2026-11-13", unit=None):
        return FactRecord(fact_id=f"integration-{field}-{period}", entity=self.name, entity_code=self.code,
                          field=field, value=value, unit=unit, period=period,
                          snapshot_time=datetime.now(timezone.utc), source_id="TEST", quality=.9)

    async def query(self, *args, **kwargs):
        return [self.fact("close_price", 10), self.fact("change", 1.2, unit="percent"),
                self.fact("fund_nav", 1.25), self.fact("fund_size", 1e9),
                self.fact("fee_rate", .15, unit="percent"), self.fact("return_1m", 3.4, unit="percent")]

    async def get_financial_metrics(self, *args):
        return [self.fact("roe", 12, unit="percent")]

    async def get_basic_info(self, *args):
        return [self.fact("industry", "银行")]

    async def get_macro_data(self, *args):
        return []

    async def get_news(self, *args):
        return []

    async def get_price_history(self, *args, **kwargs):
        field = "fund_nav" if kwargs.get("asset_type") == "基金" else "close_price"
        return [self.fact(field, 1.2, "2026-11-12"), self.fact(field, 1.25)]


def render_app(client, page, direction, target):
    from concurrent.futures import Future
    from unittest.mock import patch
    import streamlit as st
    from frontend import research_board as board

    class ImmediateExecutor:
        def submit(self, fn, *args):
            future = Future()
            future.set_result(fn(*args))
            return future

    def api(base, method, path, payload=None):
        response = client.request(method, "/api/v1" + path, json=payload)
        if response.status_code >= 400:
            st.error(response.json().get("detail", "测试接口失败"))
            return None
        return response.json()

    if "watchlist" not in st.session_state:
        st.session_state.watchlist = api("test-only", "GET", "/watchlist")
        st.session_state.watchlist_loaded = True
    st.session_state.setdefault(f"board_target_{direction}", target)
    st.session_state.setdefault("overview_targets", [])

    def fetch(base, requested_direction, requested_target):
        st.session_state.overview_targets.append(requested_target)
        return api(base, "POST", "/data/overview", {"direction": requested_direction, "target": requested_target})

    with patch.object(board, "board_prefetch_executor", return_value=ImmediateExecutor()):
        board._render_research_board("test-only", page, fetch, api)


@pytest.mark.parametrize("page,direction,kind,code,name", CASES)
def test_page_watchlist_persists_through_api_and_reopens_in_a_new_ui_session(monkeypatch, page, direction, kind, code, name):
    database = Database("sqlite+pysqlite:///:memory:", "test-secret-that-is-longer-than-thirty-two-characters")
    database.initialize()
    user = database.create_user("integration-user", "hashed")
    other_kind = "股票" if kind == "基金" else "基金"
    other = database.add_watchlist_item(int(user["id"]), "159915", other_kind, "其他方向自选")
    monkeypatch.setattr(main, "database", database)
    monkeypatch.setattr(main, "data_provider", Provider(code, name))
    monkeypatch.setattr(main, "overview_cache", OverviewCache())
    main.app.dependency_overrides[main.customer_user] = lambda: {"id": int(user["id"])}
    client = TestClient(main.app)
    try:
        initial_target = code if direction == "market" else code.split(".")[0]
        app = AppTest.from_function(render_app, args=(client, page, direction, initial_target)).run(timeout=15)
        assert not app.exception and not app.error
        next(button for button in app.button if button.label == "加入自选").click().run(timeout=15)
        assert not app.exception and not app.error
        saved = [item for item in database.list_watchlist(int(user["id"])) if item["asset_type"] == kind]
        assert len(saved) == 1 and saved[0]["target"] == code and saved[0]["name"] == name

        # 新建页面会话，必须从 API/数据库恢复，而不是沿用前一页面的 Session State。
        other_target = "600036" if direction == "stock" else None
        reopened = AppTest.from_function(render_app, args=(client, page, direction, other_target)).run(timeout=15)
        assert not reopened.exception
        selector = reopened.selectbox(key=f"board_target_{direction}")
        assert [label for label in selector.options if label.startswith("★")] == [f"★ {name} · {code}"]
        assert not any("其他方向自选" in label for label in selector.options)
        assert not any(button.label == "查看自选信息" for button in reopened.button)
        selector.select_index(selector.options.index(f"★ {name} · {code}")).run(timeout=15)
        assert not reopened.exception and not reopened.error
        selected = reopened.session_state[f"board_target_{direction}"]
        assert selected in (initial_target, code)
        assert reopened.session_state["overview_targets"][-1] == selected
        next(button for button in reopened.button if button.label == "移除自选").click().run(timeout=15)
        assert not reopened.exception and not reopened.error
        assert [item["id"] for item in database.list_watchlist(int(user["id"]))] == [other["id"]]
        assert not next(button for button in reopened.button if button.label == "加入自选").disabled
        assert not any(label.startswith("★") for label in reopened.selectbox(key=f"board_target_{direction}").options)
    finally:
        main.app.dependency_overrides.pop(main.customer_user, None)
        client.close()


def test_initial_watchlist_read_blocks_edits_but_keeps_fund_information_visible():
    from tests.test_fund_details_and_board_watchlist import board_script
    source = board_script().replace("st.session_state.setdefault('watchlist', [])",
                                  "st.session_state.setdefault('watchlist', [])\nst.session_state.watchlist_loaded = False")
    app = AppTest.from_string(source).run(timeout=15)
    assert not app.exception and app.metric and app.dataframe
    assert next(button for button in app.button if button.label == "加入自选").disabled
    assert not app.session_state["writes"]


def test_failed_initial_watchlist_read_can_retry_without_reloading_the_page():
    from tests.test_fund_details_and_board_watchlist import board_script
    source = board_script().replace("st.session_state.setdefault('watchlist', [])",
        "st.session_state.setdefault('watchlist', [])\nst.session_state.setdefault('watchlist_loaded', False)")
    source = source.replace("if method == 'POST' and path == '/watchlist':",
        "if method == 'GET' and path == '/watchlist':\n        return []\n    if method == 'POST' and path == '/watchlist':")
    app = AppTest.from_string(source).run(timeout=15)
    assert not app.exception
    assert next(button for button in app.button if button.label == "加入自选").disabled
    next(button for button in app.button if button.label == "重试加载自选").click().run(timeout=15)
    assert not app.exception
    assert app.session_state["watchlist_loaded"]
    assert not next(button for button in app.button if button.label == "加入自选").disabled
