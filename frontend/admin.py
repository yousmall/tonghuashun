"""管理员独立后台：用户、咨询和问财调用统计。"""

from typing import Any, Callable
from urllib.parse import quote

import pandas as pd
import streamlit as st

PERIODS = {"全部时间": 0, "最近 7 天": 7, "最近 30 天": 30, "最近 90 天": 90}
SKILL_LABELS = {
    "hithink-market-query": "市场与股票查询",
    "hithink-fund-query": "基金查询",
    "announcement-search": "公告搜索",
    "news-search": "新闻搜索",
    "report-search": "研报搜索",
}
ERROR_LABELS = {
    "HTTPStatusError": "服务返回错误", "ConnectError": "连接失败",
    "ReadTimeout": "读取超时", "ConnectTimeout": "连接超时",
    "PoolTimeout": "连接池超时", "ReadError": "读取失败",
    "ValueError": "数据格式异常", "JSONDecodeError": "数据格式异常",
    "TypeError": "数据格式异常", "CancelledError": "请求取消",
}


def _metrics(values: dict[str, Any]) -> None:
    with st.container(horizontal=True):
        for label, value in values.items():
            st.metric(label, value, border=True)


def _reset_users_page() -> None:
    st.session_state.admin_users_offset = 0


def render_admin(api_base: str, request: Callable, reset: Callable) -> None:
    with st.sidebar:
        st.title("管理后台")
        st.caption(f"管理员 · {st.session_state.auth_user['username']}")
        page = st.radio("管理功能", ["注册用户", "咨询统计", "问财调用"], key="admin_page")
        period = st.selectbox("统计时间", list(PERIODS), key="admin_period", on_change=_reset_users_page)
        st.button("刷新数据", icon=":material/refresh:", width="stretch")
        if st.button("退出登录", icon=":material/logout:", width="stretch"):
            request(api_base, "POST", "/auth/logout")
            reset()
            st.rerun()
    days = PERIODS[period]
    st.title(page)
    st.caption(f"{period} · 页面打开或点击刷新时更新")
    if page == "注册用户":
        _users(api_base, request, days)
    elif page == "咨询统计":
        _consultations(api_base, request, days)
    else:
        _iwencai(api_base, request, days)


def _users(base: str, request: Callable, days: int) -> None:
    search = st.text_input("搜索账号", key="admin_search", on_change=_reset_users_page)
    offset = int(st.session_state.get("admin_users_offset", 0))
    result = request(base, "GET", f"/admin/users?days={days}&offset={offset}&limit=50&q={quote(search)}")
    stats = request(base, "GET", f"/admin/statistics?days={days}")
    if not isinstance(result, dict) or not isinstance(stats, dict):
        return
    _metrics({"注册账号" if not search else "匹配账号": result["total"],
              "当前在线账号": stats["online_users"], "咨询次数": stats["consultations"]})
    st.caption(f"在线账号排在前面；退出或空闲超过 {stats['online_idle_seconds'] / 60:g} 分钟后转为离线。多处登录按一个账号统计。")
    rows = result["items"]
    if rows:
        st.dataframe(pd.DataFrame([{
            "账号": row["username"], "状态": "在线" if row["online"] else "离线",
            "角色": "管理员" if row["role"] == "admin" else "普通用户",
            "有效会话": row["online_sessions"], "咨询次数": row["consultations"],
            "咨询会话": row["conversations"], "注册时间（北京）": _time(row["created_at"]),
            "最近咨询（北京）": _time(row["last_consultation"]),
        } for row in rows]), hide_index=True, width="stretch")
    else:
        st.info("没有匹配的账号。")
    with st.container(horizontal=True):
        if st.button("上一页", disabled=offset == 0, key="admin_previous"):
            st.session_state.admin_users_offset = max(0, offset - 50)
            st.rerun()
        st.caption(f"第 {offset // 50 + 1} 页 · 每页最多 50 个账号")
        if st.button("下一页", disabled=offset + 50 >= result["total"], key="admin_next"):
            st.session_state.admin_users_offset = offset + 50
            st.rerun()
    if rows:
        username = st.selectbox("查看用户咨询情况", [row["username"] for row in rows], index=None,
                                placeholder="选择本页的一个账号", key="admin_selected_user")
        if username:
            detail = request(base, "GET", f"/admin/users/{quote(username)}/consultations?days={days}")
            if isinstance(detail, dict):
                st.subheader(f"{username} 的咨询情况")
                _metrics({"咨询次数": detail["consultations"]})
                _domain_chart(detail)
                if detail["recent"]:
                    labels = {item["domain"]: item["label"] for item in detail["domains"]}
                    st.caption("最近 50 次已保存的咨询")
                    st.dataframe(pd.DataFrame([{"咨询时间（北京）": _time(row["created_at"]),
                                                "领域": labels.get(row["domain"], "其他 / 未分类"),
                                                "问题": row["query"]} for row in detail["recent"]]), hide_index=True)
                else:
                    st.info("所选时间内没有咨询记录。")


