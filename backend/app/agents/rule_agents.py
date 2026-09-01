"""五类确定性专业智能体的 MVP 实现。

它们不是证券研究模型，也不会拉取实时数据。每个智能体只读取 ``FactRecord``
中已经被调用方授权的字段，缺少所需字段时返回 ``DEGRADED``，从而让后续的
事实核验和合规层能够安全地将结果转为教育性说明或追问。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from backend.app.agents.base import BaseAgent, clamp_score, evidence_metadata, numeric_fact_values
from backend.app.models import AgentResult, FactRecord, OrchestrationRequest, TaskStatus


def _field_matches(fact: FactRecord, names: set[str]) -> bool:
    """字段匹配统一小写化，兼容快照中常见的 ``sector_weight`` 等命名。"""
    return fact.field.lower() in names


def _matching_facts(facts: Iterable[FactRecord], names: set[str]) -> list[FactRecord]:
    """只返回本 Agent 声明需要的事实；不匹配的数据绝不会被悄悄引用。"""
    return [fact for fact in facts if _field_matches(fact, names)]


def _degraded(agent_id: str, missing: list[str], *, details: dict[str, Any] | None = None) -> AgentResult:
    """统一缺数据降级格式，便于前端展示“缺什么、下一步补什么”。"""
    return AgentResult(
        agent_id=agent_id,
        status=TaskStatus.DEGRADED,
        opinion="授权事实不足，暂不形成强结论。",
        confidence=0,
        confidence_reasons=[f"缺少字段：{', '.join(missing)}"],
        risk_flags=["证据不足"],
        invalidation_conditions=["补充缺失字段后重新核验"],
        details=details or {"missing_fields": missing},
    )


class MacroAgent(BaseAgent):
    """宏观/市场状态规则智能体。

    字段采用文档约定的 0-100 标准分。MVP 只在所有五个维度齐备时产生综合分，
    避免把零散经济数据包装成完整的市场观点。
    """

    agent_id = "market"
    _weights = {
        "growth_score": 0.25,
        "inflation_score": 0.20,
        "liquidity_score": 0.25,
        "policy_score": 0.15,
        "risk_appetite_score": 0.15,
    }

    async def run(self, request: OrchestrationRequest) -> AgentResult:
        by_field = {fact.field.lower(): fact for fact in request.facts}
        missing = [field for field in self._weights if field not in by_field]
        if missing:
            return _degraded(self.agent_id, missing)
        selected = [by_field[field] for field in self._weights]
        numeric = numeric_fact_values(selected)
        if len(numeric) != len(selected):
            return _degraded(self.agent_id, ["宏观评分字段必须为数值"])
        score = sum(by_field[field].value * weight for field, weight in self._weights.items())
        fact_ids, citations = evidence_metadata(selected)
        confidence = round(sum(fact.quality for fact in selected) / len(selected), 2)
        state = "偏积极" if score >= 60 else "偏谨慎" if score < 40 else "中性"
        return AgentResult(
            agent_id=self.agent_id,
            status=TaskStatus.COMPLETED,
            opinion=f"基于五个已授权宏观维度，市场环境为{state}。",
            score=clamp_score(score),
            confidence=confidence,
            facts_used=fact_ids,
            citations=citations,
            risk_flags=[] if 40 <= score < 60 else ["市场状态可能变化，需按快照时点复核"],
            invalidation_conditions=["任一宏观评分维度发生实质变化", "快照超过时效阈值"],
            details={"dimension_scores": {field: by_field[field].value for field in self._weights}},
        )


class IndustryAgent(BaseAgent):
    """行业景气、估值、资金、政策与拥挤度的可重算加权评分。"""

    agent_id = "industry"
    _weights = {
        "prosperity_score": 0.30,
        "valuation_score": 0.20,
        "capital_flow_score": 0.20,
        "policy_score": 0.15,
        "crowding_score": 0.15,
    }

    async def run(self, request: OrchestrationRequest) -> AgentResult:
        selected = _matching_facts(request.facts, set(self._weights))
        # 行业快照至少须覆盖一个行业实体的全部五维，避免跨行业拼分数。
        by_entity: dict[str, dict[str, FactRecord]] = defaultdict(dict)
        for fact in selected:
            by_entity[fact.entity][fact.field.lower()] = fact
        complete = [(entity, fields) for entity, fields in by_entity.items() if set(self._weights) <= set(fields)]
        if not complete:
            return _degraded(self.agent_id, list(self._weights), details={"available_entities": sorted(by_entity)})
        entity, fields = sorted(complete, key=lambda item: item[0])[0]
        chosen = [fields[name] for name in self._weights]
        if len(numeric_fact_values(chosen)) != len(chosen):
            return _degraded(self.agent_id, ["行业评分字段必须为数值"])
        score = sum(float(fields[name].value) * weight for name, weight in self._weights.items())
        fact_ids, citations = evidence_metadata(chosen)
        constraint_hit = any(entity.lower() in constraint.lower() for constraint in request.profile.constraints)
        risk_flags = ["行业与用户禁忌可能冲突"] if constraint_hit else []
        return AgentResult(
            agent_id=self.agent_id,
            status=TaskStatus.COMPLETED,
            opinion=f"{entity}行业的可重算综合评分已生成；正反催化剂需随快照复核。",
            score=clamp_score(score),
            confidence=round(sum(f.quality for f in chosen) / len(chosen), 2),
            facts_used=fact_ids,
            citations=citations,
            risk_flags=risk_flags,
            invalidation_conditions=["行业景气、资金或政策事实更新", "行业集中度触及用户上限"],
            details={"industry": entity, "dimension_scores": {name: fields[name].value for name in self._weights}},
        )


class StockAgent(BaseAgent):
    """个股研究 MVP：保留基本面与技术面，而不是伪造单一确定信号。"""

    agent_id = "security"
    _fields = {"fundamental_score", "valuation_score", "technical_score", "event_score", "governance_score"}

    async def run(self, request: OrchestrationRequest) -> AgentResult:
        selected = _matching_facts(request.facts, self._fields)
        if not selected:
            return _degraded(self.agent_id, sorted(self._fields))
        # 同一实体的字段才可放在一起讨论，取字段最齐全的实体且不补造缺项。
        by_entity: dict[str, list[FactRecord]] = defaultdict(list)
        for fact in selected:
            by_entity[fact.entity].append(fact)
        entity, chosen = max(by_entity.items(), key=lambda item: len(item[1]))
        numeric = numeric_fact_values(chosen)
        if not numeric:
            return _degraded(self.agent_id, ["至少一个数值评分字段"])
        by_field = {fact.field.lower(): value for fact, value in numeric}
        score = clamp_score(sum(by_field.values()) / len(by_field))
        fundamental = by_field.get("fundamental_score")
        technical = by_field.get("technical_score")
        conflict = fundamental is not None and technical is not None and abs(fundamental - technical) >= 25
        fact_ids, citations = evidence_metadata(chosen)
        return AgentResult(
            agent_id=self.agent_id,
            status=TaskStatus.COMPLETED if len(chosen) >= 2 else TaskStatus.DEGRADED,
            opinion=(
                f"{entity}的基本面与技术面存在时间维度分歧，应分别展示长期与短期判断。"
                if conflict else f"{entity}的可用研究维度已按授权快照汇总。"
            ),
            score=score,
            confidence=round(sum(f.quality for f in chosen) / len(chosen) * min(1, len(chosen) / 3), 2),
            confidence_reasons=[] if len(chosen) >= 2 else ["研究维度不足"],
            facts_used=fact_ids,
            citations=citations,
            risk_flags=["基本面与技术面分歧"] if conflict else [],
            invalidation_conditions=["财报期或价格快照更新", "治理或事件事实发生变化"],
            details={"security": entity, "dimension_scores": by_field, "has_time_horizon_conflict": conflict},
        )


class FundAgent(BaseAgent):
    """基金/ETF 准入优先于排序的规则智能体。"""

    agent_id = "fund"
    _fields = {"fund_risk_level", "fee_rate", "tracking_error", "fund_score", "liquidity_score"}

    async def run(self, request: OrchestrationRequest) -> AgentResult:
        selected = _matching_facts(request.facts, self._fields)
        if not selected:
            return _degraded(self.agent_id, sorted(self._fields))
        by_entity: dict[str, list[FactRecord]] = defaultdict(list)
        for fact in selected:
            by_entity[fact.entity].append(fact)
        eligible: list[tuple[str, list[FactRecord], float]] = []
        rejected: list[str] = []
        user_risk = int(request.profile.risk_level[1:]) if request.profile.risk_level and request.profile.risk_level.startswith("R") and request.profile.risk_level[1:].isdigit() else None
        for entity, facts in by_entity.items():
            values = {fact.field.lower(): fact.value for fact in facts}
            product_risk = values.get("fund_risk_level")
            if user_risk is not None and isinstance(product_risk, (int, float)) and product_risk > user_risk:
                rejected.append(entity)
                continue
            quality = [value for _, value in numeric_fact_values(facts) if value is not None]
            # fund_score 是首选；不存在时只用已有数值做透明的降级排序。
            score = float(values["fund_score"]) if isinstance(values.get("fund_score"), (int, float)) else (sum(quality) / len(quality) if quality else 0)
            eligible.append((entity, facts, clamp_score(score)))
        if not eligible:
            return AgentResult(
                agent_id=self.agent_id,
                status=TaskStatus.DEGRADED,
                opinion="没有满足当前风险准入条件的基金/ETF 候选。",
                confidence=0,
                confidence_reasons=["候选均未通过风险等级准入"],
                risk_flags=["无匹配产品"],
                invalidation_conditions=["补充更低风险候选或更新画像"],
                details={"rejected_candidates": rejected},
            )
        entity, facts, score = max(eligible, key=lambda item: item[2])
        fact_ids, citations = evidence_metadata(facts)
        return AgentResult(
            agent_id=self.agent_id,
            status=TaskStatus.COMPLETED,
            opinion=f"{entity}通过风险准入并在可用候选中具有最高透明评分；不代表收益承诺。",
            score=score,
            confidence=round(sum(f.quality for f in facts) / len(facts), 2),
            facts_used=fact_ids,
            citations=citations,
            risk_flags=["候选比较受快照覆盖范围限制"],
            invalidation_conditions=["费率、跟踪质量、流动性或用户约束变化"],
            details={"primary_candidate": entity, "rejected_candidates": rejected, "eligible_count": len(eligible)},
        )


class PortfolioAgent(BaseAgent):
    """组合诊断：只做集中度识别与目标区间提示，绝不下单或生成精确交易指令。"""

    agent_id = "portfolio"
    _weight_fields = {"weight", "portfolio_weight", "sector_weight"}

    async def run(self, request: OrchestrationRequest) -> AgentResult:
        selected = _matching_facts(request.facts, self._weight_fields)
        # 持仓快照也可带 weight；转换时没有引用 ID，因此只把它作为辅助风险提示，
        # 不把其数值作为可引用证据写入 AgentResult。
        fact_weights = [(fact, value) for fact, value in numeric_fact_values(selected)]
        if not fact_weights:
            return _degraded(self.agent_id, ["weight 或 portfolio_weight"])
        total_weight = sum(value for _, value in fact_weights)
        largest_fact, largest_weight = max(fact_weights, key=lambda item: item[1])
        concentration_excess = largest_weight > request.profile.single_security_limit
        fact_ids, citations = evidence_metadata(fact for fact, _ in fact_weights)
        risk_flags = []
        if concentration_excess:
            risk_flags.append("单标的集中度超限")
        if total_weight > 1.05:
            risk_flags.append("持仓权重和超过 100%，请检查口径")
        target_range = "以较高流动性和较低波动资产为主" if request.profile.liquidity_need == "高" or (request.profile.max_drawdown is not None and request.profile.max_drawdown <= 0.08) else "按已确认风险等级设定资产区间"
        return AgentResult(
            agent_id=self.agent_id,
            status=TaskStatus.COMPLETED,
            opinion="组合已完成规则型集中度诊断；调整应分批执行并在新快照下复核。",
            score=clamp_score((1 - min(largest_weight / request.profile.single_security_limit, 2) / 2) * 100),
            confidence=round(sum(fact.quality for fact, _ in fact_weights) / len(fact_weights), 2),
            facts_used=fact_ids,
            citations=citations,
            risk_flags=risk_flags,
            invalidation_conditions=["持仓权重、用户约束或快照时点变化"],
            details={
                "largest_position": largest_fact.entity,
                "largest_weight": largest_weight,
                "total_weight": total_weight,
                "single_security_limit": request.profile.single_security_limit,
                "target_range_note": target_range,
            },
        )


def make_rule_agents() -> dict[str, Any]:
    """构造协调器所需的五个实例，注册键与 ``CoordinatorAgent`` 映射一致。"""
    agents: list[BaseAgent] = [MacroAgent(), IndustryAgent(), StockAgent(), FundAgent(), PortfolioAgent()]
    return {agent.agent_id: agent.run for agent in agents}
