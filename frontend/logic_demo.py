"""投资逻辑链演示建议包。

平台里的真实回答若资料不足、被风险检查拦截，或尚未产生咨询，就没有通过核验的
证据引用，四层下钻只能渲染出一片空态。这份示例建议包沿用与生产完全一致的数据结构
（evidence / facts / agent_results / research_report），用来展示「逐级展开 + 数据溯源」
在有资料时究竟呈现什么，便于用户理解机制，也便于回归测试。

重要：所有数值、主体与结论均为演示，不构成投资建议；原文链接指向公开站点首页，
仅用于演示「可下钻到原文」这一交互。
"""
from __future__ import annotations

from typing import Any

TIME = "2026-09-30T10:00:00+08:00"
CNINFO = "https://www.cninfo.com.cn/"
STATS = "https://www.stats.gov.cn/"
QUOTE = "https://www.sse.com.cn/"

DEMO_NOTICE = ("演示数据 · 仅用于展示逐级展开与溯源机制，数值与结论均为示例，不构成投资建议；"
               "「打开原文」指向公开站点首页，不是该数值的原始公告。")

def demo_trace_markdown() -> str:
    """把演示包的溯源数据渲染成轻量 Markdown 表格。

    顶部面板在每个页面都会出现，用静态表格而非 st.dataframe，避免每次重跑都做
    一次 Arrow 序列化；下钻折叠里仍使用平台真实的 dataframe 组件。
    """

    from frontend.presentation import source_trace_rows

    columns = ("支持分析", "对象", "指标", "内容 / 数值", "数据时间（北京时间）", "来源", "报告期")
    lines = ["| " + " | ".join(columns) + " | 原文 |",
             "|" + "---|" * (len(columns) + 1)]
    for row in source_trace_rows(DEMO_ADVICE):
        cells = [str(row.get(name) or "—").replace("|", "\\|") for name in columns]
        url = row.get("原文")
        cells.append(f"[查看原文]({url})" if url else "—")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


