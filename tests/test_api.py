"""FastAPI 入口测试：确认 HTTP 层正确装配并调用主协调智能体。"""

from fastapi.testclient import TestClient

from backend.app.main import app


client = TestClient(app)


def test_health_endpoint_returns_ok() -> None:
    """健康检查是部署与 README 中最先验证的最小可用接口。"""

    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_portfolio_analysis_returns_auditable_advice_package() -> None:
    """组合诊断应返回 trace、证据、专业结果与合规状态。"""

    response = client.post(
        "/api/v1/portfolio/analyze",
        json={
            "query": "请诊断我的持仓组合",
            "profile": {"user_id": "u-api", "risk_level": "R3", "confirmed": True},
            "facts": [
                {
                    "fact_id": "F-API-1",
                    "entity": "示例ETF",
                    "field": "weight",
                    "value": 0.4,
                    "snapshot_time": "2026-08-14T08:00:00Z",
                    "source_id": "DEMO_SNAPSHOT",
                    "quality": 0.8,
                }
            ],
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert body["trace_id"].startswith("T-")
    assert body["compliance"]["status"] == "PASS"
    assert body["evidence"] == ["F-API-1"]
    assert len(body["agent_results"]) == 5
