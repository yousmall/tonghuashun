"""投资教学、已确认偏好/持仓、费用复盘。所有价格均为用户输入的情景值。"""
from __future__ import annotations
from collections import defaultdict, deque
from datetime import date
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

class Holding(Input):
    code: str = Field(pattern=r"^\d{6}$")
    industry: str = Field(min_length=1, max_length=60)
    value: Decimal = Field(gt=0, le=1e12, decimal_places=2)

class Preferences(Input):
    industries: list[str] = Field(default_factory=list, max_length=10)
    avoid_industries: list[str] = Field(default_factory=list, max_length=10)
    style: Literal["未设定", "短期观察", "中长期研究"] = "未设定"
    mode: Literal["prefer", "restrict"] = "prefer"
    commission_rate: Decimal = Field(default=Decimal(".0003"), ge=0, le=.05)
    minimum_commission: Decimal = Field(default=Decimal("5"), ge=0, le=1000)
    sell_tax_rate: Decimal = Field(default=Decimal(".0005"), ge=0, le=.05)
    slippage_rate: Decimal = Field(default=Decimal(".001"), ge=0, le=.05)
    planned_trade_amount: Decimal | None = Field(default=None, gt=0, le=1e8, decimal_places=2)
    max_round_trip_cost_ratio: float = Field(default=.02, ge=0, le=.2)
    @model_validator(mode="after")
    def labels(self):
        for field in ("industries", "avoid_industries"):
            values = list(dict.fromkeys(s.strip() for s in getattr(self, field)))
            if any(not s or len(s)>40 or any(ch in s for ch in "\n\r，,；;") for s in values):
                raise ValueError("行业标签应为1至40字的独立名称")
            setattr(self, field, values)
        if set(self.industries) & set(self.avoid_industries):
            raise ValueError("偏好和排除行业不能相同")
        return self

class WorkspaceSave(Input):
    expected_version: int = Field(ge=1)
    preferences: Preferences
    holdings: list[Holding] = Field(default_factory=list, max_length=100)
    holdings_as_of: date
    cash: Decimal = Field(ge=0, le=1e12, decimal_places=2)
    holdings_complete: bool = False
    @model_validator(mode="after")
    def validate_holdings(self):
        if len({h.code for h in self.holdings}) != len(self.holdings):
            raise ValueError("持仓代码不能重复")
        from backend.app.risk_questionnaire import local_today
        if self.holdings_as_of > local_today():
            raise ValueError("持仓日期不能晚于今天")
        if self.holdings_complete and self.cash + sum(h.value for h in self.holdings)<=0:
            raise ValueError("完整资产必须大于零")
        return self

class Trade(Input):
    date: date
    code: str = Field(pattern=r"^\d{6}$")
    side: Literal["buy", "sell"]
    quantity: int = Field(gt=0, le=100000000)
    price: Decimal = Field(gt=0, le=1000000, decimal_places=4)
    fee: Decimal | None = Field(default=None, ge=0, le=1e9, decimal_places=2)
    industry: str = Field(default="未分类", max_length=60)

class Nav(Input):
    date: date
    value: Decimal = Field(gt=0, le=1e14)
    flow: Decimal = Field(default=0, ge=-1e14, le=1e14)

class OpeningPosition(Input):
    code: str=Field(pattern=r"^\d{6}$")
    quantity: int=Field(gt=0,le=100000000)
    unit_cost: Decimal=Field(gt=0,le=1000000,decimal_places=4)
    acquired_on: date
    industry: str=Field(default="未分类",max_length=60)

class ReviewRequest(Input):
    trades: list[Trade] = Field(default_factory=list, max_length=20000)
    nav: list[Nav] = Field(default_factory=list, max_length=4000)
    opening_positions: list[OpeningPosition] = Field(default_factory=list, max_length=1000)

