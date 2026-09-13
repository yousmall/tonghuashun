"""问策智投全功能数据集的执行脚本。

它只做三件事：读取 ``examples/all_features_dataset.json``、按声明顺序调用 REST
接口、把每一步的实际结果与期望写进 ``examples/last_run_report.json``。

脚本本身不包含任何业务判断，也不包含密钥：账号密码来自数据集，外部服务密钥
仍由后端进程的 ``.env`` 决定。因此它既能当“一键演示”，也能当数据集的可执行
说明文档。

用法（先启动后端 ``python -m uvicorn backend.app.main:app --port 8000``）：

    python examples/run_all_features.py --list
    python examples/run_all_features.py                      # 全量
    python examples/run_all_features.py --only-scenarios 01,16,29,32
    python examples/run_all_features.py --group 合规闸门
    python examples/run_all_features.py --base http://127.0.0.1:8000/api/v1
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DATASET_PATH = HERE / "all_features_dataset.json"
REPORT_PATH = HERE / "last_run_report.json"


# --------------------------------------------------------------------------- #
# 数据集加载与引用解析
# --------------------------------------------------------------------------- #
class Dataset:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw
        self.personas: dict[str, Any] = raw.get("personas", {})
        self.packs: dict[str, list[dict[str, Any]]] = raw.get("fact_packs", {})
        self.api_base: str = raw["defaults"]["api_base"].rstrip("/")
        self.accounts: dict[str, dict[str, str]] = raw["defaults"]["accounts"]

    def persona(self, name: str) -> dict[str, Any]:
        try:
            return self.personas[name]
        except KeyError:
            raise SystemExit(f"数据集缺少画像 {name!r}") from None

    def pack(self, name: str) -> list[dict[str, Any]]:
        try:
            return deepcopy(self.packs[name])
        except KeyError:
            raise SystemExit(f"数据集缺少事实包 {name!r}") from None

    def resolve(self, value: Any, persona_name: str | None) -> Any:
        """解析 ``persona:x.y`` / ``packs:a,b`` / ``account:x`` 这类引用。"""
        if isinstance(value, str):
            if value.startswith("packs:"):
                facts: list[dict[str, Any]] = []
                for pack_name in value.removeprefix("packs:").split(","):
                    facts.extend(self.pack(pack_name.strip()))
                return facts
            if value.startswith("persona:"):
                rest = value.removeprefix("persona:")
                name, _, field = rest.partition(".")
                if not field:
                    return deepcopy(self.persona(name))
                return deepcopy(self.persona(name).get(field))
            if value.startswith("account:"):
                return deepcopy(self.accounts[value.removeprefix("account:")])
            return value
        if isinstance(value, list):
            resolved: list[Any] = []
            for item in value:
                part = self.resolve(item, persona_name)
                # 支持在列表里直接写 "packs:a,b"，展开为多条事实。
                if isinstance(item, str) and item.startswith("packs:"):
                    resolved.extend(part)
                else:
                    resolved.append(part)
            return resolved
        if isinstance(value, dict):
            return {key: self.resolve(item, persona_name) for key, item in value.items()}
        return value


# --------------------------------------------------------------------------- #
# 事实构造：把 snapshot_age_minutes 换成真实时间戳，并应用场景覆盖
# --------------------------------------------------------------------------- #
def build_facts(entries: list[dict[str, Any]], overrides: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    overrides = overrides or {}
    remove_ids = set(overrides.get("remove_fact_ids", []))
    set_age: dict[str, float] = overrides.get("set_age_minutes", {})
    set_quality: dict[str, float] = overrides.get("set_quality", {})
    now = datetime.now(timezone.utc)

    facts: list[dict[str, Any]] = []
    for entry in [*entries, *overrides.get("append_facts", [])]:
        fact = deepcopy(entry)
        if fact.get("fact_id") in remove_ids:
            continue
        fact_id = str(fact.get("fact_id", ""))
        age_minutes = float(set_age.get(fact_id, fact.pop("snapshot_age_minutes", 0)))
        if fact_id in set_quality:
            fact["quality"] = set_quality[fact_id]
        fact["snapshot_time"] = (now - timedelta(minutes=age_minutes)).isoformat().replace("+00:00", "Z")
        facts.append(fact)
    return facts


def apply_overrides(body: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """场景级覆盖：替换整包事实、追加事实、改时效/质量。"""

    def transform(value: Any) -> Any:
        if isinstance(value, list) and value and isinstance(value[0], dict) and "fact_id" in value[0]:
            return build_facts(value, overrides)
        if isinstance(value, dict):
            return {key: transform(item) for key, item in value.items()}
        if isinstance(value, list):
            return [transform(item) for item in value]
        return value

    body = dict(body)
    replace = overrides.get("replace_pack_items")
    if replace:
        # 先删掉与替换包同 ID 的旧事实，再由 append_facts 提供低分版本，
        # 避免出现重复 fact_id（会被 422 拒绝）或新旧并存造成的取值冲突。
        drop = {
            entry["fact_id"]
            for pack_name in replace
            for entry in DATASET.pack(pack_name)  # type: ignore[name-defined]
            if entry["fact_id"] in set(replace[pack_name])
        }
        overrides = {**overrides, "remove_fact_ids": sorted(drop)}
    return transform(body)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def http_call(
    base: str,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    raw_body: str | None = None,
    content_type: str | None = None,
) -> dict[str, Any]:
    url = f"{base}{path}"
    data: bytes | None = None
    headers: dict[str, str] = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif raw_body is not None:
        data = raw_body.encode("utf-8")
        headers["Content-Type"] = content_type or "text/plain;charset=UTF-8"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            body = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        status = exc.code
    except urllib.error.URLError as exc:
        return {"status": 0, "body": None, "error": f"无法连接 {url}：{exc.reason}"}

    parsed: Any = None
    if body.strip():
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = body[:500]
    return {"status": status, "body": parsed}


# --------------------------------------------------------------------------- #
# 断言
# --------------------------------------------------------------------------- #
_MISSING = object()


def lookup(body: Any, dotted: str) -> Any:
    current = body
    for part in dotted.split("."):
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return _MISSING
        elif isinstance(current, dict):
            if part not in current:
                return _MISSING
            current = current[part]
        else:
            return _MISSING
    return current


def matches_expected(actual: Any, wanted: Any) -> bool:
    """``*`` 只要求“字段存在且不是空值”，因此 False 和 0 也算命中。"""
    if isinstance(wanted, str) and "|" in wanted:
        return str(actual) in wanted.split("|")
    if wanted == "*":
        return actual is not None and actual not in ("", [], {})
    return actual == wanted


def check_expectation(expect: dict[str, Any], result: dict[str, Any]) -> tuple[bool, list[str]]:
    problems: list[str] = []
    expected_status = expect.get("status")
    if expected_status is not None and result["status"] != expected_status:
        problems.append(f"HTTP 状态期望 {expected_status}，实际 {result['status']}")

    body = result.get("body")
    for dotted, wanted in (expect.get("assert") or {}).items():
        if dotted == "error":
            if result["status"] < 400:
                problems.append("期望返回错误，实际为成功响应")
            continue
        actual = lookup(body, dotted.removeprefix("json."))
        if actual is _MISSING:
            problems.append(f"{dotted} 字段缺失")
            continue
        if not matches_expected(actual, wanted):
            problems.append(f"{dotted} 期望 {wanted!r}，实际 {actual!r}")
    return (not problems), problems


def summarise_body(body: Any) -> dict[str, Any]:
    """只保留可读的关键结论，报告里不落地完整证据包。"""

    if not isinstance(body, dict):
        if isinstance(body, list):
            return {"item_count": len(body)}
        return {"raw": body if not isinstance(body, (bytes, bytearray)) else "<binary>"}
    summary: dict[str, Any] = {}
    for key in ("status", "mode", "active", "username", "intent", "trace_id", "confidence", "conclusion", "provider", "kind"):
        if key in body:
            summary[key] = body[key]
    for key in ("compliance", "data_acquisition", "cross_validation", "task_plan", "profile"):
        value = body.get(key)
        if isinstance(value, dict):
            picked = {
                inner: value[inner]
                for inner in ("status", "matched_rules", "mode", "reason_code", "clarification_question",
                              "risk_level", "confirmed", "version", "consensus_score", "confidence")
                if inner in value
            }
            if isinstance(value.get("issues"), list):
                picked["issues"] = [item.get("code") for item in value["issues"]]
            summary[key] = picked
    if "missing_fields" in body:
        summary["missing_fields"] = body["missing_fields"]
    if isinstance(body.get("agent_results"), list):
        summary["agent_results"] = [
            {
                "agent_id": item.get("agent_id"),
                "status": item.get("status"),
                "score": item.get("score"),
                "confidence": item.get("confidence"),
                "facts_used": len(item.get("facts_used") or []),
                "risk_flags": item.get("risk_flags"),
                "rejected_candidates": (item.get("details") or {}).get("rejected_candidates"),
                "has_time_horizon_conflict": (item.get("details") or {}).get("has_time_horizon_conflict"),
            }
            for item in body["agent_results"]
        ]
    if isinstance(body.get("facts"), list):
        summary["facts_returned"] = len(body["facts"])
    if isinstance(body.get("allocation"), list):
        summary["allocation"] = body["allocation"]
    if isinstance(body.get("messages"), list):
        summary["messages"] = len(body["messages"])
    if "detail" in body:
        summary["detail"] = body["detail"]
    return summary or {"keys": sorted(body)[:12]}


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #
def run(args: argparse.Namespace) -> int:
    global DATASET
    DATASET = Dataset(json.loads(DATASET_PATH.read_text(encoding="utf-8")))
    base = (args.base or DATASET.api_base).rstrip("/")

    scenarios: list[dict[str, Any]] = DATASET.raw["scenarios"]
    if args.list:
        for scenario in scenarios:
            print(f"{scenario['id']}  [{scenario['group']}] {scenario['title']}")
        return 0

    selected_ids = set()
    if args.only_scenarios:
        selected_ids = {item.strip() for item in args.only_scenarios.split(",") if item.strip()}
    chosen = [
        scenario for scenario in scenarios
        if (not selected_ids or scenario["id"] in selected_ids)
        and (not args.group or scenario["group"] == args.group)
    ]
    if not chosen:
        print("没有匹配的场景；用 --list 查看可用编号与分组。", file=sys.stderr)
        return 2

    tokens: dict[str, str] = {}
    beacons: dict[str, str] = {}
    snapshots: dict[str, str] = {}
    results: list[dict[str, Any]] = []

    def authed(name: str | None) -> str | None:
        if name is None:
            return None
        if name.endswith("-raw"):
            return snapshots.get(name.removesuffix("-raw"))
        return tokens.get(name)

    # 预置账号：注册失败（已存在）即改为登录，保证脚本可重复执行。
    for tag, credentials in DATASET.accounts.items():
        register = http_call(base, "POST", "/auth/register", payload=credentials)
        response = register if register["status"] == 200 else http_call(
            base, "POST", "/auth/login", payload=credentials)
        if response["status"] == 200 and isinstance(response["body"], dict):
            tokens[tag] = response["body"]["access_token"]
            beacons[tag] = response["body"]["session_beacon_token"]
            snapshots[tag] = tokens[tag]
            print(f"[setup] 账号 {credentials['username']} 就绪（{'注册' if register['status'] == 200 else '登录'}）")
        else:
            print(f"[setup] 账号 {credentials['username']} 不可用：HTTP {response['status']} {response.get('error', '')}",
                  file=sys.stderr)
            return 3

    for scenario in chosen:
        persona_name = scenario.get("persona")
        call = scenario["call"]
        bodies = call.get("json_sequence") or [call.get("json")]
        sub_results: list[dict[str, Any]] = []
        for body in bodies:
            payload: dict[str, Any] | None = None
            if body is not None:
                resolved = DATASET.resolve(deepcopy(body), persona_name)
                overrides = resolved.pop("overrides", {})
                if persona_name:
                    profile = resolved.get("profile")
                    if isinstance(profile, dict):
                        resolved["profile"] = {
                            **profile,
                            "user_id": profile.pop("user_id", ""),
                            "confirmed": True,
                        }
                payload = apply_overrides(resolved, overrides)
            raw_body = call.get("raw_body")
            if isinstance(raw_body, str) and raw_body.startswith("beacon:"):
                raw_body = beacons.get(raw_body.removeprefix("beacon:"), "")

            response = http_call(
                base,
                call.get("method", "GET"),
                call["path"],
                payload=payload,
                token=authed(scenario.get("auth")),
                raw_body=raw_body,
                content_type=call.get("content_type"),
            )
            sub_results.append({
                "request": {"method": call.get("method", "GET"), "path": call["path"], "payload": payload},
                "response": {"status": response["status"], "summary": summarise_body(response["body"])},
                "raw_body": response["body"],
            })

        # 断言只作用于第一个子请求（多子请求场景以覆盖面为主）。
        passed, problems = check_expectation(scenario["expect"], {
            "status": sub_results[0]["response"]["status"], "body": sub_results[0]["raw_body"]})
        if call.get("json_sequence"):
            statuses = [item["response"]["status"] for item in sub_results]
            if any(code >= 500 for code in statuses):
                problems.append(f"存在服务端错误状态：{statuses}")
                passed = False

        # 绑定与解绑令牌
        bind = scenario["expect"].get("bind")
        if bind and passed:
            for item in bind.split("|"):
                kind, _, tag = item.partition(":")
                if kind == "token":
                    payload_body = sub_results[0]["raw_body"]
                    if isinstance(payload_body, dict):
                        tokens[tag] = payload_body["access_token"]
                        snapshots[tag] = tokens[tag]
                elif kind == "beacon":
                    payload_body = sub_results[0]["raw_body"]
                    if isinstance(payload_body, dict):
                        beacons[tag] = payload_body["session_beacon_token"]
        unbind = scenario["expect"].get("unbind")
        if unbind:
            for item in unbind.split("|"):
                kind, _, tag = item.partition(":")
                if kind == "token":
                    tokens.pop(tag, None)

        results.append({
            "id": scenario["id"],
            "group": scenario["group"],
            "title": scenario["title"],
            "passed": passed,
            "problems": problems,
            "expect_note": scenario["expect"].get("note"),
            "calls": [
                {"request": item["request"], "response": item["response"]}
                for item in sub_results
            ],
        })
        marker = "PASS" if passed else "FAIL"
        detail = "" if passed else "  ← " + "；".join(problems)
        print(f"[{marker}] {scenario['id']} {scenario['title']}{detail}")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "api_base": base,
        "dataset": str(DATASET_PATH.name),
        "scenario_count": len(results),
        "passed": sum(1 for item in results if item["passed"]),
        "failed": sum(1 for item in results if not item["passed"]),
        "scenarios": results,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n共 {report['scenario_count']} 个场景：通过 {report['passed']}，失败 {report['failed']}")
    print(f"报告已写入 {REPORT_PATH}")
    print("提示：LLM / 问财未配置或额度受限时，语义类与取数类场景会给出降级结果而不是崩溃；"
          "报告里的 expect_note 说明了该场景的可接受区间。")
    return 0 if report["failed"] == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="运行问策智投全功能输入数据集")
    parser.add_argument("--list", action="store_true", help="只列出场景编号、分组与标题")
    parser.add_argument("--base", default=None, help="覆盖 API 基址，默认取数据集中的数据")
    parser.add_argument("--only-scenarios", default=None, help="逗号分隔的场景编号，例如 01,16,29")
    parser.add_argument("--group", default=None, help="只运行某个分组，例如“合规闸门”")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
