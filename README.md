# 问策智投

当前已实现可运行的投研辅助闭环：识别用户意图、生成并确认多维画像、生成可执行 Task DAG、并行调度宏观/行业/个股/基金/组合五类智能体、跨来源与跨智能体一致性检查、事实核验，并以合规审核作为最终闸门。

系统同时提供账号注册/登录和 MySQL 对话持久化。密码使用带随机盐的 scrypt 哈希保存，前端通过有过期时间的 Bearer 令牌访问当前用户自己的历史记录；不同用户不能读取彼此的会话。应用级线程池默认提供 100 个工作线程和 100 个登录会话槽，登录成功后分配会话槽，连续 10 分钟没有用户操作会回收槽位并要求重新登录。

系统支持两种运行模式：

- `rule_only`：未配置模型时的降级模式。问卷计分、事实核验和独立规则组件仍可用；自然语言分析返回澄清，画像文本保留待补充字段，不再用关键词或正则猜测。
- `hybrid_llm`：大模型负责意图与请求风险识别、画像文本提取、专业研判、输出合规及意见矛盾复核。代码保留问卷公式、数值上限、事实引用与时效校验；模型不能覆盖这些硬约束。

无论哪种模式，系统都不会自动下单，也不会把缺失数据补造成事实。

`POST /api/v1/portfolio/analyze` 默认开启自动取数闭环：意图识别后并行调用最小必要的问财只读能力，将响应标准化为事实，按公开规则生成带输入血缘的派生评分，再进入事实核验、专业智能体、交叉验证和合规闸门。响应中的 `data_acquisition` 明确区分 `live`、`mixed`、`unavailable` 等状态，`facts` 返回本次完整证据包。调用方可显式传 `"auto_fetch": false` 关闭自动取数。

## 启动 API

使用 Python 3.11 或更高版本。

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

用户、自选与历史接口：

- `POST /api/v1/auth/register`：注册账号并返回登录令牌。
- `POST /api/v1/auth/login`：登录并返回登录令牌。
- `GET /api/v1/auth/me`：读取当前登录用户。
- `GET /api/v1/auth/session/status`：只检查租约状态，不刷新空闲计时。
- `POST /api/v1/auth/logout`：退出登录并立即释放会话槽。
- `POST /api/v1/auth/logout/beacon`：供本机 Streamlit 连接观察器在页面关闭后释放会话槽，只接受不能访问用户数据的随机回收凭据。
- `GET /api/v1/watchlist`：读取当前用户的自选研究列表。
- `POST /api/v1/watchlist`：添加股票、基金、行业或可转债到自选研究，单个账号最多 50 项。
- `DELETE /api/v1/watchlist/{item_id}`：删除当前用户的一项自选；不能访问其他用户的数据。
- `GET /api/v1/history`：读取当前用户的会话列表。
- `GET /api/v1/history/{conversation_id}`：读取当前用户的一次完整会话。

## 配置 MySQL

先创建一个 UTF-8 数据库，再在本地 `.env` 中填写以下变量。API 启动时会自动创建 `users`、`conversations`、`messages` 和 `watchlist_items` 表：

```dotenv
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=wence
MYSQL_PASSWORD=你的数据库密码
MYSQL_DATABASE=wence_zhitou
WENCE_AUTH_SECRET=至少32位的随机字符串
WENCE_AUTH_TOKEN_TTL_HOURS=24
WENCE_SESSION_MAX_WORKERS=100
WENCE_SESSION_IDLE_SECONDS=600
```

线程池采用“逻辑会话槽 + 按需工作线程”的方式：空闲用户不会永久占用 OS 线程；数据库持久化等阻塞任务在已认证请求期间提交到固定线程池。执行中的请求不会被空闲清理器回收。`GET /api/v1/readiness` 和 `GET /api/v1/metrics` 的 `session_thread_pool` 字段会返回最大工作线程、活动会话、在途请求和剩余槽位。

Streamlit 每次由用户操作触发完整重跑时都会刷新活动时间，并以不刷新活动时间的状态轮询发现已过期租约。页面关闭后，Streamlit 服务器通过活动连接状态在约 3 秒宽限期后调用回收接口；为防止回收凭据被发送到外部地址，即时关页回收只对本机回环 API 启用，其他部署仍由 10 分钟服务端超时回收。

