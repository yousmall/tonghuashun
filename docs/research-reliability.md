# 研究可靠性与性能配置

本轮针对完整测试发现的六项问题实施修改，继续使用现有 FastAPI、Streamlit 和只读问财适配器。

## 百分比事实

`FactRecord.value` 保留原始值。百分比字段使用明确单位：`unit: "percent"` 表示百分点，`unit: "ratio"` 表示小数比例。服务端重新计算 `normalized_value`，单位统一为百分点；调用方填写的规范化数值不能覆盖转换结果。

以下三种写法均表示 0.5%，产生相同规则评分：

```json
{"field": "change", "value": "0.5%"}
{"field": "change", "value": 0.5, "unit": "percent"}
{"field": "change", "value": 0.005, "unit": "ratio"}
```

无单位的裸数值不参与百分比评分或百分比对比图。旧资料没有明确单位时需要重新取数或补充单位；历史原值继续保留。问财已知比率字段按供应商的百分点约定转换，CPI/PPI 指数必须有明确百分比或同比口径。手工输入的百分号保留在原值中。

规则派生版本升级为 `DERIVED_RULE_V2`；输入刷新后重新派生。行业中值与公司指标分开，毛利率、净利率和营业利润率不再归入宏观利率。跨来源比较使用规范化百分比，同时保留原字段、单位、报告期和快照以便定位真实冲突。

## 历史证据

消息摘要仍保留最多 60 条证据预览。全部被引用事实、交叉核验冲突事实和多层派生输入存入独立的 `research_evidence` 表，与助手消息和账号关联，在同一事务中写入。用户请求中的大事实数组不再重复写进消息行。

历史详情通过当前账号和已分页消息 ID 批量恢复完整证据；历史列表不读取证据 JSON。新表由现有 `Database.initialize()` 在应用启动时创建，不要求改动已有表字段。旧记录已经丢失的证据无法凭空恢复；新记录会记录缺失引用 ID，避免误报引用完整。

## 研究范围与资料缺口

基础取数计划覆盖实际运行的专业节点。行业、基金、可转债研究均包含宏观能力。证券能力使用当前问题中的明确证券代码或经问题、用户上下文及授权资料确认的对象；当前明确代码优先于模型提议。

宏观查询使用宏观范围，证券相关行业查询使用所属行业范围；筛选条件和专题新闻保留原问题。事件资料改用公告检索，保留文档日期和链接，事件与公告能力查询相同时合并外部调用。模型只能选择固定能力枚举。

`data_acquisition.missing_fields_by_agent` 根据有效事实、明确数值和同一实体统计维度缺口。缺少维度不填中性分；界面及导出文本会解释缺口。取得多条原始资料不等于已形成完整宏观、行业或治理评分。

服务器固定查询“中国最新宏观经济”时，供应商用作实体名的已知 PMI、CPI、PPI 指标名归入“中国宏观经济”分析范围，原指标名仍保存在 `source_field` 中。只匹配明确的指标名称；自定义查询、不同国家的指标及调用方快照不合并。单位、报告期、来源和引用血缘仍保留，通胀评分必须有明确百分比输入。

数据适配器保留指标行上明确的实体和国家/地区信息。单项查询的 401/403 等权限或参数拒绝不会使全部能力熔断；这些查询仍失败并按能力记录。连续网络故障、服务端故障或限流仍触发原有全局熔断，避免向不可用服务持续发送请求。

每次请求在分析前和专业 agent 研判后各最多补取一轮，每轮最多 12 个数据能力调用，并分别受 `WENCE_RECOVERY_TIMEOUT_SECONDS` 约束。已知评分缺项先补取；agent 在研判中发现的新缺项可通过 `AgentResult.data_requirements` 请求职责范围内的问财只读能力。后端按固定方法映射、已验证研究对象和能力权限执行，不接受模型提供的方法名、URL 或密钥。补取取得新资料后重新运行完整专业分析、事实核验和风险检查；同一阶段不会再次补取。缺项、空返回、超时、权限拒绝及真实冲突仍保持待复核，矛盾原始记录继续保留。

`recovery_phases` 记录已执行阶段，`recovery_agent_requirements` 记录 agent 请求，`recovery_attempts` 分别保存两阶段的能力、成功/失败、原因码和前后缺项；现有 `recovery_*` 明细字段表示最近一次补取。历史记录保留两阶段审计。

首次补取强制刷新公开来源，成功结果更新能力缓存。分析前的相同查询在刷新后 30 秒内可复用仍有效的补取结果；并发补取合并为一次外部调用。这段复用期不能延长事实的时效，分析后的冲突或引用问题始终强制刷新。空结果和失败不缓存；缓存不保存用户画像、持仓或调用方资料。重复取得同一公开资料不会被当成独立来源确认。