def review_trades(request: ReviewRequest):
    lots=defaultdict(deque); closed=[]; missing_cost=0; unmatched=0
    fees=Decimal(0)
    for opening in request.opening_positions:
        if request.trades and opening.acquired_on>=min(t.date for t in request.trades):
            raise ValueError("期初持仓取得日期必须早于第一笔成交；单位成本应已含历史买入费用")
        lots[opening.code].append([opening.quantity,opening.unit_cost,opening.acquired_on,Decimal(0),opening.industry])
    for queue in lots.values():
        ordered=sorted(queue,key=lambda lot:lot[2]); queue.clear(); queue.extend(ordered)
    for t in sorted(enumerate(request.trades), key=lambda pair:(pair[1].date,pair[0])):
        _, t=t
        if t.fee is None: missing_cost+=1
        else: fees+=t.fee
        if t.side=="buy":
            lots[t.code].append([t.quantity,t.price,t.date,t.fee/t.quantity if t.fee is not None else None,t.industry])
            continue
        qty=t.quantity; gross=Decimal(0); cost=Decimal(0); known=t.fee is not None; weighted_days=0
        while qty and lots[t.code]:
            lot=lots[t.code][0]; n=min(qty,lot[0]); gross+=(t.price-lot[1])*n
            known=known and lot[3] is not None
            if lot[3] is not None: cost+=lot[3]*n
            weighted_days+=(t.date-lot[2]).days*n
            qty-=n; lot[0]-=n
            if not lot[0]: lots[t.code].popleft()
        if qty: unmatched+=1; continue
        if t.fee is not None: cost+=t.fee
        closed.append({"code":t.code,"industry":t.industry,"date":t.date.isoformat(),
            "gross":float(gross),"net":float(gross-cost) if known else None,
            "matched_cost":float(cost) if known else None,"holding_days":weighted_days/t.quantity})
    complete=bool(closed) and not unmatched and not missing_cost
    groups=[]
    for code in dict.fromkeys(row["code"] for row in closed):
        rows=[r for r in closed if r["code"]==code]; net_known=all(r["net"] is not None for r in rows)
        groups.append({"code":code,"closed_sells":len(rows),"gross":round(sum(r["gross"] for r in rows),2),
            "net":round(sum(r["net"] for r in rows),2) if net_known else None,
            "win_rate":sum(r["net"]>0 for r in rows)/len(rows) if net_known else None})
    performance=None
    if request.nav:
        nav=sorted(request.nav,key=lambda n:n.date)
        if len({n.date for n in nav})!=len(nav) or nav[0].flow:
            raise ValueError("净值日期不能重复，首期作为基准不能带现金流")
        factor=Decimal(1); peak=Decimal(1); drawdown=Decimal(0)
        for prev,cur in zip(nav,nav[1:]):
            # flow 为期末发生的净入金；不对不明时点现金流计算收益。
            adjusted=cur.value-cur.flow
            if adjusted<=0: raise ValueError("扣除期末净入金后的资产必须为正")
            factor*=adjusted/prev.value; peak=max(peak,factor); drawdown=max(drawdown,1-factor/peak)
        performance={"period_return":float(factor-1) if len(nav)>1 else None,
            "max_drawdown":float(drawdown) if len(nav)>1 else None,"observations":len(nav),
            "basis":"按用户资产估值和期末净入金计算时间加权收益；最大回撤仅覆盖录入观察点，非逐日数据不能代表逐日最大回撤。"}
    return {"closed":closed,"by_security":groups,"known_fees":float(fees),"missing_fee_rows":missing_cost,
        "unmatched_sells":unmatched,"matched_net":round(sum(r["net"] for r in closed),2) if complete else None,
        "performance":performance,"status":"complete_matched_sample" if complete else "partial",
        "notice":"按录入行序处理同日成交并先进先出匹配；净损益仅覆盖可完整匹配卖出，不含未平仓、分红、拆并股及期初缺失成本，不是账户总收益。费用缺失不按零处理。"}

def history_suggestions(analysis):
    industries=[]
    if analysis.get("confirmed"):
        for section in analysis.get("report_sections",[]):
            if "题材" in section.get("title",""):
                industries=list(dict.fromkeys(r["label"].split("/")[0].strip() for r in section["rows"] if r["label"]!="其他"))
    return {"industries":industries,"style":"短期观察" if (analysis.get("reported_median_holding_days") or analysis.get("average_matched_holding_days") or 999)<=30 else "未设定",
            "notice":"历史题材及持有期仅生成待确认建议；不代表策略盈利或风险承受能力。"}