`.env` 已被 Git 忽略，禁止把真实数据库密码或签名密钥写入 `.env.example` 或提交到仓库。

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
$env:DEEPSEEK_BASE_URL="https://你的模型服务/v1"
$env:DEEPSEEK_API_KEY="你的模型密钥"
$env:DEEPSEEK_MODEL="模型名称"
```

模型输入包含最近 10 条显式会话上下文（API 接受最多 20 条）、已确认画像、授权事实和规则基线。意图、画像和复核使用受限 Schema；专业输出通过 `AgentResult` 校验，引用来源由事实层重新生成。画像只提取有原文证据的允许字段，永远先生成未确认草稿；结构化填写优先于模型提取。

调用量按固定流程控制：

- 一次分析只做一次意图与请求风险判断，取数和编排直接复用结果；风险请求在取数和专业节点前返回。
- 专业节点按意图选取，最多 5 个；所有意见的语义合规和矛盾检查合并为一次调用。正常完整分析最多 7 次逻辑调用，画像评估有文本时最多 1 次。
- 同一进程的语义服务与专业智能体共享客户端，默认最多 5 个模型请求并发；每次逻辑调用默认 30 秒总预算，包含排队、网络和重试；专业节点与合规节点的等待上限随模型预算同步调整。
- 仅网络故障、429 或 5xx 最多重试 1 次，因此完整分析的 HTTP 尝试数上限为 14；认证失败和无效 JSON 不重试。可设置 `WENCE_LLM_MAX_RETRIES=0` 进一步节省调用。
- 每次默认输出上限 2000 tokens，输入上限 60000 字符；超限直接降级，不截断证据或用户语义。不使用跨用户文本缓存。
- 追问不重复取数：同一研究对象的资料若**整批仍在各自时效内**，直接沿用而不再次调用外部数据源，审计结果记录在 `data_acquisition.reused_capabilities`。研究对象由语义判断一并抽取，只用作"这份资料是否已经取过"的复用键，不改变发给数据源的查询文本；组合诊断以持仓集合为研究对象。抽不到研究对象时退回按原话匹配——换一种问法就会重新取数，这是刻意的保守行为。该机制只省去取数那一段，模型生成时间不变。

取数复用的判据刻意不维护"哪些字段才算够用"的清单：数据源每次返回的字段并不稳定，按字段清单判断会让一次缺字段就永久无法复用，而且映射一旦与实际返回不符，就会出现"少取了资料却没人发现"的隐性损失。只带查询摘要（`provider_response`）、没有任何业务字段的调用不算"已经取到资料"，下一轮必须重试。

可通过 `WENCE_LLM_TIMEOUT_SECONDS`（大于 0、最多 60）、`WENCE_LLM_MAX_RETRIES`（0 或 1）、`WENCE_LLM_MAX_CONCURRENCY`、`WENCE_LLM_MAX_OUTPUT_TOKENS` 和 `WENCE_LLM_MAX_INPUT_CHARS` 调整限制，默认值见 `.env.example`。

`WENCE_LLM_THINKING_MODE=auto` 默认对 DeepSeek V4 设置 `thinking.type=disabled`，避免思考 token 耗尽 2000 token 预算后正文为空；其他模型不发送这个扩展参数。也可显式选择 `disabled`、`enabled` 或 `omit`（由服务商决定）。启用思考时需为思考与正文共同预留输出预算；客户端拒绝 `finish_reason=length` 的截断结果。参数协议见 [DeepSeek 官方文档](https://api-docs.deepseek.com/guides/thinking_mode/)。

模型未配置、超时或输出非法时：意图返回 `unknown`，理由为统一的重试提示；模型给出判定但置信度低于 0.65 时：意图同样保守降级为 `unknown` 并澄清，但沿用模型写明的具体理由（例如"未指明研究对象"），让用户知道该补充什么，而不是收到与真实原因无关的"语义判断不可用"；画像不推测缺失值；专业输出保留规则基线；最终语义审核失败进入 `REVIEW`。就绪接口的 `language_processing` 表示自然语言能力是否已配置。

直接使用 Python 接口时，`assess_profile` 和 `understand_intent` 现在需要 `await`；同步 `plan` 需要传入已判断的 `Intent`。建议优先使用异步 `CoordinatorAgent.run`，或用 `understand_request` 取得结果后通过 `understanding=` 传给 `run`。

## 运行测试

```powershell
python -m pytest -q
```

主协调智能体位于 `backend/app/agents/coordinator.py`，五类规则智能体位于 `backend/app/agents/rule_agents.py`，第三方模型混合层位于 `backend/app/agents/llm_agents.py`，问财数据适配器位于 `backend/app/data_provider/iwencai.py`。

测试包含自动取数路由、派生血缘、过期事实保护和 API 闭环回传。100 并发测试使用进程内 ASGI 通路验证，不等同于包含公网、第三方模型、真实数据源和多实例部署的生产压测；正式提交前仍应在目标部署环境执行持续压测和可用性观测。

真实模型冒烟验证需显式执行以下命令，会读取本地模型配置并产生少量真实调用，只发送合成测试输入，不调用行情源或数据库：

```powershell
python scripts/verify_live_llm.py
```

结果保存到 `deliverables/live_llm_verification.json`，包含各步骤判定、调用耗时和是否发生降级，不记录密钥。普通 `pytest` 不会触发真实调用。

## 启动 Streamlit 前端

先在一个终端启动 API，再打开第二个终端并执行：

```powershell
python -m streamlit run frontend/streamlit_app.py
```

浏览器访问终端显示的地址（通常是 `http://localhost:8501`）。首次进入先注册或登录，在“投资偏好”中填写并确认个人情况，即可开始研究。

