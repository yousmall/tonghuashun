"""风险评估页的 Excel 股票交易画像入口。"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Callable

import streamlit as st

from backend.app.services.trade_history import FIELD_LABELS, REQUIRED


def _clear_file_analysis() -> None:
    for key in list(st.session_state):
        if key.startswith("trade_profile_") and key != "trade_profile_upload":
            st.session_state.pop(key, None)


def render_trade_analysis(analysis: dict) -> None:
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
    st.bar_chart([{"月份": r["month"], "买入笔数": r["buy_count"], "卖出笔数": r["sell_count"]}
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


def render_trade_profile(api_base: str, *, api_request: Callable) -> None:
    with st.container(border=True, key="trade-profile-panel"):
        st.subheader("从 Excel 分析用户画像")
        st.caption("上传同一用户近一年的股票成交明细（.xlsx / .xls，最多 5 MB）。"
                   "原始文件仅用于本次解析，确认后保存交易统计和行为摘要。")
        notice = st.session_state.pop("trade_profile_notice", None)
        if notice:
            st.success(notice)
        saved = st.session_state.profile.get("trading_analysis")
        if saved:
            with st.expander("已保存的交易行为画像"):
                render_trade_analysis(saved)
        uploaded = st.file_uploader("选择股票交易记录 Excel", type=["xlsx", "xls"], max_upload_size=5,
                                    key="trade_profile_upload", on_change=_clear_file_analysis)
        st.caption("必填字段：成交日期、证券代码、买卖方向、成交数量、成交价格。支持券商常见列名；读取后可手动匹配。")
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
            with st.spinner("正在识别交易工作表…"):
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
        end = st.date_input("近一年分析截止日", value=today, max_value=today, key="trade_profile_as_of")
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
