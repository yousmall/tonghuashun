"""三类数据质量缺陷的回归测试。

覆盖：
1. 问财一行里的 TTM / 静态 / 动态市盈率与普通 / 加权 ROE 必须落到不同字段，
   不能因为子串别名而合并成一个字段、凭空制造"同项记录冲突"；
2. 全功能数据集里的事实年龄必须落在 ``fact_taxonomy`` 的时效窗口内；
3. 新闻/公告/研报等天然多条的字段不能被当成"同一项记录"做取值比对。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from backend.app.agents.coordinator import cross_validate_results
from backend.app.agents.llm_agents import HybridInvestmentAgent, LLMConfig, OpenAICompatibleLLM
from backend.app.agents.rule_agents import make_rule_agents
from backend.app.data_provider.iwencai import (
    IwencaiSkillHubProvider,
    _canonical_field,
    _indicator_code,
    _record_scope,
)
from backend.app.fact_taxonomy import fact_is_current, fact_max_age_seconds
from backend.app.models import (
    AgentResult,
    FactRecord,
    OrchestrationRequest,
    TaskStatus,
    UserProfile,
)

ROOT = Path(__file__).resolve().parent.parent
DATASET_PATH = ROOT / "examples" / "all_features_dataset.json"

# 真实取数的原始返回行（取自问财 finance-query 的财务指标查询）。
FINANCIAL_ROW = {
    "股票代码": "600519.SH",
    "股票简称": "贵州茅台",
    "最新价": "1251.24",
    "最新涨跌幅": -0.204179,
    "最新市盈率ttm": 19.207608,
    "最新市净率": 6.225392,
    "净资产收益率[20260630]": 17.9543,
    "营业收入同比增长率[20260630]": 1.4699,
    "最新静态市盈率": 19.00086,
    "最新动态市盈率": 17.568079,
    "最新a股流通市值": "1564152100000.000",
    "加权净资产收益率[20260630]": 16.75,
    "营业收入[20260630]": 90703260964.48,
}


# --------------------------------------------------------------------------- #
# P1：同一行的不同口径不能被子串别名压成同一字段
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("最新市盈率ttm", "pe_ttm"),
        ("市盈率ttm", "pe_ttm"),
        ("市盈率", "pe_ttm"),
        ("最新静态市盈率", "pe_static"),
        ("静态市盈率(pe)", "pe_static"),
        ("最新动态市盈率", "pe_dynamic"),
        ("动态市盈率(预测)", "pe_dynamic"),
        ("净资产收益率[20260630]", "roe"),
        ("加权净资产收益率[20260630]", "roe_weighted"),
        ("净资产收益率(加权)", "roe_weighted"),
        ("市净率", "pb"),
        ("最新价", "close_price"),
        ("涨跌幅[20260923]", "change"),
        ("最新涨跌幅", "change"),
        # 涨跌额（元）与涨跌幅（%）是同一行的两个不同量，不能合并。
        ("涨跌_前复权[20260923]", "change_amount"),
        ("涨跌额", "change_amount"),
        ("营业收入同比增长率[20260630]", "revenue_growth"),
        ("营业收入[20260630]", "revenue"),
    ],
)
def test_metric_aliases_keep_variants_apart(raw: str, expected: str) -> None:
    assert _canonical_field(raw) == expected


def test_indicator_code_reads_weighted_roe_out_of_parentheses() -> None:
    assert _indicator_code("净资产收益率roe(加权,公布值)") == "roe_weighted"
    assert _indicator_code("净资产收益率roe") == "roe"


@pytest.mark.asyncio
async def test_financial_payload_normalizes_into_distinct_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"datas": [FINANCIAL_ROW], "row_count": 1})

    provider = IwencaiSkillHubProvider(
        "iw-secret", base_url="https://iwencai.example", max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    facts = await provider.get_financial_metrics("600519")
    await provider.aclose()

    by_field = {fact.field: fact.value for fact in facts}
    assert by_field["pe_ttm"] == 19.207608
    assert by_field["pe_static"] == 19.00086
    assert by_field["pe_dynamic"] == 17.568079
    assert by_field["roe"] == 17.9543
    assert by_field["roe_weighted"] == 16.75
    assert by_field["revenue_growth"] == 1.4699
    # 每个字段只应出现一次：重复出现即说明口径又被合并了。
    assert len(facts) == len({fact.field for fact in facts})


@pytest.mark.asyncio
async def test_quote_payload_keeps_change_and_change_amount_apart() -> None:
    """问财行情行同时给出涨跌幅(%)与涨跌_前复权(元)，两者不是同一个数。"""

    quote_row = {
        "股票代码": "600519.SH",
        "股票简称": "贵州茅台",
        "收盘价[20260923]": "1251.24",
        "涨跌幅[20260923]": -0.204179,
        "涨跌_前复权[20260923]": "-2.56",
        "成交量[20260923]": "3098122",
        "换手率[20260923]": 0.248,
        "成交额[20260923]": "3.89463078271E9",
        "振幅[20260923]": 1.643803,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"datas": [quote_row], "row_count": 1})

    provider = IwencaiSkillHubProvider(
        "iw-secret", base_url="https://iwencai.example", max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    facts = await provider.get_quote("600519")
    await provider.aclose()

    by_field = {fact.field: fact.value for fact in facts}
    assert by_field["change"] == -0.204179
    assert by_field["change_amount"] == "-2.56"

    result = AgentResult(
        agent_id="security", status=TaskStatus.COMPLETED, opinion="行情已核对",
        confidence=0.8, facts_used=[fact.fact_id for fact in facts],
    )
    checked = cross_validate_results([result], facts)
    assert checked.status.value == "PASS"
    assert checked.issues == []


def test_three_pe_variants_do_not_self_conflict() -> None:
    """一次真实取数不能再产生同字段互相矛盾的 3 条事实。"""

    timestamp = datetime.now(timezone.utc)
    facts = [
        FactRecord(
            fact_id=f"F-{field}", entity="600519", field=field, value=value,
            snapshot_time=timestamp, source_id="IWENCAI_SKILLHUB", quality=0.9,
        )
        for field, value in (
            ("pe_ttm", 19.207608), ("pe_static", 19.00086), ("pe_dynamic", 17.568079),
            ("roe", 17.9543), ("roe_weighted", 16.75),
        )
    ]
    result = AgentResult(
        agent_id="security", status=TaskStatus.COMPLETED, opinion="估值已核对",
        confidence=0.8, facts_used=[fact.fact_id for fact in facts],
    )
    checked = cross_validate_results([result], facts)
    assert checked.status.value == "PASS"
    assert checked.issues == []


# --------------------------------------------------------------------------- #
# P2：数据集事实年龄必须与 fact_taxonomy 的时效窗口一致
# --------------------------------------------------------------------------- #
def _load_dataset() -> dict:
    return json.loads(DATASET_PATH.read_text(encoding="utf-8"))


def _scenario_bodies(scenario: dict) -> list[dict]:
    call = scenario["call"]
    return [body for body in (call.get("json_sequence") or [call.get("json")]) if isinstance(body, dict)]


def _overridden_fact_ids(dataset: dict) -> set[str]:
    """场景会用 overrides 故意把事实改成过期或低质量，比对时要排除。

    只排除"改年龄/改质量"这两种刻意构造的越界；``remove_fact_ids`` /
    ``replace_pack_items`` 只是某个场景不使用该事实，同一条事实仍被其它场景
    使用，因此它依然必须落在时效窗口内。
    """

    ids: set[str] = set()
    for scenario in dataset["scenarios"]:
        for body in _scenario_bodies(scenario):
            overrides = body.get("overrides") or {}
            ids |= set(overrides.get("set_age_minutes", {}))
            ids |= set(overrides.get("set_quality", {}))
    return ids


def _facts_from_packs(dataset: dict, *, now: datetime) -> list[tuple[str, dict, FactRecord]]:
    built: list[tuple[str, dict, FactRecord]] = []
    for pack, entries in dataset["fact_packs"].items():
        for entry in entries:
            built.append((
                pack,
                entry,
                FactRecord(
                    fact_id=entry["fact_id"],
                    entity=entry["entity"],
                    field=entry["field"],
                    value=entry["value"],
                    period=entry.get("period"),
                    snapshot_time=now - timedelta(minutes=float(entry.get("snapshot_age_minutes", 0))),
                    source_id=entry["source_id"],
                    quality=float(entry["quality"]),
                ),
            ))
    return built


def test_dataset_facts_stay_inside_their_freshness_windows() -> None:
    dataset = _load_dataset()
    overridden = _overridden_fact_ids(dataset)
    now = datetime.now(timezone.utc)

    expired = [
        (pack, entry["fact_id"], entry["field"],
         float(entry.get("snapshot_age_minutes", 0)), fact_max_age_seconds(fact) / 60)
        for pack, entry, fact in _facts_from_packs(dataset, now=now)
        if entry["fact_id"] not in overridden and not fact_is_current(fact, now)
    ]
    assert expired == [], f"数据集事实年龄越界（包/事实/字段/年龄分钟/窗口分钟）：{expired}"


def test_dataset_score_facts_are_inside_the_score_window() -> None:
    """评分类事实曾被设成 20–25 分钟，全部被 15 分钟窗口剔除。"""

    dataset = _load_dataset()
    overridden = _overridden_fact_ids(dataset)
    now = datetime.now(timezone.utc)

    offenders = [
        (pack, entry["fact_id"], entry["field"], entry.get("snapshot_age_minutes"))
        for pack, entry, fact in _facts_from_packs(dataset, now=now)
        if entry["fact_id"] not in overridden
        and entry["field"] in {"growth_score", "inflation_score", "liquidity_score",
                               "policy_score", "risk_appetite_score", "prosperity_score",
                               "valuation_score", "capital_flow_score", "crowding_score",
                               "fundamental_score", "technical_score", "event_score",
                               "governance_score", "fund_score"}
        and not fact_is_current(fact, now)
    ]
    assert offenders == [], f"这些评分事实一进核验就会被剔除：{offenders}"


# --------------------------------------------------------------------------- #
# P3：天然多条的字段不做"同一项记录"比对
# --------------------------------------------------------------------------- #
def test_dataset_list_valued_assertions_are_indexed() -> None:
    """``matched_rules`` 是列表：断言写成整串比较会永远失败（场景 28 曾经如此）。"""

    dataset = _load_dataset()
    offenders = [
        (scenario["id"], path)
        for scenario in dataset["scenarios"]
        for path in (scenario["expect"].get("assert") or {})
        if path.endswith(".matched_rules")
    ]
    assert offenders == [], f"这些断言缺少列表下标：{offenders}"


def test_record_scope_distinguishes_same_day_records() -> None:
    first = _record_scope({"publish_date": "2026-07-28", "title": "半年度经营数据"})
    second = _record_scope({"publish_date": "2026-07-28", "title": "原材料价格波动提示"})
    assert first and second and first != second
    # 没有标题时退回正文，同样不能把同日两条记录并成一条。
    third = _record_scope({"publish_date": "2026-07-28", "content": "甲"})
    fourth = _record_scope({"publish_date": "2026-07-28", "content": "乙"})
    assert third and fourth and third != fourth
    # 只有日期、没有标题或正文时区分不了两条记录，此时不能返回一个看似唯一的标识。
    assert _record_scope({"publish_date": "2026-07-28"}) == ""


@pytest.mark.asyncio
async def test_same_day_news_rows_are_two_records_not_a_conflict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [
            {"证券简称": "600519", "新闻标题": "公司披露半年度经营数据",
             "content": "营业收入同比增长 14%。", "publish_date": "2026-07-28"},
            {"证券简称": "600519", "新闻标题": "公司提示原材料价格波动",
             "content": "毛利率可能承压。", "publish_date": "2026-07-28"},
        ]})

    provider = IwencaiSkillHubProvider(
        "iw-secret", base_url="https://iwencai.example", max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    facts = await provider.get_news("600519")
    await provider.aclose()

    news = [fact for fact in facts if fact.field == "news"]
    # 每行产生"标题 + 正文"两条 news 事实；两条新闻 = 4 条事实、2 个逐条标识。
    assert len(news) == 4
    # 两条同日新闻各自成组：period 是逐条标识，不是发布日期。
    assert len({fact.period for fact in news}) == 2

    result = AgentResult(
        agent_id="security", status=TaskStatus.COMPLETED, opinion="消息面已核对",
        confidence=0.8, facts_used=[fact.fact_id for fact in facts],
    )
    checked = cross_validate_results([result], facts)
    assert checked.status.value == "PASS"
    assert checked.issues == []


@pytest.mark.asyncio
async def test_institutional_research_rows_are_individual_records() -> None:
    """机构调研按"每家机构一行"返回：行级字段不能互相判为同项冲突。"""

    rows = [
        {"股票代码": "600519.SH", "股票简称": "贵州茅台", "最新价": "1251.24",
         "最新涨跌幅": -0.204179, "研究员": "朱梦兰", "原始评级": "买入",
         "公告日期": "20260827", "研报": "2026年中报点评：市场化程度持续提升",
         "研究机构": "长江证券", "调整方向": "维持", "上次原始评级": "买入",
         "研报链接": "https://news.10jqka.com.cn/m59524979_sr/",
         "预测净利润中值[20261231]": 83869000000.0},
        {"股票代码": "600519.SH", "股票简称": "贵州茅台", "最新价": "1251.24",
         "最新涨跌幅": -0.204179, "研究员": "张潇倩", "原始评级": "买入",
         "目标价": 1647.05, "公告日期": "20260824",
         "研报": "更新报告：市场化改革顺利推进", "研究机构": "浙商证券",
         "调整方向": "维持", "上次原始评级": "买入",
         "研报链接": "https://news.10jqka.com.cn/m59429079_sr/",
         "预测净利润中值[20261231]": 83869000000.0},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"datas": rows, "row_count": 2})

    provider = IwencaiSkillHubProvider(
        "iw-secret", base_url="https://iwencai.example", max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    facts = await provider.get_institutional_research("600519")
    await provider.aclose()

    for field in ("研究员", "研究机构", "研报"):
        records = [fact for fact in facts if fact.field == field]
        assert len(records) == 2, field
        assert len({fact.period for fact in records}) == 2, field
    # 原文地址属于行级溯源元数据，不再作为会参与数值冲突判断的事实。
    assert not any(fact.field == "研报链接" for fact in facts)
    assert {fact.source_url for fact in facts} == {
        "https://news.10jqka.com.cn/m59524979_sr/",
        "https://news.10jqka.com.cn/m59429079_sr/",
    }

    result = AgentResult(
        agent_id="security", status=TaskStatus.COMPLETED, opinion="机构观点已汇总",
        confidence=0.8, facts_used=[fact.fact_id for fact in facts],
    )
    checked = cross_validate_results([result], facts)
    assert checked.status.value == "PASS"
    assert checked.issues == []


def test_dataset_news_pair_from_one_source_is_not_a_conflict() -> None:
    """数据集里同日同源的两条新闻（FNEWS-600519-1/2）不能触发同项冲突。"""

    dataset = _load_dataset()
    now = datetime.now(timezone.utc)
    news_facts = [
        fact for pack, entry, fact in _facts_from_packs(dataset, now=now)
        if pack == "security_600519" and entry["field"] == "news"
    ]
    assert len(news_facts) == 2
    assert len({fact.source_id for fact in news_facts}) == 1
    assert len({fact.period for fact in news_facts}) == 1

    result = AgentResult(
        agent_id="security", status=TaskStatus.COMPLETED, opinion="消息面已核对",
        confidence=0.8, facts_used=[fact.fact_id for fact in news_facts],
    )
    checked = cross_validate_results([result], news_facts)
    assert checked.status.value == "PASS"
    assert checked.issues == []


def test_multi_institution_ratings_are_not_one_item() -> None:
    """同一实体可以同时有"增持"和"减持"：那是两家机构的行，不是同项冲突。"""

    timestamp = datetime.now(timezone.utc)
    facts = [
        FactRecord(
            fact_id=f"F-{institution}", entity="600519", field="rating", value=rating,
            period="2026-07-29", snapshot_time=timestamp,
            source_id="IWENCAI_SKILLHUB", quality=0.9,
        )
        for institution, rating in (("券商甲", "增持"), ("券商乙", "减持"))
    ]
    result = AgentResult(
        agent_id="security", status=TaskStatus.COMPLETED, opinion="机构观点已汇总",
        confidence=0.8, facts_used=[fact.fact_id for fact in facts],
    )
    checked = cross_validate_results([result], facts)
    assert checked.status.value == "PASS"
    assert checked.issues == []


def test_single_valued_fields_still_report_same_item_conflicts() -> None:
    """多值豁免不能顺手放过真正的同项冲突。"""

    timestamp = datetime.now(timezone.utc)
    facts = [
        FactRecord(
            fact_id=f"F-{value}", entity="600519", field="pe_ttm", value=value,
            period="2026Q2", snapshot_time=timestamp + timedelta(seconds=index),
            source_id="IWENCAI_SKILLHUB", quality=0.9,
        )
        for index, value in enumerate((21.4, 18.9))
    ]
    result = AgentResult(
        agent_id="security", status=TaskStatus.COMPLETED, opinion="估值已核对",
        confidence=0.8, facts_used=[facts[0].fact_id],
    )
    checked = cross_validate_results([result], facts)
    assert checked.status.value == "REVIEW"
    assert "INTERNAL_VALUE_CONFLICT" in {issue.code for issue in checked.issues}


# --------------------------------------------------------------------------- #
# 混合智能体：模型不得抹掉规则基线的确定性分数
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_llm_null_score_keeps_rule_baseline_score() -> None:
    """模型返回 score=null 时，节点会退出共识与评分分散核验——必须沿用基线分。"""

    def llm_handler(request: httpx.Request) -> httpx.Response:
        content = {
            "status": "completed",
            "opinion": "宏观维度显示环境偏谨慎。",
            "score": None,
            "confidence": 0.7,
            "facts_used": [f"F-{field}" for field in (
                "growth_score", "inflation_score", "liquidity_score",
                "policy_score", "risk_appetite_score",
            )],
            "risk_flags": [],
            "invalidation_conditions": ["宏观数据更新"],
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]})

    llm = OpenAICompatibleLLM(
        LLMConfig(base_url="https://llm.example/v1", api_key="secret", model="third-party", max_retries=0),
        transport=httpx.MockTransport(llm_handler),
    )
    facts = [
        FactRecord(
            fact_id=f"F-{field}", entity="宏观数据", field=field, value=value,
            snapshot_time=datetime.now(timezone.utc), source_id="TEST", quality=0.9,
        )
        for field, value in (
            ("growth_score", 10), ("inflation_score", 20), ("liquidity_score", 30),
            ("policy_score", 40), ("risk_appetite_score", 50),
        )
    ]
    agent = HybridInvestmentAgent("market", make_rule_agents()["market"], llm)
    baseline = await make_rule_agents()["market"](OrchestrationRequest(
        query="分析市场", profile=UserProfile(user_id="u", confirmed=True), facts=facts))
    result = await agent.run(OrchestrationRequest(
        query="分析市场", profile=UserProfile(user_id="u", confirmed=True), facts=facts))

    assert result.details["engine"] == "third_party_llm"
    assert baseline.score is not None
    assert result.score == baseline.score
    assert any("基线" in reason for reason in result.confidence_reasons)
