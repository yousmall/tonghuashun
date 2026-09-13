"""FastAPI 入口测试：确认 HTTP 层正确装配并调用主协调智能体。"""

from datetime import datetime, timezone

from fastapi.testclient import TestClient

import backend.app.main as main_module
from backend.app.main import app


client = TestClient(app)


def test_health_endpoint_returns_ok() -> None:
    """健康检查是部署与 README 中最先验证的最小可用接口。"""

    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_data_fetch_returns_explicit_unavailable_result(monkeypatch) -> None:
    """问财连接失败应成为可处理的业务结果，而不是把整个资料面板打成 502。"""

    class FailingProvider:
        source_id = "IWENCAI_TEST"

        async def get_quote(self, _: str):
            raise RuntimeError("无法建立问财服务连接，请检查网络、DNS 或代理设置后重试。")

    monkeypatch.setattr(main_module, "data_provider", FailingProvider())
    response = client.post(
        "/api/v1/data/fetch",
        json={"kind": "quote", "target": "贵州茅台", "filters": {}},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["provider"] == "IWENCAI_TEST"
    assert body["status"] == "unavailable"
    assert body["message"] == "无法建立问财服务连接，请检查网络、DNS 或代理设置后重试。"
    assert body["facts"] == []


def test_data_fetch_explains_missing_provider_configuration(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "data_provider", None)

    response = client.post(
        "/api/v1/data/fetch",
        json={"kind": "quote", "target": "贵州茅台", "filters": {}},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert "IWENCAI_API_KEY" in response.json()["message"]
    assert response.json()["facts"] == []


def test_profile_assessment_and_confirmation_are_explicit_two_steps() -> None:
    """画像评估不能绕过用户确认，确认后版本必须向前推进。"""
    assessment = client.post(
        "/api/v1/profile/assess",
        json={"user_id": "u-profile-api", "narrative": "我 2 年后买房，最多接受 8% 回撤"},
    )

    assert assessment.status_code == 200
    draft = assessment.json()["profile"]
    assert draft["confirmed"] is False
    assert draft["horizon_months"] == 24
    confirmed = client.post("/api/v1/profile/confirm", json={"profile": draft})
    assert confirmed.status_code == 200
    assert confirmed.json()["confirmed"] is True
    assert confirmed.json()["version"] == 2


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
                    # 使用运行时新鲜快照，测试不应把固定历史数据误当作当前行情。
                    "snapshot_time": datetime.now(timezone.utc).isoformat(),
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
