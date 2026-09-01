# 问策智投 MV

当前已实现可运行的 MVP 闭环：识别用户意图、生成/确认画像、生成可执行 Task DAG、并行调度宏观/行业/个股/基金/组合五类规则型智能体、事实核验，并以合规审核作为最终闸门。

所有专业智能体只使用请求中显式传入的 `FactRecord` 快照；不会访问实时行情、不会自动下单，也不会把缺失数据补造成事实。

## 启动 API

```powershell
python -m pip install -r requirements.txt
python -m uvicorn backend.app.main:app --reload --port 8000
```

启动后访问 `http://127.0.0.1:8000/api/v1/health`，预期返回 `{"status":"ok"}`。
接口文档位于 `http://127.0.0.1:8000/docs`。

除组合诊断外，MVP 还提供两个画像接口：

- `POST /api/v1/profile/assess`：问卷/文本生成**未确认**画像草稿，并返回提取证据和缺失字段。
- `POST /api/v1/profile/confirm`：显式确认画像并递增版本号；只有确认后的画像能进入个性化诊断。

`POST /api/v1/portfolio/analyze` 的 `facts` 中每项必须包含带时区的 `snapshot_time`（如 `2026-08-29T08:00:00Z`）、`source_id` 与 `quality`。超过核验时效、来源为空或质量过低的事实会被自动移出最终证据并触发 `REVIEW`。

## 运行测试

```powershell
python -m pytest -q
```

主协调智能体位于 `backend/app/agents/coordinator.py`，五类专业智能体位于 `backend/app/agents/rule_agents.py`。它们均为可测试的确定性实现；后续替换为真实数据源或 LLM 时，仍必须保留相同 Pydantic 协议、事实引用与合规闸门。

## 启动 Streamlit 前端

先在一个终端启动 API，再打开第二个终端并执行：

```powershell
python -m streamlit run frontend/streamlit_app.py
```

浏览器访问终端显示的地址（通常是 `http://localhost:8501`）。前端提供首页/对话、画像中心、市场驾驶舱、标的研究、组合诊断、协作过程和证据中心七个页面；所有页面仅调用本地 FastAPI，不会自行生成市场事实或自动下单。