前端提供五个入口：投资问答、自选研究、持仓分析、历史记录、投资偏好。首页增加研究工作台，集中展示资料、自选、持仓与偏好状态，并根据当前状态提示下一步操作。取数与问答仍在同一页，提问前备料、提问后核对依据都不需要切换界面。

- 投资问答：负责提问与备料。可以按研究方向（个股、行业、市场、基金、可转债）提问，也可以直接对话；页尾的“研究资料”面板内嵌查询、补充和整理三个页签，查询行情、财务指标、新闻、公告、研报、基金/ETF、行业排名和可转债，手工补充带来源与日期的资料，并搜索、移除或清空现有资料。手工资料百分比请带 `%`。
- 自选研究：按账号保存股票、基金、行业和可转债自选项，支持一键发起研究，并可选择 2 至 4 项进行横向比较；内部记录编号不在业务界面展示。
- 持仓分析：除组合诊断外，增加仓位分布、最大/前两大持仓、单一标的上限、未配置仓位和机械压力测试。压力测试只做确定性情景测算，不作为价格预测。
- 历史记录：包含“对话记录”和“分析详情”两个页签。分析详情按分析记录查看分项观点、实际引用与未引用资料、完成情况；内部评分、节点编号等不展示。

结果仍以结论、注意事项和下一步为主，详细信息按需展开。提问与资料面板共用同一份研究资料：面板中新增或移除都会影响之后的分析，新对话也会保留事先准备的研究资料；移除当前资料不会改动已有分析的依据。历史对话可恢复后继续提问。

界面采用"品牌酒红 + 中性冷灰"的终端式视觉：品牌主色保持 `#AD384E`，正文与数据回到高对比底色，侧栏为整行高亮的胶囊导航，页面顶部条显示资料条数与偏好确认状态，首页与持仓页以数据卡概览关键数值，风险/合规提示用红、正常/已完成用绿。样式分层如下：

- 颜色、字体、圆角、语义色与图表色板：`.streamlit/config.toml`
- 页面底色层次、卡片、侧栏导航、数据卡与空状态：`frontend/assets/app.css`
- 页面结构、数据卡内容与状态徽标：`frontend/streamlit_app.py`

页面底色使用品牌玫红竖向渐变（顶部约一半为纯色平台，下半段过渡到 `#FEFEFE`），以 CSS 渐变等价实现，因此不再需要随项目保存背景图片；卡片、指标卡与空状态统一改为实心白底并加一层淡阴影，保证在粉底上仍有足够对比度。页面不展示启动命令、演示数据入口、服务状态或智能体内部指标；开发与诊断说明保留在本文档和 API 中。

## 提交材料

正式交付物统一放在 `deliverables/`：项目简介 PPT、项目技术说明书、产品使用说明、系统测试报告、项目分工开发过程与训练记录，以及演示视频。团队成员真实姓名和实际职责必须在提交前由团队核对，项目不会凭空生成个人贡献记录。