补取审计增加 `recovery_phase`（`before_analysis` / `after_analysis`）、`recovery_missing_fields_before`、`recovery_timings_ms`、`recovery_cached_capabilities`。`recovery_reanalyzed` 只在已经分析后再次执行协调器时为真；分析前补取保持为假。

## 模式、缓存与观察指标

`research_mode` 支持 `deep`（默认）和 `fast`，投资问答页面提供选择。两种模式保留相同事实核验和风险审核。快速模式缩短正文和输出预算、减少每种文档的输入数量；规则基线及派生血缘保留。它不承诺每次请求都更快，也不改变外部数据权限。

公开研究资料缓存最多保存 128 个能力查询。默认以 `.tmp_flow/public_research_cache.sqlite3` 在同一主机多个 API 进程间共享；取数租约合并并发调用，过期写入者不能覆盖新证据。缓存按供应商授权摘要分区，只存适配器返回的公开事实，每次读取逐字段重新检查时效。画像、持仓、调用方资料和模型回答不进入这份共享缓存。空结果与失败不缓存；共享存储运行时故障降为进程内取数，并通过 readiness 披露错误类型。它不支持多台主机通过网络文件系统共享 SQLite。

模型输出另有短期内存缓存：仅在已认证账号下，对账号、研究模式、系统指令和完整输入的摘要精确匹配；默认 120 秒，最多 256 项。不写磁盘，不跨账号或进程共享；改变事实、画像或上下文即不复用。缓存命中后仍执行结构校验、引用校验、规则约束、事实核验与合规合并。`model_calls.cache_hit` 区分真实调用与复用，复用费用为零，原始调用保留供应商 token 用量。失败不缓存；一个等待者取消不会取消其他等待者的调用。

同机登录会话改用 `.tmp_flow/login_sessions.sqlite3` 共享租约，修复令牌在另一 API 进程返回本机 401 的问题。100 个会话上限按共享存储统一统计；退出、浏览器回收凭据和空闲回收对全部进程生效。请求持有有期限的租约，避免进程崩溃后永久占位；工作线程仍属于各自进程。此项与供应商返回的 401 分开诊断。

`WENCE_RESEARCH_MAX_CONCURRENCY` 默认每进程 8 个研究请求，排队预算由 `WENCE_RESEARCH_QUEUE_SECONDS` 控制，默认 1 秒。超载返回 HTTP 429 和 `Retry-After`，不发起该请求的付费模型调用。此限制是过载保护，不能等同于每秒吞吐量或百用户研究容量。完整回答耗时通过 `timings_ms.total` 和压测的实际 HTTP 响应时间记录，流式阶段提示不算完整回答。

建议包提供阶段耗时 `timings_ms`、每项能力耗时与缓存命中、专业节点耗时和 `model_calls`。模型调用统计包含排队、网络、尝试次数、输入事实数量、供应商报告的 token 用量与调用状态，不保存提示词、密钥或请求头。真实模型的百用户容量和长期 P95 仍需要独立验证。

可以通过环境变量设置模型预算：

- `WENCE_LLM_MAX_CONCURRENCY`、`WENCE_LLM_TIMEOUT_SECONDS`、`WENCE_LLM_MAX_RETRIES`。
- `WENCE_LLM_MAX_INPUT_CHARS`、`WENCE_LLM_MAX_OUTPUT_TOKENS`。
- `WENCE_LLM_INPUT_USD_PER_MILLION`、`WENCE_LLM_OUTPUT_USD_PER_MILLION`：供应商对应的 USD/百万 token 单价。仅在两个单价和 token 用量均已知时计算估算成本；未配置时成本为 `null`，不猜测价格。估算不替代供应商账单。

建议包及历史增加 `model_cost`，包含已知费用小计、未知调用数量、缓存命中数和估算总额。只要存在费用未知的调用，总额继续为 `null`，避免把部分账单当作全部费用。readiness 的 `model_pricing_configured` 可检查两个单价是否齐全；实际合同单价需由账号所有者提供。

取数审计增加 `capability_errors` 和 `recovery_errors`：供应商 401 标记 `AUTHENTICATION_REJECTED`，403 标记 `CAPABILITY_FORBIDDEN`。相同失败能力不会在补取阶段重复请求；所有已请求能力均拒绝认证且没有成功或复用资料时，不再追加新的补取能力。错误摘要仅包含状态码、原因码和是否可重试，不保存响应正文或请求头。项目代码无法替供应商开通账号授权。

同一公开查询的授权拒绝另有 15 秒错误状态抑制，供同机进程共同读取；它不把失败当作有效事实，也不影响其他查询或能力。更换任一已配置密钥会改变授权命名空间，立即绕过旧状态。适配器支持按 Skill 配置 `IWENCAI_MARKET_API_KEY`、`IWENCAI_FINANCE_API_KEY`、`IWENCAI_MACRO_API_KEY`、`IWENCAI_FUND_API_KEY`、`IWENCAI_SELECTOR_API_KEY` 及公告/新闻/研报专用密钥；空值沿用原 `IWENCAI_API_KEY`。此配置用于供应商实际分别签发凭据的场景，不能扩大凭据权限。

