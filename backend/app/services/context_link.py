"""从会话上下文找回本轮省略的研究标的。

精简/即时研究不调用模型，只能按已加载资料回答。用户追问「它的市净率呢」
时，问题本身没有点名标的，这里按最近一轮提到的标的补齐，避免退回澄清。
只做确定性的字面匹配：不推断新标的，也不把不同领域的问题串在一起。
"""
from __future__ import annotations

import re
from typing import Any

from backend.app.models import FactRecord
from backend.app.research_routing import user_question

# 明确换话题的说法：出现这些词时不再沿用上一轮标的。
OTHER_SCOPE = re.compile(r"市场|大盘|指数|板块|宏观|行业|基金|ETF|可转债|转债|持仓|组合", re.I)


def _entity_index(facts: list[FactRecord]) -> dict[str, set[str]]:
    """代码与名称都映射到资料里的标准实体名，长键优先匹配。"""

    index: dict[str, set[str]] = {}
    for fact in facts:
        entity = str(getattr(fact, "entity", "") or "").strip()
        if not entity:
            continue
        index.setdefault(entity.casefold(), set()).add(entity)
        code = str(getattr(fact, "entity_code", "") or "").strip()
        if code:
            index.setdefault(code.casefold(), set()).add(entity)
            index.setdefault(code.split(".")[0].casefold(), set()).add(entity)
    return index


def _entities_in(content: str, index: dict[str, set[str]]) -> set[str]:
    text = content.casefold()
    found, spans = set(), []
    for key in sorted(index, key=len, reverse=True):
        pattern = r"(?<![0-9a-z_.])"+re.escape(key)+r"(?![0-9a-z_.])" if re.fullmatch(r"\d{5,6}(?:\.[a-z]{2})?",key) else re.escape(key)
        for match in re.finditer(pattern, text):
            if not any(start <= match.start() and match.end() <= end for start,end in spans):
                found.update(index[key])
                spans.append(match.span())
    return found


def _omitted_subject(question: str) -> bool:
    # 只继承代词或省略主语的指标追问；未知的新证券名称/代码不能被旧对象替换。
    if re.search(r"(?<!\d)\d{5,6}(?:\.[A-Za-z]{2})?(?!\d)", question):
        return False
    return bool(re.match(
        r"^(?:那|那么|再看|继续看|还有|另外)?(?:它|其|这只|该|这家|这个|上面|刚才|"
        r"市盈率|市净率|ROE|PE\b|PB\b|管理费|托管费|销售服务费|费率|跟踪误差|跟踪指数|"
        r"净值|价格|股价|涨跌幅|转股溢价|纯债溢价|到期收益率|营收|利润|现金流|风险|业绩|盈利)",question,re.I))


def _other_scope(question: str, direction: str) -> bool:
    remaining = question
    if direction == "基金筛选":
        remaining = re.sub(r"跟踪指数|基金|ETF", "", remaining, flags=re.I)
    elif direction == "可转债分析":
        remaining = re.sub(r"可转债|转债", "", remaining)
    elif direction == "行业分析":
        remaining = re.sub(r"行业|板块", "", remaining)
    return bool(OTHER_SCOPE.search(remaining))


def carry_context_entity(request: Any, facts: list[FactRecord]) -> str | None:
    """返回上一轮提到的标的；本轮已点名其他领域或没有上下文时返回 None。"""

    query = user_question(str(getattr(request, "query", "") or ""))
    direction = getattr(request,"research_direction","") or ""
    if _other_scope(query,direction) or not _omitted_subject(query):
        return None
    index = _entity_index(facts)
    if not index:
        return None
    turns = list(getattr(request, "context_messages", ()) or ())
    current = query.strip()
    for turn in reversed(turns):
        if getattr(turn, "role", "") != "user":
            continue
        raw_content = str(getattr(turn, "content", "") or "").strip()
        if any(raw_content.startswith(label+"：") for label in ("市场解读","行业分析","个股研究","基金筛选","可转债分析") if label != direction):
            return None
        content = user_question(raw_content)
        if not content or content == current:
            continue
        found = _entities_in(content, index)
        if found:
            return next(iter(found)) if len(found) == 1 else None
        if _other_scope(content,direction) or not _omitted_subject(content):
            return None
    target = str(getattr(request, "research_target", "") or "").strip().casefold()
    if target:
        found = index.get(target) or index.get(target.split(".")[0]) or set()
        return next(iter(found)) if len(found) == 1 else None
    return None
