---
name: iwencai-financial-research
description: Use the project's read-only iWenCai SkillHub adapter for current Chinese-market data, security or sector screening, company diagnosis, macro/events, institutional research, and news/report/announcement searches. Do not use it for order execution or unverified investment claims.
---

# iWenCai Financial Research

Use this skill when a task needs current or externally sourced financial facts that the local snapshot cannot supply. It routes to the project's existing `IwencaiSkillHubProvider`, so all returned items pass through the shared `FactRecord` normalization layer.

## Route the request

Read [references/capability-map.md](references/capability-map.md) when choosing a capability or when a request spans several research stages. Prefer the smallest set of calls that can answer the question.

For an end-user analysis, call `POST /api/v1/portfolio/analyze` with a confirmed profile. `auto_fetch` defaults to `true`: the backend classifies the intent, fetches the minimum evidence set concurrently, derives only traceable rule scores, verifies every cited fact, and returns `data_acquisition` plus the complete `facts` package. Use `auto_fetch: false` only when the caller explicitly wants supplied-snapshot analysis.

Use the helper from the repository root for diagnostics or an explicit one-capability fetch:

```powershell
python .agents/skills/iwencai-financial-research/scripts/fetch.py quote 600519
python .agents/skills/iwencai-financial-research/scripts/fetch.py research_report 贵州茅台
python .agents/skills/iwencai-financial-research/scripts/fetch.py stock_screen "近三年 ROE 大于 15% 且资产负债率低于 50%"
python .agents/skills/iwencai-financial-research/scripts/fetch.py fund 基金ETF --filters '{"risk_level":"R3","type":"宽基ETF"}'
```

If `IWENCAI_API_KEY` is absent, explain that the project remains in snapshot/rule-only mode and ask the user to configure the environment variable locally. Never ask them to paste a real key into source code, chat output, logs, or committed files.

## Evidence and safety

- Treat every result as time-bounded evidence, not as a recommendation. Preserve `source_id`, `snapshot_time`, `period`, and `quality` when passing facts to agents.
- Do not infer missing values or silently merge facts from different entities, periods, or accounting definitions.
- For a personalized portfolio conclusion, require a confirmed user profile and pass evidence through fact verification, cross-validation, and the compliance gate.
- State the data timestamp and important gaps. Separate reported facts from analysis and uncertainty.
- Never place orders, generate executable trade instructions, promise returns, or use SkillHub simulation-trading capabilities. This project's boundary is read-only research and investor education.
- Avoid broad calls when a precise symbol, market, period, or metric is available. Respect upstream rate limits and do not retry indefinitely.
