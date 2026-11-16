"""隔离数据库的真实 HTTP/Streamlit 验证；不使用真实用户或外部行情/模型。"""
from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic, sleep

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
ANSWERS = dict(zip((f"q{i:02}" for i in range(1, 28)), "ACABDCCACCBBCCBCCDBCADAECDC", strict=True))
ANSWERS.update(q03="B", q20="B", q21="A", q22="D", q23="A", q25="C")


def run(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "accounts.sqlite3").exists():
        raise ValueError("请选择新的测试输出目录；不会复用或覆盖已有账号数据库。")
    api = f"http://127.0.0.1:{args.api_port}/api/v1"
    web = f"http://127.0.0.1:{args.web_port}"
    env = dict(os.environ, PYTHON_DOTENV_DISABLED="1", DEEPSEEK_API_KEY="", IWENCAI_API_KEY="",
        WENCE_WECHAT_APP_ID="", WENCE_WECHAT_APP_SECRET="", IWENCAI_PUBLIC_RECOVERY_ENABLED="0",
        MYSQL_URL="sqlite+pysqlite:///" + (output / "accounts.sqlite3").as_posix(),
        WENCE_AUTH_SECRET="isolated-personalized-browser-test-secret-20261116",
        WENCE_SESSION_STORE_PATH=str(output / "sessions.sqlite3"),
        WENCE_RESEARCH_CACHE_PATH=str(output / "research.sqlite3"),
        WENCE_API_BASE=api, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    processes, handles, tokens = [], [], []
    report = {"scope": "isolated_real_http_and_streamlit", "external_model_and_market_calls": False,
              "checks": [], "passed": False}
    session = requests.Session()
    session.trust_env = False

    def start(command, name):
        log = (output / name).open("w", encoding="utf-8")
        handles.append(log)
        processes.append(subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))

    def wait(url):
        deadline = monotonic() + 40
        while monotonic() < deadline:
            if any(p.poll() is not None for p in processes):
                raise RuntimeError("test service exited; inspect local server logs")
            try:
                if session.get(url, timeout=2).status_code == 200:
                    return
            except requests.RequestException:
                pass
            sleep(.2)
        raise RuntimeError("test service readiness timeout")

    def check(name, condition):
        report["checks"].append({"name": name, "passed": bool(condition)})
        if not condition:
            raise AssertionError(name)

    def request(method, path, payload=None, token=None, status=200):
        response = session.request(method, api + path, json=payload,
            headers={"Authorization": "Bearer " + token} if token else {}, timeout=15)
        check(f"{method} {path} HTTP {status}", response.status_code == status)
        return response.json()

    def account(name):
        data = request("POST", "/auth/register", {"username": name, "password": "TestPlan_20261116!"})
        tokens.append(data["access_token"])
        return data["access_token"]

    def save(token, **patch):
        before = request("GET", "/profile", token=token)["profile"]
        # 使用 API 当前版本，避免测试脚本硬编码问卷版本。
        from backend.app.risk_questionnaire import QUESTIONNAIRE_VERSION
        assessed = request("POST", "/profile/assess", {"questionnaire_version": QUESTIONNAIRE_VERSION,
            "risk_answers": {**ANSWERS, **patch}}, token)["profile"]
        assessed["version"] = before["version"]
        return request("POST", "/profile/confirm", {"profile": assessed}, token)

    try:
        start([sys.executable, "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", str(args.api_port)], "api.log")
        wait(api + "/health")
        readiness = request("GET", "/readiness")
        check("test backend has no external model or market configured", readiness.get("mode") == "rule_only"
              and readiness.get("third_party_llm_configured") is False
              and readiness.get("iwencai_skillhub_configured") is False)
        report["readiness"] = {key: readiness[key] for key in
            ("mode", "third_party_llm_configured", "iwencai_skillhub_configured")}
        first, second = account("browser_plan_a"), account("browser_plan_b")
        request("POST", "/profile/investment-plan", {}, status=401)
        empty = request("POST", "/profile/investment-plan", {}, second)
        check("second account has no allocation", empty["status"] == "needs_assessment" and not empty["allocation"])
        saved = save(first)
        before = request("GET", "/profile", token=first)
        long = request("POST", "/profile/investment-plan", {"investment_amount": "100000.01"}, first)
        check("confirmed profile produces real HTTP plan", long["status"] == "ready")
        check("amounts conserve principal to the cent", sum(Decimal(str(r["reference_amount"])) for r in long["allocation"]) == Decimal("100000.01"))
        check("plan preview does not persist changes", request("GET", "/profile", token=first) == before)
        for payload in ({"user_id": "2"}, {"risk_level": "R5"}, {"profile": saved},
                        {"investment_amount": "NaN"}, {"investment_amount": "1000.001"}, {"investment_amount": -1}):
            request("POST", "/profile/investment-plan", payload, first, status=422)
        mismatch = request("POST", "/profile/investment-plan", {"investment_amount": 200000}, first)
        check("mismatched principal blocks allocation", mismatch["status"] == "needs_information" and not mismatch["allocation"])
        save(first, q25="A")
        short = request("POST", "/profile/investment-plan", {}, first)
        check("earlier funding time changes allocation at same risk grade", short["profile_summary"]["risk_level"] == long["profile_summary"]["risk_level"] and short["allocation"][0]["reference_weight"] == 0 < long["allocation"][0]["reference_weight"])
        save(first, q23="E")
        debt = request("POST", "/profile/investment-plan", {}, first)
        check("heavy debt removes equity exposure", debt["allocation"][0]["reference_weight"] == 0)
        save(first, q26="A")
        zero = request("POST", "/profile/investment-plan", {}, first)
        check("zero loss requirement preserves cash", zero["allocation"][2]["reference_weight"] == 1)
        save(first, q25="E")
        unknown = request("POST", "/profile/investment-plan", {}, first)
        check("unknown funding date requires information", unknown["status"] == "needs_information" and not unknown["allocation"])
        save(first)
        # 退出 API 测试会话，浏览器通过真实登录页面建立独立会话。
        for token in tokens:
            request("POST", "/auth/logout", {}, token)
        tokens.clear()
        start([sys.executable, "-m", "streamlit", "run", "frontend/streamlit_app.py", "--server.address", "127.0.0.1",
               "--server.port", str(args.web_port), "--server.headless", "true", "--browser.gatherUsageStats", "false"], "frontend.log")
        wait(web + "/_stcore/health")
        check("real Streamlit page responds", session.get(web, timeout=5).status_code == 200)
        report["passed"] = True
        report["browser_url"] = web
        report["browser_test_accounts"] = ["browser_plan_a", "browser_plan_b"]
        (output / "http-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"ready": True, "checks": len(report["checks"]), "browser_url": web, "output": str(output)}), flush=True)
        if args.serve:
            deadline = monotonic() + 900
            while monotonic() < deadline and not (output / "stop").exists():
                if any(p.poll() is not None for p in processes):
                    raise RuntimeError("test service stopped unexpectedly")
                sleep(.5)
    finally:
        for token in tokens:
            try:
                session.post(api + "/auth/logout", headers={"Authorization": "Bearer " + token}, timeout=3)
            except requests.RequestException:
                pass
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for handle in handles:
            handle.close()
        report["test_services_stopped"] = True
        (output / "http-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".tmp_test" / ("personalized-live-" + datetime.now().strftime("%Y%m%d-%H%M%S")))
    parser.add_argument("--api-port", type=int, default=8765)
    parser.add_argument("--web-port", type=int, default=8516)
    parser.add_argument("--serve", action="store_true")
    run(parser.parse_args())
