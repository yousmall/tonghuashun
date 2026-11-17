"""由带真实交易日期收盘价推导的走势观察模型。

这些字段只描述已经发生的价格序列（五日均线、三日累计升幅、信号中和后的
方向判断），不预测收益、不进入投资建议的证据链与合规审核。
"""
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

Verdict = Literal["上行", "偏强震荡", "震荡", "偏弱震荡", "下行", "数据不足"]


class MovingAveragePoint(BaseModel):
    """均线序列中的一点；日期与原始收盘序列一一对应。"""

    date: date
    value: float = Field(gt=0)


class TrendSignal(BaseModel):
    """单个可复算信号；score 为正表示偏强，为负表示偏弱。"""

    key: str
    label: str
    reading: str
    direction: Literal["up", "down", "flat"] = "flat"
    score: float = Field(ge=-1, le=1)
    weight: float = Field(gt=0, le=1)
    note: str | None = None


class ThreeDayRise(BaseModel):
    """最近三个交易日的连续升幅与“上升高度”（同一计价单位）。"""

    available: bool = False
    change_pct: float | None = None
    height: float | None = None
    consecutive_up_days: int = Field(default=0, ge=0)
    all_up: bool = False
    daily_changes_pct: list[float] = Field(default_factory=list)
    start_date: date | None = None
    end_date: date | None = None


class IndicatorPoint(BaseModel):
    date: date
    values: dict[str, float | None] = Field(default_factory=dict)


class TechnicalIndicator(BaseModel):
    name: str
    category: str
    parameters: str
    method: str
    available: bool = False
    latest_date: date | None = None
    latest: dict[str, float] = Field(default_factory=dict)
    series: list[IndicatorPoint] = Field(default_factory=list)
    unit: str | None = None
    message: str | None = None


class TrendAnalysis(BaseModel):
    """多信号中和后的走势判断；信号冲突时明确降级为震荡。"""

    available: bool = False
    window: int = Field(default=0, ge=0)
    latest_close: float | None = None
    latest_date: date | None = None
    start_date: date | None = None
    ma5: float | None = None
    ma5_series: list[MovingAveragePoint] = Field(default_factory=list)
    ma5_slope_pct: float | None = None
    price_vs_ma5_pct: float | None = None
    ma10: float | None = None
    ma20: float | None = None
    ma5_vs_ma20_pct: float | None = None
    three_day: ThreeDayRise = Field(default_factory=ThreeDayRise)
    benchmark_name: str | None = None
    relative_strength_pct: float | None = None
    score: float | None = Field(default=None, ge=0, le=100)
    verdict: Verdict = "数据不足"
    neutralized: bool = False
    confidence: float = Field(default=0.0, ge=0, le=1)
    signals: list[TrendSignal] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    indicators: dict[str, TechnicalIndicator] = Field(default_factory=dict)
