# Capability map

The project intentionally exposes the SkillHub capabilities that support its research workflow. Simulation trading is excluded.

| Kind | Use for | Project method |
| --- | --- | --- |
| `quote` | Latest price, change, volume, turnover | `get_quote` |
| `financial` | Valuation and financial metrics | `get_financial_metrics` |
| `basic_info` | Listing profile, industry, main business | `get_basic_info` |
| `company_operations` | Revenue mix, customers, suppliers, subsidiaries, contracts | `get_company_operations` |
| `shareholder_equity` | Controllers, equity base, shareholder structure | `get_shareholder_equity` |
| `event` | Forecasts, ownership changes, pledges, unlocks, regulatory events | `get_event_data` |
| `macro` | CPI, PPI, PMI, rates, FX, social financing | `get_macro_data` |
| `institutional_research` | Ratings, target prices, earnings forecasts | `get_institutional_research` |
| `news` | Current financial news and policy developments | `get_news` |
| `research_report` | Broker and institutional research reports | `get_research_reports` |
| `announcement` | Listed-company disclosures | `get_announcements` |
| `stock_screen` | Natural-language A-share screening | `screen_stocks` |
| `sector_screen` | Natural-language sector or theme screening | `screen_sectors` |
| `fund` | Fund and ETF candidate screening | `get_fund_candidates` |
| `industry` | Industry performance, valuation, capital flow, prosperity | `get_industry_rank` |
| `convertible` | Convertible-bond price, premium, yield, size, rating | `get_convertible_bond` |

## Typical combinations

- Company diagnosis: `basic_info` + `financial` + `company_operations` + `shareholder_equity` + `event` + `announcement` + `research_report`.
- Market dashboard: `macro` + `industry` + `news`; add `quote` only for explicit instruments.
- Candidate research: `stock_screen`, `fund`, `sector_screen`, or `convertible`, followed by instrument-level fact calls for the shortlisted entities.
- Event analysis: `event` + `announcement` + `news`; use `research_report` only when institutional interpretation is needed.

Do not call every capability by default. Use the minimum evidence set that answers the request and makes gaps visible.

## Automatic analyze routes

The `AutomatedResearchPipeline` currently uses these default minimum sets:

- Market: `macro` + `industry` + `news`.
- Industry: `industry` + `news`.
- Security: `quote` + `financial` + `event` + `institutional_research`.
- Fund: `fund`.
- Convertible bond: `convertible` + `news`.
- Portfolio: `macro` + `industry`, plus `quote` + `financial` for at most four explicit holdings.

Education, unknown intent, or an unconfirmed profile never triggers external data calls. Each provider call fails independently; successful facts still proceed while failures are exposed only as capability names in the acquisition audit.
