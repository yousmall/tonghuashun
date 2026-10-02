"""技术验收展示页，使用本地合成报告；与正式投资入口分开。"""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import streamlit as st
from frontend.result_views import render_advice

st.set_page_config(page_title="问策智投 · 功能验收", layout="wide")
st.title("问策智投 · 功能验收演示")
st.warning("本页全部行情、语义判断与分析示例均为合成测试数据，只展示功能和安全边界。真实模型、外部数据与生产性能须单独验证。")

@st.cache_data(ttl=10, max_entries=1, show_spinner=False)
def read_report():
    return json.loads((ROOT / "deliverables/requirements_acceptance.json").read_text(encoding="utf-8"))

if not (ROOT / "deliverables/requirements_acceptance.json").is_file():
    st.info("请先运行 scripts/verify_requirements.py 生成本机验收报告。")
    st.stop()

report = read_report()
st.caption("报告生成时间：" + report["generated_at"])
load = report["load"]
with st.container(horizontal=True):
    st.metric("隔离测试账号", load["users"])
    st.metric("HTTP 成功数", load["http_200_count"])
    st.metric("P95 完整响应", f"{load['p95_ms'] / 1000:.3f} 秒")
    st.metric("最慢完整响应", f"{load['max_ms'] / 1000:.3f} 秒")
st.caption("测试包含真实 Bearer 校验、100 个登录租约、服务端画像、并行规则分析和 SQLite 历史保存；不包含公网、真实模型和生产 MySQL。")
st.write("历史隔离验证：" + ("通过" if load["history_isolation_passed"] else "未通过"))
name = st.selectbox("选择功能场景", [sample["name"] for sample in report["samples"]])
sample = next(sample for sample in report["samples"] if sample["name"] == name)
render_advice(sample["advice"], export_key="acceptance", question="合成测试问题：" + name)
with st.expander("验收边界与可分享报告"):
    for limitation in report["limitations"]:
        st.write(limitation)
    st.download_button("下载完整验收证据 JSON", json.dumps(report, ensure_ascii=False, indent=2),
                       file_name="requirements_acceptance.json", mime="application/json")