def _domain_chart(stats: dict) -> None:
    rows = [{"领域": item["label"], "咨询次数": item["count"]} for item in stats["domains"]]
    st.bar_chart(pd.DataFrame(rows), x="领域", y="咨询次数", horizontal=True)


def _consultations(base: str, request: Callable, days: int) -> None:
    stats = request(base, "GET", f"/admin/statistics?days={days}")
    if not isinstance(stats, dict):
        return
    _metrics({"咨询次数": stats["consultations"], "咨询用户": stats["consulting_users"]})
    st.caption("每轮已保存的用户提问计一次；历史回看不重复计数。旧记录缺少领域信息时归入其他 / 未分类。")
    _domain_chart(stats)
    if stats["daily"]:
        st.line_chart(pd.DataFrame([{"日期（UTC）": item["date"], "咨询次数": item["count"]}
                                   for item in stats["daily"]]), x="日期（UTC）", y="咨询次数")
    st.subheader("各领域咨询最多的 5 个标的或主题")
    st.caption("有明确研究对象时按标的统计；没有对象时按提问主题统计。次数相同时按名称排序。")
    for item in stats["domains"]:
        with st.container(border=True):
            st.markdown(f"**{item['label']}** · {item['count']} 次咨询")
            if item["top"]:
                st.dataframe(pd.DataFrame([{"排名": index, "标的 / 主题": row["topic"],
                                            "咨询次数": row["count"], "咨询用户": row["users"]}
                                           for index, row in enumerate(item["top"], 1)]), hide_index=True)
            else:
                st.caption("所选时间内暂无咨询。")


def _iwencai(base: str, request: Callable, days: int) -> None:
    stats = request(base, "GET", f"/admin/iwencai?days={days}")
    if not isinstance(stats, dict):
        return
    rate = f"{stats['successful'] / stats['total'] * 100:.1f}%" if stats["total"] else "—"
    _metrics({"实际请求": stats["total"], "成功请求": stats["successful"], "失败请求": stats["failed"],
              "成功率": rate, "重试请求": stats["retries"], "平均耗时": f"{stats['average_ms']:g} ms"})
    st.caption(f"成功含 {stats['empty']} 次空结果；返回 {stats['facts']} 条资料。每次真实 HTTP 尝试计一次，重试单独计数；缓存复用和熔断拦截不计为接口调用。")
    st.caption("调用日志从本功能启用后开始记录，不能还原此前未记录的请求。")
    if stats.get("tracking_errors_since_start"):
        st.warning(f"本次服务启动后有 {stats['tracking_errors_since_start']} 次日志写入失败，统计可能不完整。")
    if not stats["total"]:
        st.info("所选时间内暂无问财调用记录。")
        return
    st.subheader("接口调用明细")
    st.dataframe(pd.DataFrame([{
        "接口": SKILL_LABELS.get(row["skill_id"], "其他问财查询"), "请求次数": row["total"],
        "成功": row["successful"], "失败": row["failed"], "空结果": row["empty"],
        "重试": row["retries"], "平均耗时（毫秒）": row["average_ms"], "资料条数": row["facts"],
    } for row in stats["by_interface"]]), hide_index=True)
    if stats["daily"]:
        st.line_chart(pd.DataFrame([{"日期（UTC）": row["date"], "请求次数": row["count"], "失败次数": row["failed"]}
                                   for row in stats["daily"]]), x="日期（UTC）", y=["请求次数", "失败次数"])
    if stats["errors"]:
        st.subheader("失败原因")
        st.dataframe(pd.DataFrame([{"原因": ERROR_LABELS.get(row["error_type"], "请求失败"),
                                    "服务状态码": str(row["status_code"] or "未收到响应"),
                                    "次数": row["count"]} for row in stats["errors"]]), hide_index=True)


def _time(value: Any) -> str:
    if not value:
        return "—"
    return pd.Timestamp(value).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M")
