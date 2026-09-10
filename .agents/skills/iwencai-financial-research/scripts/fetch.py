"""Small CLI for the project's read-only iWenCai SkillHub adapter."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

load_dotenv(REPOSITORY_ROOT / ".env")

from backend.app.data_provider import IwencaiSkillHubProvider  # noqa: E402


METHODS = {
    "quote": "get_quote",
    "financial": "get_financial_metrics",
    "news": "get_news",
    "fund": "get_fund_candidates",
    "industry": "get_industry_rank",
    "convertible": "get_convertible_bond",
    "basic_info": "get_basic_info",
    "company_operations": "get_company_operations",
    "shareholder_equity": "get_shareholder_equity",
    "event": "get_event_data",
    "macro": "get_macro_data",
    "institutional_research": "get_institutional_research",
    "research_report": "get_research_reports",
    "announcement": "get_announcements",
    "stock_screen": "screen_stocks",
    "sector_screen": "screen_sectors",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch normalized read-only facts from iWenCai SkillHub")
    parser.add_argument("kind", choices=sorted(METHODS))
    parser.add_argument("target", help="Symbol, entity, time window, or natural-language screening query")
    parser.add_argument("--filters", default="{}", help="JSON object used by the fund capability")
    return parser.parse_args()


async def run() -> int:
    args = parse_args()
    provider = IwencaiSkillHubProvider.from_env()
    if provider is None:
        print("IWENCAI_API_KEY is not configured; the project remains in snapshot/rule-only mode.", file=sys.stderr)
        return 2

    try:
        if args.kind == "fund":
            filters = json.loads(args.filters)
            if not isinstance(filters, dict):
                print("--filters must be a JSON object", file=sys.stderr)
                return 2
            facts = await provider.get_fund_candidates(filters)
        else:
            method = getattr(provider, METHODS[args.kind])
            facts = await method(args.target)
    except json.JSONDecodeError as exc:
        print(f"--filters must be a JSON object: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps([fact.model_dump(mode="json") for fact in facts], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