同一账号、相同实际原始输入的规则派生结果会保留第一次计算时间，而不是每次重写评分时间戳。派生规则仍先计算选定输入，完整原始证据仍进入冲突核验；任一输入、规则输出或账号变化即重新生成。该短期内存复用防止热请求因无意义的时间戳变化重新调用全部模型，不延长原始事实和评分的时效。

## 新增评分输入口径

以下是可复算的研究代理规则，没有收益预测或实证校准保证。`clip` 将值限制为 0 至 100，百分比输入使用百分点；没有单位、字段过期或质量不足时不生成。派生事实保存 `derivation_rule` 和全部 `derived_from`，历史继续保存其完整输入血缘。

| 维度 | 原始输入 | 规则 |
| --- | --- | --- |
| 流动性 | `m2_growth`，M2 同比百分点 | `clip(50+5*(x-8))`，已有有效流动性分或利率代理分优先 |
| 风险偏好 | `market_advancing_ratio`，同一市场上涨家数占比 | 百分点原值，限 0 至 100 |
| 行业景气 | `industry_revenue_growth`，行业收入同比百分点 | `clip(50+2*x)` |
| 资金流向 | `capital_flow`、`turnover_value` | `clip(50+50*净流入/成交额)`；相同实体、货币单位和非空期间；成交额为正，净流入绝对值不超过成交额 |
| 拥挤度 | `industry_turnover_percentile`，明确的历史百分位 | `100-x`，高分表示较不拥挤，限 0 至 100 |

政策、事件、治理不能从文档数量或“未找到处罚”推断。调用方可以提供有原文依据的结构化评估事实：`policy_assessment`、`event_assessment`、`governance_assessment`。值必须标明对应版本 `POLICY_V1`、`EVENT_V1`、`GOVERNANCE_V1`，并显式 `complete: true`；每个分项要有 `label` 和 `evidence_id`。引用必须来自当前同一实体、带期间和公开 HTTPS 链接的公告、新闻、事件或研报。结构化标签是明确提供的评估输入，代码只验证引用和计算，不能证明人工解读在语义上正确。

政策标签 supportive/neutral/restrictive 对应 75/50/25；事件 favorable/neutral/adverse 对应 75/50/25。治理必须同时具备审计意见、监管状态、披露状态，不能只补其中一项：audit_opinion 的 unqualified/qualified/adverse/disclaimer 为 100/50/0/0；regulatory_status 的 explicitly_clear/penalty 为 100/0；disclosure_status 的 timely/delayed 为 100/0；三项取平均。缺少明确的清查证据就不能填写 explicitly_clear。

```json
{"rubric":"POLICY_V1","complete":true,
 "policy":{"label":"supportive","evidence_id":"NOTICE-001"}}
```

问财查询新增相应原始指标及单位别名，但供应商是否提供这些指标仍以实际返回为准。缺项会继续显示在 `missing_fields_by_agent`。个股规则按五个不同的评分维度判断完成状态；重复同一字段不能提高覆盖率，缺项保持降级并列出缺失维度。

## 可复用容量验收

`scripts/verify_research_capacity.py` 创建独立 SQLite 账号库，以正常注册、19题测评和确认接口建立测试账号，然后启动两个 API 进程。报告分别保存冷请求、热请求、并发轮次、实际持续窗口、完整研究响应 P50/P95、429、未形成研究的 200、模型调用与缓存命中、缺项和历史引用恢复结果。默认不会读取或修改生产账号数据。

离线验证使用 `--offline`，上游模型及数据均为明确标注的合成响应，HTTP、两个 API 进程、账号隔离及历史保存是真实执行；不能据此声称真实模型百用户容量。真实模式会消耗模型费用和供应商配额，需要执行者确认测试授权。至少一分钟实测也不等于长期生产 P95，持续时长以报告的 `measured_load_window_seconds` 为准。

## 评分职责

`AgentResult.score` 和 `rule_score` 保持可复算规则分；模型返回的数值单独存为 `model_score`。`score_policy` 为 `rule_only`。偏离或缺失规则分时给出 `score_difference_reason`，模型分不能代替规则分参加共识。执行记录中展示两种评分及差异；评分不表示预期收益。

## 同类项目参考

本项目采用适合现有规模的改进，没有迁移框架。数据单位与供应商字段标准化参考 [OpenBB 标准化说明](https://docs.openbb.co/odp/python/developer/standardization)。数据组织、定量计算与定性研判分工参考 [FinRobot 论文](https://arxiv.org/abs/2411.08804)；任务质量验证参考 [FinGPT 基准说明](https://github.com/AI4Finance-Foundation/FinGPT/blob/master/fingpt/FinGPT_Benchmark/readme.md)。上述是设计参考，当前实现与验收结果以本仓库的测试和报告为准。
