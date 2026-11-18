"""风险评估页的 Excel 股票交易画像入口。"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from typing import Callable

import streamlit as st
from frontend.charts import render_bar

from backend.app.risk_questionnaire import assessment_is_current
from backend.app.services.trade_history import FIELD_LABELS, REQUIRED


def _clear_file_analysis() -> None:
    for key in list(st.session_state):
        if key.startswith("trade_profile_") and key != "trade_profile_upload":
            st.session_state.pop(key, None)


def render_trade_analysis(analysis: dict) -> None:
    if analysis.get("cost_review"):
        from frontend.trade_review import render_review
        with st.expander("查看扣费复盘"):
            render_review(analysis["cost_review"])
    if analysis.get("input_kind") == "analysis_report":
        render_report_analysis(analysis)
        return
    st.caption(f"分析窗口：{analysis['window_start']} 至 {analysis['window_end']}；"
               f"实际成交日期：{analysis['first_trade_date']} 至 {analysis['last_trade_date']}")
    with st.container(horizontal=True):
        st.metric("有效成交", f"{analysis['trade_count']} 笔", border=True)
        st.metric("交易股票", f"{analysis['security_count']} 只", border=True)
        days = analysis.get("average_matched_holding_days")
        st.metric("已匹配平均持有", f"{days:.1f} 天" if days is not None else "无法计算", border=True)
    for note in analysis.get("behavioral_notes", []):
        st.write(note)
    st.caption("交易行为仅作为画像补充；风险承受能力以确认的风险问卷为准。")
    render_bar([{"月份": r["month"], "买入笔数": r["buy_count"], "卖出笔数": r["sell_count"]}
                  for r in analysis["monthly"]], x="月份", y=["买入笔数", "卖出笔数"])
    with st.expander("交易统计与数据核验"):
        st.write(f"买入成交额：{analysis['buy_amount']:,.2f} 元；卖出成交额：{analysis['sell_amount']:,.2f} 元。")
        pnl = analysis.get("matched_sell_gross_pnl")
        st.write(f"完全匹配卖出：{analysis['matched_sell_count']} 笔；"
                 f"未完整匹配：{analysis['unmatched_sell_count']} 笔。")
        st.write(f"已匹配卖出毛损益（未计费用）：{pnl:,.2f} 元。" if pnl is not None else "缺少完整买卖匹配，无法计算卖出毛损益。")
        st.dataframe([{"证券代码": r["code"], "证券名称": r["name"], "成交笔数": r["count"],
                       "买入成交额": round(r["buy_amount"], 2), "卖出成交额": round(r["sell_amount"], 2),
                       "双边交易额占比": f"{r['turnover_share']:.1%}"} for r in analysis["top_securities"]], hide_index=True)
        for item in analysis["limitations"]:
            st.write(item)
        if analysis.get("excluded_rows"):
            st.table([{"排除原因": k, "行数": v} for k, v in analysis["excluded_rows"].items()])
        if analysis.get("row_issues"):
            st.dataframe([{"Excel 行号": r["row"], "核验提示": r["reason"]} for r in analysis["row_issues"]], hide_index=True)
        if analysis.get("preview"):
            st.caption("有效交易预览（最多 20 行）")
            st.dataframe([{"行号": t["row"], "日期": t["date"], "证券代码": t["code"],
                           "方向": "买入" if t["side"] == "buy" else "卖出", "数量": t["quantity"],
                           "价格": t["price"]} for t in analysis["preview"]], hide_index=True)
        if analysis.get("matching_evidence"):
            st.caption("先进先出匹配依据（最多 50 条）")
            st.dataframe([{"证券代码": t["code"], "买入行": t["buy_row"], "卖出行": t["sell_row"],
                           "匹配股数": t["quantity"], "持有天数": t["days"]}
                          for t in analysis["matching_evidence"]], hide_index=True)
        source = analysis["source"]
        st.caption(f"来源：{source['file_name']} / {source['sheet_name']} / 表头第 {source['header_row']} 行；"
                   f"文件校验值：{source['sha256']}")


def render_report_analysis(analysis: dict) -> None:
    st.info("已识别为交易行为分析报告；以下指标来自报告，尚未用逐笔成交明细验证。")
    st.caption(f"报告期间：{analysis.get('window_start') or '未注明'} 至 {analysis.get('window_end') or '未注明'}")
    with st.container(horizontal=True):
        st.metric("报告成交笔数", f"{analysis['trade_count']:,} 笔", border=True)
        st.metric("报告涉及股票", f"{analysis['security_count']} 只", border=True)
        days = analysis.get("reported_median_holding_days")
        st.metric("报告持仓时间中位数", f"{days:g} 天" if days is not None else "未注明", border=True)
    for limitation in analysis["limitations"]:
        st.caption(limitation)
    if analysis.get("monthly"):
        render_bar([{"月份": row["month"], "成交笔数": row["trade_count"]} for row in analysis["monthly"]],
                     x="月份", y="成交笔数")
    with st.expander("报告指标与来源", expanded=True):
        st.dataframe([{"指标": row["label"], "报告数值": row["value"], "单位或口径": row["unit"],
                       "来源": f"{row['sheet']} 第 {row['row']} 行"} for row in analysis["report_metrics"]], hide_index=True)
    for section in analysis["report_sections"]:
        with st.expander(section["title"]):
            first, second, third = section["columns"]
            st.dataframe([{first: row["label"], second: row["value"], third: row["detail"],
                           "本表重算占比": f"{row['calculated_share']:.1%}" if row["calculated_share"] is not None else "无法计算",
                           "来源行": row["row"]} for row in section["rows"]], hide_index=True)
            st.caption(f"本表数值合计：{section['calculated_total']:,.2f}；来源：{section['sheet']}，表头第 {section['header_row']} 行。")
    with st.expander("原报告行为摘要与口径说明"):
        for note in analysis.get("behavioral_notes", []):
            st.write(note)
        for note in analysis.get("source_notes", []):
            st.write(note["text"])
            st.caption(f"来源：{note['sheet']} 第 {note['row']} 行")
        for check in analysis.get("reconciliation", []):
            st.write(f"{check['label']}：表内重算 {check['calculated']:g}，报告 {check['reported']:g}，" +
                     ("一致。" if check["matches"] else "不一致，请核对。"))
    source = analysis["source"]
    st.caption(f"来源：{source['file_name']}；文件校验值：{source['sha256']}")


def render_trade_portrait(profile: dict, *, draft: dict | None = None) -> None:
    analysis = draft or profile.get("trading_analysis")
    if not analysis:
        return

    report = analysis.get("input_kind") == "analysis_report"
    st.markdown("### 用户画像 · Excel 交易行为")
    period = " 至 ".join(str(day) for day in (analysis.get("window_start"), analysis.get("window_end")) if day)
    source = "汇总报告" if report else "成交明细"
    state = "待确认" if draft else "已保存"
    st.caption(" ｜ ".join(filter(None, (state, source, period))))

    days = analysis.get("reported_median_holding_days" if report else "average_matched_holding_days")
    trade_count = analysis["trade_count"]
    active_days = analysis.get("active_days")
    daily = trade_count / active_days if active_days else None
    if days is None:
        investor_type = "交易活跃型，持有周期待核对" if daily is not None and daily >= 5 else "持有周期待核对"
        holding = "可用持有时长数据不足。"
    else:
        investor_type = "短线交易型" if days <= 10 else "波段交易型" if days <= 30 else "较长周期持有型"
        basis = "报告持有时长中位数" if report else "可匹配成交的平均持有时长"
        holding = f"{basis}为 {days:g} 天。"
    if daily is not None and daily >= 5 and days is not None:
        investor_type += "，成交较频繁"
    st.markdown(f"**行为类型**：{investor_type}。{holding}")
    trading = f"成交 {trade_count:,} 笔，涉及 {analysis['security_count']:,} 只股票"
    if daily is not None:
        trading += f"，每个有成交日平均 {daily:.1f} 笔"
    st.markdown(f"**交易活跃度与覆盖**：{trading}。")

    top = analysis.get("top_securities") or []
    if top:
        st.markdown(f"**交易集中度**：成交额最高的单只股票占双边成交额 {top[0]['turnover_share']:.1%}。")
    if report:
        for section in analysis.get("report_sections", []):
            dimension, metric, _ = section["columns"]
            if dimension not in {"板块", "题材方向"} or not section["rows"] or "金额" not in metric:
                continue
            leading = max(section["rows"], key=lambda row: row["value"])
            share = leading.get("calculated_share")
            if share:
                st.markdown(f"**{dimension}偏好**：表内成交金额最大的类别占 {share:.1%}。")
                st.text(leading["label"])

    product_types = profile.get("preferred_product_types")
    if assessment_is_current(profile) and product_types:
        if (profile.get("financial_plan") or {}).get("equity_cap") == 0:
            focus = "根据已确认问卷的资金用途及回撤约束，先比较流动性和期限匹配的固定收益类产品。"
        else:
            levels = "、".join(profile.get("suitable_product_levels") or [])
            match = f"风险等级在 {levels} 范围内的" if levels else ""
            focus = f"按已确认问卷比较{match}{'、'.join(product_types)}，核对成本和赎回条件。"
    else:
        focus = "可先比较分散型宽基指数产品和低波动固定收益类产品的波动、费用及赎回条件。完成风险问卷后确认适用品种。"
    st.markdown(f"**建议关注的品种**：{focus}")
    if (days is not None and days <= 10) or (daily is not None and daily >= 5):
        st.markdown("**后续关注**：优先复盘交易频次与扣费，用扣费后结果检验短周期交易是否有效。")
    if report:
        st.caption("汇总指标来自上传报告；持有时长使用报告的中位数口径。")
        if any(not check["matches"] for check in analysis.get("reconciliation", [])):
            st.warning("报告部分合计存在差异，请查看下方数据核验后再使用结论。")
    st.caption("行为分型依据：持有时长不超过 10 天为短线，超过 10 天至 30 天为波段，超过 30 天为较长周期。风险等级和拟投资期限以问卷为准。")


def render_trade_profile(api_base: str, *, api_request: Callable) -> dict | None:
    with st.container(border=True, key="trade-profile-panel"):
        st.subheader("从 Excel 分析用户画像")
        st.caption("上传同一用户的股票成交明细或交易行为分析报告（.xlsx / .xls，最多 5 MB）。"
                   "原始文件仅用于本次解析，确认后保存交易统计和行为摘要。明细按近一年统计，汇总报告保留原报告期间。")
        notice = st.session_state.pop("trade_profile_notice", None)
        if notice:
            st.success(notice)
        saved = st.session_state.profile.get("trading_analysis")
        if saved:
            with st.expander("已保存的交易行为画像"):
                render_trade_analysis(saved)
        uploaded = st.file_uploader("选择股票交易记录 Excel", type=["xlsx", "xls"], max_upload_size=5,
                                    key="trade_profile_upload", on_change=_clear_file_analysis)
        st.caption("成交明细需包含日期、代码、买卖方向、数量、价格，可手动匹配列；分析报告自动识别多工作表中的关键指标、月度统计、板块题材偏好和成本。")
        if uploaded is None:
            return
        raw = uploaded.getvalue()
        if not raw or len(raw) > 5 * 1024 * 1024:
            st.error("请选择非空且不超过 5 MB 的 Excel 文件。")
            return
        digest = hashlib.sha256(raw).hexdigest()
        base = {"file_name": uploaded.name, "content_base64": base64.b64encode(raw).decode("ascii")}
        if st.button("读取 Excel", key="trade_profile_read"):
            st.session_state.pop("trade_profile_draft", None)
            st.session_state.pop("trade_profile_inspection", None)
            with st.spinner("正在识别成交明细或分析报告…"):
                result = api_request(api_base, "POST", "/profile/trades/inspect", base)
            if result:
                st.session_state.trade_profile_inspection = result
                st.session_state.trade_profile_file_digest = digest
                # 同一文件重新读取后，使工作表/表头控件回到识别结果。
                for key in list(st.session_state):
                    if key.startswith(("trade_profile_sheet_", "trade_profile_header_", "trade_profile_column_")):
                        st.session_state.pop(key, None)
        inspection = st.session_state.get("trade_profile_inspection")
        if not inspection or st.session_state.get("trade_profile_file_digest") != digest:
            return
        if inspection.get("input_kind") == "analysis_report":
            return _render_report_import(api_base, base, digest, inspection, api_request=api_request)
        sheet = st.selectbox("交易工作表", inspection["sheets"],
                             index=inspection["sheets"].index(inspection["sheet_name"]), key=f"trade_profile_sheet_{digest}")
        if sheet != inspection["sheet_name"]:
            st.session_state.pop("trade_profile_draft", None)
            result = api_request(api_base, "POST", "/profile/trades/inspect", {**base, "sheet_name": sheet})
            if not result:
                return
            inspection = result
            st.session_state.trade_profile_inspection = result
        header = st.number_input("表头所在行", min_value=1, max_value=30, value=inspection["header_row"],
                                 key=f"trade_profile_header_{digest}_{sheet}")
        if sheet != inspection["sheet_name"] or header != inspection["header_row"]:
            st.session_state.pop("trade_profile_draft", None)
            result = api_request(api_base, "POST", "/profile/trades/inspect",
                                 {**base, "sheet_name": sheet, "header_row": int(header)})
            if not result:
                return
            inspection = result
            st.session_state.trade_profile_inspection = result
        if not inspection["columns"]:
            st.warning("所选工作表为空，请选择交易明细工作表。")
            return
        mapping = {}
        with st.expander("核对交易列映射", expanded=not all(k in inspection["suggested_columns"] for k in REQUIRED)):
            for field in FIELD_LABELS:
                options = ["不使用此列", *inspection["columns"]]
                suggestion = inspection["suggested_columns"].get(field)
                value = st.selectbox(FIELD_LABELS[field] + ("（必填）" if field in REQUIRED else "（可选）"), options,
                                     index=options.index(suggestion) if suggestion in options else 0,
                                     key=f"trade_profile_column_{digest}_{sheet}_{header}_{field}")
                mapping[field] = "" if value == options[0] else value
            if inspection.get("preview"):
                st.caption("自动识别的原表预览")
                st.dataframe(inspection["preview"], hide_index=True)
        today = datetime.now(timezone(timedelta(hours=8))).date()
        file_last = inspection.get("last_trade_date")
        as_of_key = "trade_profile_as_of"
        if file_last and file_last < today.isoformat():
            st.caption(f"文件内最后成交日为 {file_last}；默认按今天回溯一年，更早的成交会被排除。"
                       "需要覆盖文件完整期间时，可把截止日设为该日期。")
            if st.button(f"按文件最后成交日（{file_last}）统计", key=f"trade_profile_use_last_{digest}"):
                st.session_state[as_of_key] = date.fromisoformat(file_last)
                st.rerun()
        end = st.date_input("近一年分析截止日", value=today, max_value=today, key=as_of_key)
        payload = {**base, "sheet_name": sheet, "header_row": int(header), "columns": mapping, "as_of": end.isoformat()}
        signature = hashlib.sha256(json.dumps({k: v for k, v in payload.items() if k != "content_base64"},
                                             sort_keys=True).encode()).hexdigest()
        if st.button("分析交易画像", key="trade_profile_analyze", type="primary", disabled=any(not mapping[k] for k in REQUIRED)):
            st.session_state.pop("trade_profile_draft", None)
            with st.spinner("正在分析近一年交易记录…"):
                analysis = api_request(api_base, "POST", "/profile/trades/analyze", payload)
            if analysis:
                st.session_state.trade_profile_draft = {"signature": signature, "analysis": analysis,
                                                       "version": int(st.session_state.profile.get("version") or 1)}
        return _render_analysis_draft(api_base, payload, signature, api_request=api_request)


def _render_report_import(api_base, base, digest, inspection, *, api_request):
    st.success("已识别多工作表交易行为分析报告，无需匹配成交明细列。")
    st.caption("将导入整份报告，保留原报告期间；确认前可核对指标、来源与数据差异。")
    signature = "report:" + digest
    draft = st.session_state.get("trade_profile_draft")
    if not draft or draft.get("signature") != signature:
        render_report_analysis(inspection["report_preview"])
    if st.button("分析交易画像", key="trade_profile_analyze", type="primary"):
        st.session_state.pop("trade_profile_draft", None)
        with st.spinner("正在提取报告指标并核对表内合计…"):
            analysis = api_request(api_base, "POST", "/profile/trades/analyze", base)
        if analysis:
            st.session_state.trade_profile_draft = {"signature": signature, "analysis": analysis,
                                                   "version": int(st.session_state.profile.get("version") or 1)}
    return _render_analysis_draft(api_base, base, signature, api_request=api_request)


def _render_analysis_draft(api_base, payload, signature, *, api_request):
    draft = st.session_state.get("trade_profile_draft")
    if not draft or draft["signature"] != signature:
        return
    render_trade_analysis(draft["analysis"])
    st.caption("请核对交易统计，确认后补充到当前账号画像。")
    has_risk_draft = bool(st.session_state.get("profile_draft"))
    if has_risk_draft:
        st.info("请先确认并保存上方的风险测评结果，再保存交易画像。")
    stale = int(st.session_state.profile.get("version") or 1) != draft["version"]
    if stale:
        st.info("用户画像已更新，请重新点击“分析交易画像”后再核对并保存。")
    if st.button("确认并保存交易画像", key="trade_profile_confirm", disabled=has_risk_draft or stale):
        with st.spinner("正在保存交易画像…"):
            profile = api_request(api_base, "POST", "/profile/trades/confirm",
                                  {**payload, "expected_version": draft["version"]})
        if profile:
            st.session_state.profile = profile
            st.session_state.pop("trade_profile_draft", None)
            # 清除后台画像缓存，防止恢复逻辑用旧版本覆盖刚保存的摘要。
            from frontend.account_prefetch import discard_account_result
            discard_account_result(api_base, "/profile")
            st.session_state.trade_profile_notice = "交易行为画像已保存。"
            st.rerun()
    return draft["analysis"] if not stale else None
