# 问策智投

当前已实现可运行的投研辅助闭环：识别用户意图、生成并确认多维画像、生成可执行 Task DAG、并行调度宏观/行业/个股/基金/组合五类智能体、跨来源与跨智能体一致性检查、事实核验，并以合规审核作为最终闸门。

系统支持两种运行模式：

- `rule_only`：无需外部密钥，使用确定性规则和本地/手工事实快照。
- `hybrid_llm`：配置第三方 OpenAI-compatible 模型后，规则引擎负责可重算基线，大模型负责主题研判与解释。模型只能引用显式授权的 `FactRecord`，越权引用或结构化校验失败会自动回退。

无论哪种模式，系统都不会自动下单，也不会把缺失数据补造成事实。

`POST /api/v1/portfolio/analyze` 默认开启自动取数闭环：意图识别后并行调用最小必要的问财只读能力，将响应标准化为事实，按公开规则生成带输入血缘的派生评分，再进入事实核验、专业智能体、交叉验证和合规闸门。响应中的 `data_acquisition` 明确区分 `live`、`mixed`、`unavailable` 等状态，`facts` 返回本次完整证据包。调用方可显式传 `"auto_fetch": false` 关闭自动取数。

## 启动 API

```powershell
python -m pip install -r requirements.txt
python -m uvicorn backend.app.main:app --reload --port 8000
```

启动后访问 `http://127.0.0.1:8000/api/v1/health`，预期返回 `{"status":"ok"}`。
接口文档位于 `http://127.0.0.1:8000/docs`。

就绪状态和运行指标：

- `GET /api/v1/readiness`：显示当前是规则模式还是混合大模型模式，以及问财数据源是否已配置。
- `GET /api/v1/metrics`：显示单进程请求量、失败数、观测成功率和 P50/P95/P99 延迟。该接口用于运行观测，不冒充生产 SLA。

除组合诊断外，系统还提供两个画像接口：

- `POST /api/v1/profile/assess`：问卷/文本生成**未确认**画像草稿，并返回提取证据和缺失字段。
- `POST /api/v1/profile/confirm`：显式确认画像并递增版本号；只有确认后的画像能进入个性化诊断。

`POST /api/v1/portfolio/analyze` 的 `facts` 中每项必须包含带时区的 `snapshot_time`（如 `2026-08-29T08:00:00Z`）、`source_id` 与 `quality`。超过核验时效、来源为空或质量过低的事实会被自动移出最终证据并触发 `REVIEW`。

时效按数据类型区分：实时行情 60 秒、新闻/公告/研报 5 分钟、标准化投研评分 15 分钟、持仓快照 1 天、慢速财务指标 90 天。跨来源同一实体/字段/报告期取值冲突，以及多个专业智能体评分显著分散，都会进入 `cross_validation` 并触发人工复核。

## 接入问财 SkillHub/OpenAPI

通过环境变量提供只读密钥，严禁写入仓库：

```powershell
$env:IWENCAI_API_KEY="你的只读 API Key"
$env:IWENCAI_BASE_URL="https://openapi.iwencai.com"
```

配置后，统一分析接口会按市场、行业、个股、基金、可转债或组合意图自动取数；也可调用 `POST /api/v1/data/fetch` 手工拉取 16 类能力，包括行情、财务、事件、宏观、机构调研、新闻、公告、研报、筛选、基金/ETF、行业和可转债。适配器包含并发限制、指数退避重试、短时熔断、来源标识和字段标准化。

## 接入第三方大模型

任何提供 OpenAI-compatible Chat Completions 接口的第三方模型均可接入：

```powershell
$env:WENCE_LLM_BASE_URL="https://你的模型服务/v1"
$env:WENCE_LLM_API_KEY="你的模型密钥"
$env:WENCE_LLM_MODEL="模型名称"
```

模型输入包含最近 20 条显式会话上下文、已确认画像、授权事实和规则基线；输出必须通过 `AgentResult` 校验，引用来源由事实层重新生成。

## 运行测试

```powershell
python -m pytest -q
```

主协调智能体位于 `backend/app/agents/coordinator.py`，五类规则智能体位于 `backend/app/agents/rule_agents.py`，第三方模型混合层位于 `backend/app/agents/llm_agents.py`，问财数据适配器位于 `backend/app/data_provider/iwencai.py`。

测试包含自动取数路由、派生血缘、过期事实保护和 API 闭环回传。100 并发测试使用进程内 ASGI 通路验证，不等同于包含公网、第三方模型、真实数据源和多实例部署的生产压测；正式提交前仍应在目标部署环境执行持续压测和可用性观测。

## 启动 Streamlit 前端

先在一个终端启动 API，再打开第二个终端并执行：

```powershell
python -m streamlit run frontend/streamlit_app.py
```

浏览器访问终端显示的地址（通常是 `http://localhost:8501`）。前端提供多轮对话、画像中心、市场驾驶舱、标的研究、组合诊断、协作过程和证据中心七个页面，支持画像维度图、专业评分对比、资产类别目标区间、DAG、证据和一致性问题展示。

## 提交材料

正式交付物统一放在 `deliverables/`：项目简介 PPT、项目技术说明书、产品使用说明、系统测试报告、项目分工开发过程与训练记录，以及演示视频。团队成员真实姓名和实际职责必须在提交前由团队核对，项目不会凭空生成个人贡献记录。
