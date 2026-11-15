"""基金浏览字段：统一取数口径与展示分组，不推算供应商缺失值。"""

FUND_RETURNS = {
    "return_1m": "近1月收益率", "return_3m": "近3月收益率",
    "return_6m": "近6月收益率", "return_1y": "近1年收益率",
    "return_3y": "近3年收益率", "return_ytd": "今年以来收益率",
}
FUND_MONEY = {"yield_7d": "七日年化收益率", "income_per_10000": "每万份收益"}
FUND_PROFILE = {
    "fund_type": "基金类型", "fund_subtype": "基金细分类型", "fund_company": "基金管理人", "fund_manager": "基金经理",
    "fund_custodian": "基金托管人", "inception_date": "成立日期", "fund_size": "基金规模",
    "tracking_index": "跟踪标的", "performance_benchmark": "业绩比较基准",
    "subscription_status": "申购状态", "redemption_status": "赎回状态",
    "reported_fund_size": "净资产规模（披露）", "reported_inception_date": "原始成立日期（档案）",
}
FUND_FEES = {
    "fee_rate": "管理费率", "custody_fee_rate": "托管费率",
    "sales_service_fee_rate": "销售服务费率", "subscription_fee_rate": "申购费率",
    "redemption_fee_rate": "赎回费率",
}
FUND_RISK = {
    "fund_risk_level": "基金风险等级", "max_drawdown_1y": "近1年最大回撤",
    "volatility_1y": "近1年年化波动率", "sharpe_ratio_1y": "近1年夏普比率",
    "tracking_error": "跟踪误差",
}
FUND_ALLOCATION = {
    "stock_position": "股票仓位", "bond_position": "债券仓位", "cash_position": "现金仓位",
    "top10_weight": "前十大重仓股占比", "top_holdings": "重仓证券", "bond_holdings": "重仓债券", "industry_allocation": "行业配置",
}
FUND_PERCENT_FIELDS = frozenset({*FUND_RETURNS, *FUND_FEES, "yield_7d", "max_drawdown_1y", "volatility_1y",
                                 "stock_position", "bond_position", "cash_position", "top10_weight",
                                 "accumulated_nav_change", "reference_nav_change",
                                 *("reference_" + field for field in FUND_RETURNS)})
