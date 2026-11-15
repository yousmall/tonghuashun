"""把用户输入的名称或代码解析为数据源核准的证券简称。

只做一次只读查询，并且要求返回记录的证券代码与输入一致：名称相近但身份
不同的记录不会被当作该标的的名称。取不到时保持为空，由后续行情返回补齐，
绝不猜测或按字符串相似度替换。
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

QUERY_FIELDS = {
    "指数": ("hithink-market-query", "指数简称、指数代码"),
    "股票": ("hithink-market-query", "股票简称、股票代码"),
    "基金": ("hithink-fund-query", "基金简称、基金代码"),
    "可转债": ("hithink-market-query", "可转债简称、可转债代码"),
    "行业": ("hithink-industry-query", "板块简称、板块代码"),
}
CODE = re.compile(r"(?<!\d)(\d{5,6})(?:\.([A-Za-z]{2}))?(?!\d)")
INVALID_NAMES = {"多证券资料", "多证券", "未知", "—", "-"}


def _clean(name: Any) -> str | None:
    text = re.sub(r"\s+", " ", str(name or "")).strip()
    if not text or text in INVALID_NAMES or len(text) > 60:
        return None
    return text


def security_name(facts: list[Any], target: str) -> str | None:
    """从返回记录中挑出与输入身份一致的一条名称。"""

    match = CODE.search(str(target))
    expected = match.group(1) if match else None
    suffix = match.group(2).upper() if match and match.group(2) else None
    fallback = None
    for fact in facts:
        name = _clean(getattr(fact, "entity", None))
        if not name:
            continue
        code = str(getattr(fact, "entity_code", "") or "").upper()
        digits = code.split(".")[0]
        if expected is None:
            fallback = fallback or name
            continue
        if digits != expected:
            continue
        if suffix and "." in code and code.split(".")[1] != suffix:
            continue
        return name
    # 只有按名称查询（输入不是代码）时才接受唯一返回的名称。
    return fallback if expected is None else None


async def resolve_security_name(provider: Any, target: str, asset_type: str) -> str | None:
    """尽力解析名称；数据源未配置、超时或身份不符时返回 ``None``。"""

    identity = await resolve_security_identity(provider, target, asset_type)
    return identity[0] if identity else None


async def resolve_security_identity(provider: Any, target: str, asset_type: str) -> tuple[str, str] | None:
    """仅接受唯一的名称和代码组合；名称查询保存代码，便于跨页面再次打开。"""
    if provider is None or asset_type not in QUERY_FIELDS:
        return None
    skill, fields = QUERY_FIELDS[asset_type]
    try:
        facts = await asyncio.wait_for(
            provider.query(f"{target}的{fields}", limit=2, skill_id=skill), timeout=8)
    except asyncio.CancelledError:
        raise
    except (RuntimeError, TimeoutError, ValueError):
        # 名称只是展示优化：数据源不可用时保留原始输入，不阻断添加自选。
        return None
    identities = [(security_name([fact], target), str(fact.entity_code or "").upper()) for fact in facts]
    unique = list(dict.fromkeys((name, code) for name, code in identities
                               if name and re.fullmatch(r"\d{5,6}(?:\.[A-Z]{2})?", code)))
    return unique[0] if len(unique) == 1 else None
