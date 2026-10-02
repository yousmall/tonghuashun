"""仅调用一次真实语义模型，验证五入口分类；不访问行情、数据库或保存敏感信息。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from backend.app.agents.llm_agents import LLMConfig, OpenAICompatibleLLM
from backend.app.models import Intent, OrchestrationRequest
from backend.app.semantic import SemanticService


async def main() -> int:
    load_dotenv(ROOT / ".env")
    config = LLMConfig.from_env()
    if config is None:
        print("模型未配置；无法完成实时入口分类。")
        return 2
    client = OpenAICompatibleLLM(config)
    cases = (
        ("市场解读", "请解读当前沪深300指数和宏观市场环境", Intent.MARKET_ANALYSIS),
        ("行业分析", "比较半导体与白酒行业的景气和估值", Intent.INDUSTRY_ANALYSIS),
        ("基金筛选", "筛选沪深A股中非ST公司，ROE大于8%，资产负债率低于70%", Intent.SECURITY_RESEARCH),
        ("基金筛选", "筛选管理费率低的沪深300 ETF", Intent.FUND_SCREENING),
        ("可转债分析", "分析某只可转债的转股溢价率和到期收益率", Intent.CONVERTIBLE_BOND_ANALYSIS),
        ("市场解读", "帮我制定旅游路线", Intent.UNKNOWN),
    )
    try:
        semantic = SemanticService(client)
        results = []
        for direction, question, expected in cases:
            result = await semantic.understand(OrchestrationRequest(
                query=f"{direction}：{question}", research_direction=direction,
                profile={"user_id": "SYNTHETIC_ROUTE_SMOKE", "confirmed": True},
            ))
            passed = result.intent is expected
            results.append(passed)
            print(f"{direction}：{'通过' if passed else '未通过'}；结果={result.intent.value}；预期={expected.value}；置信度={result.confidence:.2f}")
            if not passed:
                print(f"模型说明：{result.reason}")
    finally:
        await client.aclose()
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(main()))