def fit_candidate(profile, *, code, industry, now=None):
    """在既有事实/适当性闸门之后使用；没有完整持仓时不给可买金额。"""
    from backend.app.risk_questionnaire import local_today
    now=now or local_today(); workspace=profile.investment_workspace or {}
    pref=Preferences.model_validate(workspace.get("preferences",{}))
    match=lambda labels: any(s.casefold() in industry.casefold() for s in labels)
    result={"allowed":True,"bonus":0,"reasons":[],"max_additional_amount":None,
        "industry":industry,"basis":"用户确认偏好；偏好加分最多5分，仅对已经通过证据核验的候选排序，不改变研究分或风险等级。"}
    if match(pref.avoid_industries) or pref.mode=="restrict" and pref.industries and not match(pref.industries):
        result.update(allowed=False,reasons=["不符合已确认行业范围"]); return result
    if match(pref.industries): result.update(bonus=5,reasons=["符合已确认的行业研究偏好"])
    if pref.planned_trade_amount is not None:
        result["cost_scenario"]=cost_scenario(pref.planned_trade_amount,pref.model_dump())
        if result["cost_scenario"]["break_even_return"]>pref.max_round_trip_cost_ratio:
            result.update(allowed=False,reasons=["本次计划交易金额的往返费用情景超过用户确认的成本预算"])
    if not workspace.get("holdings_complete"):
        result["reasons"].append("未确认完整现有资产，无法核对新增持仓额度")
        return result
    day=date.fromisoformat(workspace["holdings_as_of"])
    if not 0<=(now-day).days<=7:
        result.update(allowed=False,reasons=["持仓估值超过7天或日期无效，请更新后再推荐"]); return result
    total=float(workspace.get("cash",0))+sum(float(h["value"]) for h in workspace.get("holdings",[]))
    single=sum(float(h["value"]) for h in workspace["holdings"] if h["code"]==code)
    sector=sum(float(h["value"]) for h in workspace["holdings"] if h["industry"].strip().casefold()==industry.strip().casefold())
    limit=max(0,min(float(workspace["cash"]),total*profile.single_security_limit-single,total*profile.industry_limit-sector))
    result["max_additional_amount"]=round(limit,2)
    if pref.planned_trade_amount is not None and float(pref.planned_trade_amount)>limit:
        result.update(allowed=False,reasons=["计划新增金额超过现金或单票/行业剩余额度"])
    result["reasons"].append("按完整资产与已确认单票、行业上限核对新增资金；行业名称须使用同一分类口径")
    if not industry or any(h["industry"]=="未分类" for h in workspace["holdings"]):
        result.update(allowed=False,reasons=["现有持仓或候选行业未分类，无法确认行业集中度"])
    elif limit<=0: result.update(allowed=False,reasons=["可用现金或单票/行业额度已用尽"])
    return result

def cost_scenario(amount, preferences):
    p=Preferences.model_validate(preferences or {}); a=Decimal(str(amount))
    if not a.is_finite() or a<=0 or a>Decimal("1e12"): raise ValueError("请提供有效情景金额")
    cost=2*max(p.minimum_commission,a*p.commission_rate)+a*p.sell_tax_rate+2*a*p.slippage_rate
    return {"amount":float(a),"round_trip_cost":float(cost.quantize(Decimal('.01'))),
        "break_even_return":float(cost/a),"notice":"按相同买卖金额、双边佣金及滑点的静态算术情景，费率为用户假设，未包含其他费用；不是上涨预测或交易所收费标准。"}

class PaperOrder(Input):
    expected_version: int=Field(ge=1)
    side: Literal["buy","sell"]
    code: str=Field(pattern=r"^\d{6}$")
    quantity: int=Field(gt=0,le=1000000)
    price: Decimal=Field(gt=0,le=1000000,decimal_places=4)
    fee: Decimal=Field(ge=0,le=1000000,decimal_places=2)
    date: date

class JournalEntry(Input):
    expected_version: int=Field(ge=1)
    code: str=Field(pattern=r"^\d{6}$")
    thesis: str=Field(min_length=1,max_length=1000)
    invalidation: str=Field(min_length=1,max_length=1000)
    review_date: date
    outcome: str=Field(default="",max_length=2000)

def paper_order(state, order):
    """教学假设：初始10万元、100股整手、买入次日才能卖出，不连接券商。"""
    from copy import deepcopy
    from backend.app.risk_questionnaire import local_today
    state=deepcopy(state or {"cash":"100000.00","trades":[]})
    trades=state["trades"]
    if len(trades)>=1000: raise ValueError("教学账户已达到1000笔记录上限")
    if order.quantity%100: raise ValueError("本教学练习每笔数量须为100股的整数倍")
    if order.date>local_today() or trades and order.date<date.fromisoformat(trades[-1]["date"]):
        raise ValueError("交易日期不能倒退或晚于今天")
    cash=Decimal(state["cash"]); amount=order.price*order.quantity
    if order.side=="buy":
        if amount+order.fee>cash: raise ValueError("模拟现金不足")
        cash-=amount+order.fee
    else:
        available=sum(t["quantity"]*(1 if t["side"]=="buy" else -1) for t in trades if t["code"]==order.code and t["date"]<order.date.isoformat())
        available-=sum(t["quantity"] for t in trades if t["code"]==order.code and t["side"]=="sell" and t["date"]==order.date.isoformat())
        if order.quantity>available: raise ValueError("模拟可卖持仓不足（教学练习禁止卖出当日买入股数）")
        if amount<order.fee: raise ValueError("费用不能超过卖出金额")
        cash+=amount-order.fee
    state["cash"]=str(cash.quantize(Decimal('.01')))
    trades.append(order.model_dump(mode="json",exclude={"expected_version"}))
    state["review"]=review_trades(ReviewRequest(trades=trades))
    return state
