"""少量真实只读探针：记录行情/新闻/研报接入状态，不保存密钥或行情正文。"""
import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
from backend.app.data_provider import IwencaiSkillHubProvider


async def run(output):
    load_dotenv(ROOT / ".env")
    provider = IwencaiSkillHubProvider.from_env()
    report = {"time": datetime.now(timezone.utc).isoformat(), "scope": "single_authorized_provider_three_information_types",
              "capture_time_is_not_exchange_timestamp": True, "checks": []}
    if provider is None:
        report["status"] = "not_configured"
    else:
        async def probe(kind, method):
            started = perf_counter()
            try:
                facts = await asyncio.wait_for(method("600519"), timeout=30)
                substantive = [fact for fact in facts if fact.field != "provider_response"]
                return {"kind": kind, "status": "ok" if substantive else "empty", "fact_count": len(substantive),
                        "fields": sorted({f.field for f in substantive}), "source_ids": sorted({f.source_id for f in substantive}),
                        "original_link_count": sum(bool(f.source_url) for f in substantive),
                        "elapsed_seconds": round(perf_counter() - started, 3)}
            except Exception as exc:
                errors = []
                while exc is not None:
                    item = {"type": type(exc).__name__}
                    if getattr(exc, "response", None) is not None:
                        item["http_status"] = exc.response.status_code
                    errors.append(item)
                    exc = exc.__cause__
                return {"kind": kind, "status": "unavailable", "errors": errors,
                        "elapsed_seconds": round(perf_counter() - started, 3)}
        try:
            report["checks"] = await asyncio.gather(
                probe("quote", provider.get_quote), probe("news", provider.get_news),
                probe("research_report", provider.get_research_reports))
        finally:
            await provider.aclose()
        report["status"] = "ok" if all(item["status"] == "ok" for item in report["checks"]) else "partial_or_unavailable"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "deliverables/requirements_live_sources.json")
    raise SystemExit(asyncio.run(run(parser.parse_args().output)))