DEMO_ADVICE: dict[str, Any] = {
    "conclusion": (
        "综合结论：示例科技估值与盈利增速基本匹配，处于合理区间；"
        "但行业需求波动、资金流向与机构持仓出现背离，仍需跟踪。以下每一条判断都可逐层回查到原始数据。"
    ),
    "compliance": {"status": "PASS"},
    "evidence": ["F1", "F2", "F3", "F4", "F5", "F6", "F7"],
    "facts": [
        {"fact_id": "F1", "entity": "示例科技", "field": "营业收入", "value": "128.6 亿元",
         "snapshot_time": TIME, "source_id": "IWENCAI:FINANCE", "source_url": CNINFO,
         "period": "2026Q3", "quality": .92},
        {"fact_id": "F2", "entity": "示例科技", "field": "归母净利润", "value": "14.2 亿元",
         "snapshot_time": TIME, "source_id": "IWENCAI:FINANCE", "source_url": CNINFO,
         "period": "2026Q3", "quality": .92},
        {"fact_id": "F3", "entity": "示例科技", "field": "市盈率(TTM)", "value": "22.4 倍",
         "snapshot_time": TIME, "source_id": "IWENCAI:VALUATION", "source_url": QUOTE,
         "period": "20260930", "quality": .9},
        {"fact_id": "F4", "entity": "电子设备制造业", "field": "制造业PMI", "value": "50.6",
         "snapshot_time": TIME, "source_id": "USER_SUPPLIED:国家统计局月度公布", "source_url": STATS,
         "period": "202609", "quality": .9},
        {"fact_id": "F5", "entity": "示例科技", "field": "主力资金净流入", "value": "-3.8 亿元",
         "snapshot_time": TIME, "source_id": "IWENCAI:FUND_FLOW", "source_url": QUOTE,
         "period": "20260930", "quality": .86},
        {"fact_id": "F6", "entity": "示例科技", "field": "营收同比增速", "value": "12.4%",
         "snapshot_time": TIME, "source_id": "DERIVED:CALC", "derived_from": ["F1"],
         "derivation_rule": "YOY_V1：本期营业收入 / 上年同期营业收入 - 1", "quality": .8},
        {"fact_id": "F7", "entity": "示例科技", "field": "机构持仓比例", "value": "18.6%",
         "snapshot_time": TIME, "source_id": "IWENCAI:HOLDING", "source_url": CNINFO,
         "period": "2026Q3", "quality": .88},
        {"fact_id": "REJECTED", "entity": "未通过核验的资料", "field": "市场传闻",
         "value": "不应出现在逻辑链里", "snapshot_time": TIME, "source_id": "DEMO_SNAPSHOT"},
    ],
    "agent_results": [
        {"agent_id": "market", "status": "completed", "confidence": .78,
         "opinion": "流动性整体中性偏松，但主力资金净流出显示短期分歧加大，指数层面以震荡看待。",
         "facts_used": ["F4", "F5", "REJECTED"],
         "confidence_reasons": ["PMI 与资金流水的时间都在近一个月内", "两项资料来源可回溯"],
         "risk_flags": ["资金流向与宏观景气背离", "单日资金流数据波动较大"],
         "invalidation_conditions": ["PMI 连续两月回落至荣枯线下", "主力资金持续净流出超过 5 个交易日"]},
        {"agent_id": "industry", "status": "completed", "confidence": .82,
         "opinion": "电子设备制造业景气温和回升，需求端仍存在季节性波动，需观察订单能否延续。",
         "facts_used": ["F4", "F1"],
         "confidence_reasons": ["行业指标与公司营收相互印证"],
         "risk_flags": ["需求季节性波动", "上游价格传导"],
         "invalidation_conditions": ["行业 PMI 回落", "下游订单增速转负"]},
        {"agent_id": "security", "status": "completed", "confidence": .74,
         "opinion": "22.4 倍市盈率与 12.4% 营收同比增速基本匹配，估值不便宜也未明显透支；"
                    "盈利质量仍需用经营现金流进一步验证。",
         "facts_used": ["F1", "F2", "F3", "F6"],
         "confidence_reasons": ["财报与估值数据同属 2026Q3 口径", "派生指标可回溯到原始营收"],
         "risk_flags": ["盈利下修", "尚未取得现金流数据"],
         "invalidation_conditions": ["净利润增速低于营收增速", "估值分位升至历史高位"]},
        {"agent_id": "portfolio", "status": "completed", "confidence": .69,
         "opinion": "机构持仓比例与短期资金流向背离，说明筹码结构存在分歧，暂不宜按单一趋势信号处理。",
         "facts_used": ["F5", "F7"],
         "confidence_reasons": ["两类持仓数据来源独立"],
         "risk_flags": ["持仓拥挤度上升"],
         "invalidation_conditions": ["机构持仓与资金流向重新同向"]},
    ],
    "research_report": {
        "title": "示例科技 · 研究证据对照", "status": "reviewed",
        "summary": "依据已通过核验的引用整理支持因素、不利因素与重新评估条件。",
        "method": "按分析维度归集已核验事实，不额外生成未经引用的观点。",
        "coverage": {"cited": 7, "available": 8, "dated": 7,
                     "dimensions": ["市场环境", "行业分析", "个股研究", "持仓分析"],
                     "missing_dimensions": []},
        "key_findings": [
            {"title": "估值与增速基本匹配", "detail": "PE 22.4 倍对应营收同比 12.4%，未见明显透支。",
             "stance": "favorable", "fact_ids": ["F3", "F6", "REJECTED"]},
            {"title": "营收增速由原始营收派生", "detail": "该指标可继续回溯到财报原始营收与计算规则。",
             "stance": "neutral", "fact_ids": ["F6", "F1"]},
            {"title": "资金流向与机构持仓背离", "detail": "短期资金净流出但机构持仓仍高，需重新评估筹码结构。",
             "stance": "caution", "fact_ids": ["F5", "F7"]},
        ],
        "sections": [
            {"entity": "示例科技", "title": "估值与盈利",
             "evidence": [
                 {"observation": "市盈率 22.4 倍", "interpretation": "与盈利增速基本匹配。",
                  "stance": "favorable", "fact_ids": ["F3"], "source": "IWENCAI:VALUATION",
                  "source_url": QUOTE},
                 {"observation": "营收 128.6 亿元", "interpretation": "提供派生指标的原始口径。",
                  "stance": "neutral", "fact_ids": ["F1"], "source": "IWENCAI:FINANCE",
                  "source_url": CNINFO},
             ],
             "limitations": ["未覆盖经营现金流"], "change_conditions": ["盈利下修"]},
            {"entity": "电子设备制造业", "title": "行业景气",
             "evidence": [
                 {"observation": "制造业 PMI 50.6", "interpretation": "位于扩张区间。",
                  "stance": "favorable", "fact_ids": ["F4"],
                  "source": "USER_SUPPLIED:国家统计局月度公布", "source_url": STATS},
             ],
             "limitations": ["未覆盖订单数据"], "change_conditions": ["PMI 回落至荣枯线下"]},
        ],
        "limitations": ["演示数据，未接入实时行情"],
    },
}
