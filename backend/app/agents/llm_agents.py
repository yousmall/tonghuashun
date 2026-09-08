"""可选的第三方大模型投研层。

默认环境没有密钥时继续使用确定性规则 Agent；配置 OpenAI-compatible 服务后，
本模块把规则结果、已确认画像和授权 FactRecord 交给模型做主题研判与解释。
模型只能返回结构化 AgentResult，引用越权、解析失败或远程故障都会安全回退。
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import ValidationError

from backend.app.agents.base import BaseAgent
from backend.app.agents.rule_agents import make_rule_agents
from backend.app.models import AgentResult, OrchestrationRequest, TaskStatus


ROLE_INSTRUCTIONS = {
    "market": "研判宏观与市场环境，解释增长、通胀、流动性、政策和风险偏好。",
    "industry": "跟踪行业景气、估值、资金、政策与拥挤度，列出正反催化剂。",
    "security": "分别分析基本面、估值、技术面、事件与治理，不给收益承诺。",
    "fund": "先执行风险等级准入，再比较费率、跟踪质量、流动性和候选评分。",
    "portfolio": "分析集中度、期限和流动性适配，只给目标区间与复核条件，不下单。",
}


@dataclass(frozen=True)
class LLMConfig:
    """第三方 OpenAI-compatible Chat Completions 配置。"""

    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 8.0
    max_retries: int = 2

    @classmethod
    def from_env(cls) -> "LLMConfig | None":
        base_url = os.getenv("WENCE_LLM_BASE_URL", "").strip().rstrip("/")
        api_key = os.getenv("WENCE_LLM_API_KEY", "").strip()
        model = os.getenv("WENCE_LLM_MODEL", "").strip()
        if not (base_url and api_key and model):
            return None
        return cls(base_url=base_url, api_key=api_key, model=model)


class OpenAICompatibleLLM:
    """最小、可测试的 OpenAI-compatible 客户端，含重试和结构化输出。"""

    def __init__(self, config: LLMConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self.transport = transport

    async def complete_json(self, *, system: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_body = {
            "model": self.config.model,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
            ],
        }
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    transport=self.transport,
                    timeout=self.config.timeout_seconds,
                ) as client:
                    response = await client.post(
                        f"{self.config.base_url}/chat/completions",
                        headers=headers,
                        json=request_body,
                    )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                if isinstance(content, dict):
                    return content
                return json.loads(content)
            except (httpx.HTTPError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < self.config.max_retries:
                    await asyncio.sleep(0.1 * (2**attempt))
        raise RuntimeError("第三方大模型调用失败") from last_error


class HybridInvestmentAgent(BaseAgent):
    """规则计算打底、大模型负责研判解释的混合专业 Agent。"""

    def __init__(self, agent_id: str, rule_handler: Any, llm: OpenAICompatibleLLM) -> None:
        self.agent_id = agent_id
        self.rule_handler = rule_handler
        self.llm = llm

    async def run(self, request: OrchestrationRequest) -> AgentResult:
        baseline: AgentResult = await self.rule_handler(request)
        if not request.facts:
            return baseline
        allowed_ids = {fact.fact_id for fact in request.facts}
        facts_by_id = {fact.fact_id: fact for fact in request.facts}
        payload = {
            "query": request.query,
            "conversation": [turn.model_dump(mode="json") for turn in request.context_messages[-10:]],
            "profile": request.profile.model_dump(mode="json"),
            "authorized_facts": [fact.model_dump(mode="json") for fact in request.facts],
            "rule_baseline": baseline.model_dump(mode="json"),
            "required_output": {
                "agent_id": self.agent_id,
                "status": "completed|degraded|unknown",
                "opinion": "仅依据 authorized_facts 的研判",
                "score": "0-100 或 null",
                "confidence": "0-1",
                "confidence_reasons": [],
                "facts_used": ["授权 fact_id"],
                "risk_flags": [],
                "invalidation_conditions": [],
                "details": {},
            },
        }
        system = (
            "你是证券投研辅助系统中的受限专业智能体。"
            + ROLE_INSTRUCTIONS[self.agent_id]
            + "只能使用 authorized_facts 中的信息和数值；缺数据必须降级，不得补造事实。"
            "输出单个 JSON 对象，不要 Markdown，不得承诺收益或给出自动交易指令。"
        )
        try:
            raw = await self.llm.complete_json(system=system, payload=payload)
            raw["agent_id"] = self.agent_id
            raw.setdefault("status", TaskStatus.COMPLETED)
            raw.setdefault("confidence", 0)
            raw.setdefault("opinion", baseline.opinion)
            raw.setdefault("facts_used", [])
            candidate = AgentResult.model_validate(raw)
            self.ensure_fact_only(candidate.facts_used, request.facts)
            # citations 由受信事实层重建，不接受模型自行填写的数据来源。
            candidate.citations = sorted({facts_by_id[fact_id].source_id for fact_id in candidate.facts_used})
            if not candidate.facts_used:
                candidate.status = TaskStatus.DEGRADED
                candidate.confidence = min(candidate.confidence, 0.3)
                candidate.confidence_reasons.append("大模型输出没有可核验事实引用")
            candidate.details = {**candidate.details, "engine": "third_party_llm", "model": self.llm.config.model}
            return candidate
        except (RuntimeError, ValidationError, ValueError):
            fallback = baseline.model_copy(deep=True)
            fallback.confidence_reasons.append("第三方大模型不可用或输出未通过结构化校验，已回退规则引擎")
            fallback.details = {**fallback.details, "engine": "rule_fallback"}
            return fallback


def make_investment_agents() -> tuple[dict[str, Any], bool]:
    """根据环境变量装配混合 Agent；返回注册表及 LLM 是否已启用。"""

    rule_agents = make_rule_agents()
    config = LLMConfig.from_env()
    if config is None:
        return rule_agents, False
    client = OpenAICompatibleLLM(config)
    hybrid = {
        agent_id: HybridInvestmentAgent(agent_id, handler, client).run
        for agent_id, handler in rule_agents.items()
    }
    return hybrid, True
