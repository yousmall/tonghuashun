"""只使用最终核验认可的引用生成可复算价格情景，不填补预测数据。"""
from __future__ import annotations

import math
from datetime import date

from backend.app.models.schemas import (
    ComplianceStatus, FactRecord, ReturnExpectation, ReturnScenario, UserProfile,
)


def _price(fact: FactRecord) -> float | None:
    if isinstance(fact.value, bool):
        return None
    try:
        value = float(str(fact.value).replace(",", "").strip())
    except (ValueError, TypeError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _currency(fact: FactRecord) -> str | None:
    units = {"元": "CNY", "人民币": "CNY", "CNY": "CNY", "RMB": "CNY",
             "港元": "HKD", "港币": "HKD", "HKD": "HKD", "美元": "USD", "USD": "USD"}
    explicit = units.get((fact.unit or "").upper())
    detected = next((currency for label, currency in units.items()
                     if label in (fact.source_field or "") and label not in {"元"}), None)
    if explicit and detected and explicit != detected:
        return None
    return explicit or detected


def _date(fact: FactRecord) -> str | None:
    # 抓取时间不能替代行情日期或研报发布日期。
    raw = fact.observation_date.isoformat() if fact.observation_date else fact.period
    if not raw:
        return None
    if len(raw) == 8 and raw.isdigit():
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    try:
        stamp = date.fromisoformat(raw)
    except ValueError:
        return None
    return raw if stamp <= fact.snapshot_time.date() else None


def build_return_expectation(
    facts: list[FactRecord], evidence: list[str], profile: UserProfile, status: ComplianceStatus,
) -> ReturnExpectation:
    if status == ComplianceStatus.BLOCK or not profile.confirmed:
        return ReturnExpectation(status="blocked", summary="风险检查或画像确认尚未通过，暂不提供收益情景。")
    output = ReturnExpectation(user_goal_annual=profile.expected_annual_return,
                               investment_horizon_months=profile.horizon_months)
    accepted = set(evidence)
    relevant = [fact for fact in facts if fact.fact_id in accepted
                and fact.field in {"close_price", "target_price"}]
    for target in relevant:
        if target.field != "target_price":
            continue
        currency, target_value, target_date = _currency(target), _price(target), _date(target)
        if not currency or target_value is None or not target_date:
            continue
        # 双方有代码时按完整代码匹配；单方有代码不能靠同名跨市场拼接。
        candidates = [fact for fact in relevant if fact.field == "close_price"
                      and ((target.entity_code and fact.entity_code == target.entity_code)
                           or (not target.entity_code and not fact.entity_code and fact.entity == target.entity))
                      and _currency(fact) == currency and _date(fact)]
        if not candidates:
            continue
        latest = max(_date(fact) for fact in candidates)
        candidates = [fact for fact in candidates if _date(fact) == latest]
        values = {_price(fact) for fact in candidates}
        # 同日价格分歧、无效数值或目标资料晚于参考价时，不任意选一个数字。
        if len(values) != 1 or None in values or target_date > latest:
            continue
        price = candidates[0]
        current = _price(price)
        change = target_value / current - 1
        if not math.isfinite(change):
            continue
        output.scenarios.append(ReturnScenario(
            entity=target.entity, current_price=current, target_price=target_value,
            price_return=change, currency=currency, price_date=latest, target_date=target_date,
            evidence=[price.fact_id, target.fact_id],
        ))
    if output.scenarios:
        output.status = "scenario"
        output.summary = "以下是已引用机构目标价对应的价格情景空间，需结合原文复核，不能作为收益预测。"
    return output
