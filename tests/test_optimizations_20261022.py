"""2026-10-22 优化项的回归测试。

覆盖四块新增能力：确定性组合优化、数值级幻觉检测、Prometheus 观测出口、
登录失败限流，以及"意图识别与取数重叠"的预取复用判定。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from backend.app import main as main_module
from backend.app.database import Database
from backend.app.models.schemas import FactRecord
from backend.app.services import numeric_grounding, portfolio_optimizer
from backend.app.services.login_guard import LoginThrottle
from backend.app.session_pool import SessionThreadPool


def _fact(entity: str, field: str, value, *, unit: str | None = None, quality: float = 0.9) -> FactRecord:
    return FactRecord(
        fact_id=f"{entity}-{field}", entity=entity, field=field, value=value, unit=unit,
        snapshot_time=datetime.now(timezone.utc), source_id="IWENCAI_SKILLHUB", quality=quality,
    )


def _authenticated_client(monkeypatch, request) -> tuple[TestClient, str]:
    database = Database("sqlite+pysqlite:///:memory:", "test-secret-that-is-longer-than-thirty-two-characters")
    database.initialize()
    monkeypatch.setattr(main_module, "database", database)
    session_pool = SessionThreadPool()
    request.addfinalizer(session_pool.shutdown)
    monkeypatch.setattr(main_module, "session_thread_pool", session_pool)
    client = TestClient(main_module.app)
    # 不进 lifespan：会话池/数据库表已在本夹具里准备好，避免关闭全局缓存影响其他用例。
    registered = client.post("/api/v1/auth/register",
                             json={"username": "opt-user", "password": "strong-pass-1"})
    assert registered.status_code == 200, registered.text
    return client, registered.json()["access_token"]


# --------------------------------------------------------------------------
# 组合优化：约束投影与风险平价
# --------------------------------------------------------------------------


def test_portfolio_plan_respects_caps_and_sums_to_one() -> None:
    holdings = [
        {"symbol": "600519", "weight": 0.55, "industry": "白酒", "volatility": 0.28},
        {"symbol": "300750", "weight": 0.25, "industry": "电池", "volatility": 0.42},
        {"symbol": "601318", "weight": 0.20, "industry": "保险", "volatility": 0.22},
    ]
    plan = portfolio_optimizer.build_portfolio_plan(
        holdings,
        constraints=portfolio_optimizer.OptimizationConstraints(single_limit=0.35, industry_limit=0.5),
        risk_level="C4",
    )
    assert plan["status"] == "ok"
    assert plan["basis"] == "volatility_only"
    assert plan["recommended_strategy"] == "risk_parity"
    for name, weights in plan["candidates"].items():
        # 候选权重按 4 位小数回写，合计容差要留在舍入量级。
        assert abs(sum(weights) - 1.0) < 1e-3, name
        assert max(weights) <= 0.35 + 1e-6, name
    # 超限的输入要被点名，而不是悄悄归一化掉。
    assert plan["constraint_violations"]["single_security"] == ["600519"]
    assert plan["constraint_violations"]["relaxed"] == []
    assert plan["method"] == "deterministic_arithmetic_no_llm"
    assert plan["warnings"]


def test_conservative_profile_prefers_inverse_volatility() -> None:
    plan = portfolio_optimizer.build_portfolio_plan(
        [{"symbol": "A", "weight": 0.5, "volatility": 0.1},
         {"symbol": "B", "weight": 0.5, "volatility": 0.4}],
        # 2 条持仓配 20% 上限不可行，会触发放宽并压平权重，这里放开上限只看逆波动。
        constraints=portfolio_optimizer.OptimizationConstraints(
            single_limit=1.0, industry_limit=1.0),
        risk_level="C2",
    )
    assert plan["recommended_strategy"] == "inverse_volatility"
    # 低波动标的应获得更高目标权重。
    assert plan["candidates"]["inverse_volatility"][0] > plan["candidates"]["inverse_volatility"][1]


def test_live_returns_produce_correlation_and_turnover() -> None:
    stable = [100 + index * 0.5 for index in range(45)]
    volatile = [100 + (index % 7) * 3 for index in range(45)]
    plan = portfolio_optimizer.build_portfolio_plan(
        [{"symbol": "A", "weight": 0.6}, {"symbol": "B", "weight": 0.4}],
        price_series={"A": stable, "B": volatile},
        total_value=100_000,
    )
    assert plan["basis"] == "live_returns"
    matrix = plan["correlation"]["matrix"]
    assert matrix[0][0] == 1.0 and matrix[1][1] == 1.0
    assert -1.0 <= matrix[0][1] <= 1.0
    assert plan["target"]["volatility"] is not None
    assert len(plan["target"]["risk_contribution"]) == 2
    assert all("amount" in row for row in plan["rebalance"])
    assert plan["turnover"] >= 0.0


def test_price_series_from_points_sorts_and_cleans() -> None:
    series = portfolio_optimizer.price_series_from_points([
        {"date": "2026-01-03", "value": "1,680.00 元"},
        {"date": "2026-01-02", "value": 100},
        {"date": "2026-01-04", "value": "-"},
    ])
    assert series == [100.0, 1680.0]


# --------------------------------------------------------------------------
# 数值溯源（幻觉检测）
# --------------------------------------------------------------------------


def test_numeric_grounding_flags_numbers_missing_from_facts() -> None:
    facts = [_fact("示例科技", "营收", 12_345_000_000, unit="元")]
    grounded = numeric_grounding.check_text_grounding("示例科技营收123.45亿元。", facts)
    assert grounded["claim_count"] >= 1
    assert grounded["grounded_ratio"] == 1.0
    assert grounded["risk"] == "low"

    invented = numeric_grounding.check_text_grounding("示例科技营收999.99亿元。", facts)
    assert invented["ungrounded_count"] >= 1
    assert invented["risk"] in {"medium", "high"}
    assert any("999.99" in item.get("raw", "") for item in invented["ungrounded"])


def test_numeric_grounding_summary_handles_agents_without_numbers() -> None:
    facts = [_fact("示例科技", "close_price", 42.5, unit="元")]
    summary = numeric_grounding.summarize_grounding([], facts)
    assert summary["claim_count"] == 0
    assert summary["status"] == "checked"
    assert summary["method"] == "regex_extraction_against_cited_facts"
    assert numeric_grounding.summarize_grounding([], [])["status"] == "no_facts"


# --------------------------------------------------------------------------
# 观测出口
# --------------------------------------------------------------------------


def test_prometheus_endpoint_exposes_exposition_format() -> None:
    # 不进 lifespan：这里只验证导出格式，不需要真正初始化数据库。
    client = TestClient(main_module.app)
    response = client.get("/api/v1/metrics/prometheus")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    body = response.text
    assert "wence_build_info" in body
    assert "wence_requests_total" in body
    assert "# TYPE" in body


def test_metrics_payload_includes_login_throttle() -> None:
    client = TestClient(main_module.app)
    payload = client.get("/api/v1/metrics").json()
    assert "login_throttle" in payload
    assert payload["login_throttle"]["scope"] == "single_process_memory"


# --------------------------------------------------------------------------
# 登录限流
# --------------------------------------------------------------------------


def test_login_throttle_locks_then_expires() -> None:
    clock = {"now": 1000.0}
    throttle = LoginThrottle(max_failures=3, window_seconds=60, lockout_seconds=30,
                             clock=lambda: clock["now"])
    key = throttle.key("Alice", "10.0.0.1")
    assert throttle.failure(key) == 0
    assert throttle.failure(key) == 0
    assert throttle.failure(key) == 30
    assert throttle.retry_after(key) == 30
    clock["now"] += 31
    assert throttle.retry_after(key) == 0
    clock["now"] += 1
    throttle.success(key)
    assert throttle.snapshot()["tracked_keys"] == 0


def test_login_endpoint_returns_429_with_retry_after(monkeypatch, request) -> None:
    client, _ = _authenticated_client(monkeypatch, request)
    throttle = LoginThrottle(max_failures=3, window_seconds=600, lockout_seconds=45)
    monkeypatch.setattr(main_module, "login_throttle", throttle)
    request.addfinalizer(throttle.reset)

    for _ in range(2):
        assert client.post("/api/v1/auth/login",
                           json={"username": "opt-user", "password": "wrong-pass"}).status_code == 401
    third = client.post("/api/v1/auth/login", json={"username": "opt-user", "password": "wrong-pass"})
    assert third.status_code == 429
    assert third.headers["Retry-After"] == "45"

    locked = client.post("/api/v1/auth/login",
                         json={"username": "opt-user", "password": "strong-pass-1"})
    assert locked.status_code == 429

    throttle.reset()
    allowed = client.post("/api/v1/auth/login",
                          json={"username": "opt-user", "password": "strong-pass-1"})
    assert allowed.status_code == 200


# --------------------------------------------------------------------------
# 新增端到端接口
# --------------------------------------------------------------------------


def test_portfolio_plan_endpoint(monkeypatch, request) -> None:
    monkeypatch.setattr(main_module, "data_provider", None)
    client, token = _authenticated_client(monkeypatch, request)
    response = client.post("/api/v1/portfolio/plan", headers={"Authorization": f"Bearer {token}"}, json={
        "holdings": [
            {"symbol": "600519", "name": "示例白酒", "weight": 0.5, "volatility": 0.25},
            {"symbol": "000001", "name": "示例银行", "weight": 0.35, "volatility": 0.18},
            {"symbol": "300750", "name": "示例电池", "weight": 0.15, "volatility": 0.4},
        ],
        "include_price_history": False,
        "total_value": 200000,
    })
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["status"] == "ok"
    assert plan["price_history_coverage"] == "0/3"
    assert len(plan["rebalance"]) == 3
    assert plan["elapsed_ms"] >= 0
    assert isinstance(plan["industry_exposure"], dict)
    assert plan["basis"] == "volatility_only"
    # 3 条持仓配默认 20% 单票上限本身不可行：放宽并披露，而不是留下 10% 无法解释的空仓。
    assert plan["constraint_violations"]["relaxed"]
    for name, weights in plan["candidates"].items():
        assert abs(sum(weights) - 1.0) < 1e-3, name
        assert max(weights) <= 1 / 3 + 1e-4, name


def test_infeasible_cap_is_relaxed_and_disclosed() -> None:
    plan = portfolio_optimizer.build_portfolio_plan(
        [{"symbol": "A", "weight": 0.4}, {"symbol": "B", "weight": 0.35}, {"symbol": "C", "weight": 0.25}],
        constraints=portfolio_optimizer.OptimizationConstraints(single_limit=0.2),
    )
    relaxed = plan["constraint_violations"]["relaxed"]
    assert relaxed and "单票上限" in relaxed[0]
    assert any("放宽" in line for line in plan["warnings"])
    for weights in plan["candidates"].values():
        assert abs(sum(weights) - 1.0) < 1e-3


def test_grounding_check_endpoint() -> None:
    client = TestClient(main_module.app)
    response = client.post("/api/v1/research/grounding-check", json={
        "text": "示例科技最新收盘价42.5元，另有88.8元的传闻价。",
        "facts": [{
            "fact_id": "f-1", "entity": "示例科技", "field": "close_price", "value": 42.5,
            "unit": "元", "snapshot_time": datetime.now(timezone.utc).isoformat(),
            "source_id": "IWENCAI_SKILLHUB", "quality": 0.9,
        }],
    })
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["claim_count"] >= 1
    assert payload["ungrounded_count"] >= 1


# --------------------------------------------------------------------------
# 预取复用判定（纯函数，不需要真实取数）
# --------------------------------------------------------------------------


def test_speculation_key_requires_direction_and_target() -> None:
    from backend.app.models.schemas import OrchestrationRequest

    direction = main_module.DIRECTION_FOR_INTENT[main_module.Intent.SECURITY_RESEARCH]
    deep = OrchestrationRequest(query="分析示例科技", research_direction=direction,
                                research_target="示例科技", research_mode="deep", profile={"confirmed": True})
    assert main_module._speculation_key(deep) == (main_module.Intent.SECURITY_RESEARCH, "示例科技")

    no_target = deep.model_copy(update={"research_target": None})
    assert main_module._speculation_key(no_target) is None
    lite = deep.model_copy(update={"research_mode": "lite"})
    assert main_module._speculation_key(lite) is None
    assert main_module._speculation_key(deep.model_copy(update={"research_direction": None})) is None


def test_take_prefetch_discards_mismatched_result() -> None:
    async def scenario() -> object:
        async def slow():
            await asyncio.sleep(5)
            return ("prepared", "acquisition")

        task = asyncio.ensure_future(slow())
        result = await main_module._take_prefetch(
            task, (main_module.Intent.SECURITY_RESEARCH, "示例科技"),
            main_module.Intent.UNKNOWN, "示例科技", lambda _stage: None,
        )
        # 取消是"排队"动作：让出一次事件循环才能看到任务真的停下。
        await asyncio.sleep(0)
        assert task.cancelled() or task.done()
        return result

    assert asyncio.run(scenario()) is None


def test_take_prefetch_reuses_matching_result() -> None:
    async def scenario() -> object:
        async def ready():
            return ("prepared", "acquisition")

        task = asyncio.ensure_future(ready())
        stages: list[str] = []
        return await main_module._take_prefetch(
            task, (main_module.Intent.SECURITY_RESEARCH, "示例科技"),
            main_module.Intent.SECURITY_RESEARCH, "示例科技", stages.append,
        ), stages

    result, stages = asyncio.run(scenario())
    assert result == ("prepared", "acquisition")
    assert stages == ["复用预取资料"]


def test_risk_contribution_sums_to_volatility() -> None:
    holdings = [{"symbol": "A", "weight": 0.5, "volatility": 0.2},
                {"symbol": "B", "weight": 0.5, "volatility": 0.3}]
    plan = portfolio_optimizer.build_portfolio_plan(
        holdings, price_series={"A": [100 + i * 0.4 for i in range(40)],
                                "B": [100 + (i % 5) for i in range(40)]},
    )
    contributions = plan["target"]["risk_contribution"]
    assert len(contributions) == 2
    # 风险贡献以占比给出，合计应为 1。
    assert abs(sum(contributions) - 1.0) < 1e-3
