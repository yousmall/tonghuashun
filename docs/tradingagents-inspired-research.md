# 参考 TradingAgents 的研究链路优化

本次保留现有专业节点并行和事实核验，在最终交付时增加结构化证据报告，不引入新的外部依赖。

## 改动
- API 新增 research_report：按“标的＋研究维度”组织证据，覆盖行情、经营、估值、资金、宏观、基金持仓及新闻事件。
- 每条证据保留事实编号、所属期、来源、原文链接、数值解读和支持/不利/中性分类。正向分类仅描述该指标的已发生表现，不代表买入评级。
- 规则评分独立说明，不将其当作原始金额、估值倍数或收益概率；没有有效日期的指标保留为中性待核实。
- 冲突事实及其派生后代撤出报告；缺失、过期、低质量、无完整血缘和未来日期的资料不能形成证据观点。
- 各维度保留判断限制及重新评估条件；多标的、不同财报期分别展示，限制报告大小并披露省略内容。
- 深入和快速模式分别生成简短的分项结论和投资逻辑链中的关键依据，避免在结论里复述证据数据；不能覆盖原问题时缩小判断范围。深入模式只列有实质影响的反面证据，不设最低字数；快速模式的逻辑链分项说明仍限制在 80 字内。不增加模型调用次数。
- 页面增加“研究证据对照”，历史记录与 PDF 使用相同报告内容。被风险检查拦截时，不展示证据观点。
- 普通分析及预生成报告打开入口都接入该报告层，旧报告重新打开时可按已复核事实生成对照。

## 数据流
授权资料 → 专业分析 → 引用与交叉核验 → 风险检查 → 结构化证据报告 → 页面 / 历史 / PDF。

这次实现的是基于现有证据的正反因素对照，没有声称额外执行了多空模型辩论，也没有新增交易执行或收益承诺。即时模式不会因为报告更丰富而增加实时取数或模型等待。

## 主要文件
- backend/app/models/research_report.py：报告数据契约。
- backend/app/services/research_report.py：有界证据整理、冲突血缘排除、维度解读。
- backend/app/agents/llm_agents.py：深入研究表达约束。
- frontend/research_report.py：页面和导出共用文案。
- tests/test_research_report.py：证据边界、接口、历史、页面、导出及模型调用契约。

## 参考
- [TradingAgents 项目说明](https://github.com/TauricResearch/TradingAgents)
- [分项报告组装](https://github.com/TauricResearch/TradingAgents/blob/main/tradingagents/reporting.py)
- [行情分析器](https://github.com/TauricResearch/TradingAgents/blob/main/tradingagents/agents/analysts/market_analyst.py)

## 验证范围
新增报告测试 16 项通过；相关组合验证 105 项通过。全量离线回归 971 项通过、4 项失败（52.16 秒），失败项与本轮修改前记录一致。完整结果见 outputs/testing/20261111-research-report.xml。
示例使用合成数据，不能作为当前行情或真实投资判断；未执行真实模型/实时数据端到端调用或当前已运行服务的重启验证。

### 仍未解决的全量检查失败
- test_speculation_key_requires_direction_and_target：测试请求缺少必填 profile。
- test_page_defaults_to_automatic_and_delivers_one_final_answer：未找到测试预期的研究模式选择框。
- test_analysis_deadline_cancels_work_and_releases_admission：预期 504，实际 500。
- test_streamlit_sends_selected_page_separately_from_question：未捕获测试预期的请求参数。

本次没有修改这些检查涉及的业务行为或测试断言。已有历史回答不会自动重写；后端加载新代码后，重新提问可获得研究证据对照。预生成报告通过已复核的打开入口交付时也会生成对照。
