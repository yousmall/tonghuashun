"""走势观察：五日均线、三日累计升幅与多信号中和判断。

设计约束（与项目其余部分一致）：

1. 只使用供应商返回的真实交易日期，同一天出现互相冲突的数值时整条丢弃，
   不用后取的值覆盖，也不插值或补零。
2. 每个信号都是可直接复算的简单比例，权重组内归一化；数据不足的信号直接
   退出计算，并在 ``notes`` 中说明，而不是按 0 分参与。
3. “中和判断”指多空信号同时成立时不做方向性结论：出现明显相反信号时，
   无论综合分多高都降级为“震荡”，避免单边叙事。
4. 这里是描述性技术观察，不预测收益，也不进入投资建议的证据与合规链。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Sequence

from backend.app.models.trend import (
    MovingAveragePoint,
    ThreeDayRise,
    TrendAnalysis,
    TrendSignal,
)

# 信号权重之和为 1.0（不含可选的相对强弱信号）；缺失信号时按剩余权重归一化。
WEIGHTS = {
    "ma5_slope": 0.28,
    "price_vs_ma5": 0.16,
    "ma5_vs_ma20": 0.20,
    "three_day": 0.20,
    "consistency": 0.16,
    "relative_strength": 0.15,
}
# 每个信号的“满分”幅度：达到该幅度即计 ±1，避免不同单位直接相加。
SCALES = {
    "ma5_slope": 2.5,
    "price_vs_ma5": 3.0,
    "ma5_vs_ma20": 3.0,
    "three_day": 6.0,
    "relative_strength": 4.0,
}
CONFLICT_THRESHOLD = 0.40
FLAT_THRESHOLD = 0.12


@dataclass(frozen=True)
class Series:
    points: list[tuple[date, float]]
    conflicts: int = 0

    def __len__(self) -> int:
        return len(self.points)


def _pair(item: Any) -> tuple[date | None, float | None]:
    """接受 ``(date, value)``、``PriceHistoryPoint``、``FactRecord`` 或字典。"""

    if isinstance(item, (tuple, list)) and len(item) == 2:
        day, value = item
    elif isinstance(item, dict):
        day, value = item.get("date") or item.get("period"), item.get("value")
    else:
        day = getattr(item, "date", None) or getattr(item, "period", None)
        value = getattr(item, "value", None)
    if isinstance(day, str):
        try:
            day = date.fromisoformat(day)
        except ValueError:
            return None, None
    if not isinstance(day, date):
        return None, None
    if isinstance(value, bool):
        return None, None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, None
    if not math.isfinite(number) or number <= 0:
        return None, None
    return day, number


def close_series(points: Iterable[Any]) -> Series:
    """按日期去重排序；同一天数值冲突时两条都不用，并记录冲突条数。"""

    values: dict[date, float] = {}
    conflicts: set[date] = set()
    for item in points or []:
        day, value = _pair(item)
        if day is None or value is None:
            continue
        previous = values.get(day)
        if previous is not None and not math.isclose(previous, value, rel_tol=1e-9, abs_tol=1e-9):
            conflicts.add(day)
        values[day] = value
    kept = [(day, value) for day, value in sorted(values.items()) if day not in conflicts]
    return Series(points=kept, conflicts=len(conflicts))


def moving_average(values: Sequence[float], window: int) -> list[float | None]:
    """简单移动平均；不足窗口的位置返回 ``None``，不向前填充。"""

    if window <= 0:
        raise ValueError("窗口必须为正整数")
    output: list[float | None] = []
    total = 0.0
    for index, value in enumerate(values):
        total += float(value)
        if index >= window:
            total -= float(values[index - window])
        output.append(total / window if index >= window - 1 else None)
    return output


def three_day_rise(series: Series) -> ThreeDayRise:
    """最近三个交易日的累计升幅与上升高度。"""

    points = series.points
    if len(points) < 4:
        return ThreeDayRise(available=False)
    window = points[-4:]
    closes = [value for _, value in window]
    change_pct = (closes[-1] / closes[0] - 1) * 100 if closes[0] else 0.0
    daily = [(closes[index] / closes[index - 1] - 1) * 100 for index in range(1, 4)]
    streak = 0
    for index in range(len(points) - 1, 0, -1):
        if points[index][1] > points[index - 1][1]:
            streak += 1
        else:
            break
    return ThreeDayRise(
        available=True,
        change_pct=round(change_pct, 2),
        height=round(closes[-1] - closes[0], 2),
        consecutive_up_days=streak,
        all_up=all(value > 0 for value in daily),
        daily_changes_pct=[round(value, 2) for value in daily],
        start_date=window[0][0],
        end_date=window[-1][0],
    )


def _clip(value: float, limit: float = 1.0) -> float:
    return max(-limit, min(limit, value))


def _scaled(value: float, scale: float) -> float:
    return _clip(value / scale if scale else 0.0)


def _direction(score: float) -> str:
    if score > FLAT_THRESHOLD:
        return "up"
    if score < -FLAT_THRESHOLD:
        return "down"
    return "flat"


def _relative_strength(series: Series, benchmark: Series | None) -> tuple[float | None, int]:
    """最近 5 个共同交易日的超额涨幅；共同日期不足则返回 ``None``。"""

    if benchmark is None or len(series) < 6 or len(benchmark) < 6:
        return None, 0
    left = dict(series.points[-6:])
    right = dict(benchmark.points[-6:])
    common = sorted(set(left) & set(right))
    if len(common) < 6:
        return None, len(common)
    base = common[0]
    asset_return = left[common[-1]] / left[base] - 1
    index_return = right[common[-1]] / right[base] - 1
    return (asset_return - index_return) * 100, len(common)


def _verdict(score: float) -> str:
    if score >= 65:
        return "上行"
    if score >= 55:
        return "偏强震荡"
    if score >= 45:
        return "震荡"
    if score >= 35:
        return "偏弱震荡"
    return "下行"


def analyze_trend(
    points: Iterable[Any],
    *,
    benchmark: Iterable[Any] | None = None,
    benchmark_name: str | None = None,
) -> TrendAnalysis:
    """计算均线、三日升幅与中和判断；数据不足时返回显式不可用。"""

    points = list(points)
    series = close_series(points)
    benchmark_series = close_series(benchmark) if benchmark is not None else None
    from backend.app.services.technical_indicators import calculate_indicators
    analysis = TrendAnalysis(window=len(series), benchmark_name=benchmark_name,
                             indicators=calculate_indicators(points))
    if series.conflicts:
        analysis.notes.append(f"有 {series.conflicts} 个交易日的数值自相矛盾，已整条剔除。")
    if len(series) == 0:
        analysis.notes.append("没有带真实交易日期的收盘价，无法计算走势。")
        return analysis

    analysis.start_date = series.points[0][0]
    analysis.latest_date = series.points[-1][0]
    analysis.latest_close = round(series.points[-1][1], 4)
    if len(series) < 6:
        analysis.notes.append(
            f"当前只有 {len(series)} 个交易日，五日均线至少需要 5 个收盘价且再往前 1 日用于比较。"
        )
        return analysis

    closes = [value for _, value in series.points]
    ma5_values = moving_average(closes, 5)
    ma10_values = moving_average(closes, 10)
    ma20_values = moving_average(closes, 20)
    ma5 = ma5_values[-1]
    ma5_prev = ma5_values[-2]
    assert ma5 is not None and ma5_prev is not None
    analysis.available = True
    analysis.ma5 = round(ma5, 4)
    analysis.ma5_series = [
        MovingAveragePoint(date=day, value=round(value, 4))
        for (day, _), value in zip(series.points, ma5_values)
        if value is not None
    ]
    analysis.ma5_slope_pct = round((ma5 / ma5_prev - 1) * 100, 2) if ma5_prev else None
    analysis.price_vs_ma5_pct = round((closes[-1] / ma5 - 1) * 100, 2)
    analysis.ma10 = round(ma10_values[-1], 4) if ma10_values[-1] is not None else None
    analysis.ma20 = round(ma20_values[-1], 4) if ma20_values[-1] is not None else None
    long_average = analysis.ma20 if analysis.ma20 is not None else analysis.ma10
    long_label = "MA20" if analysis.ma20 is not None else "MA10"
    if long_average is not None:
        analysis.ma5_vs_ma20_pct = round((ma5 / long_average - 1) * 100, 2)

    analysis.three_day = three_day_rise(series)

    signals: list[TrendSignal] = []
    slope = analysis.ma5_slope_pct or 0.0
    signals.append(TrendSignal(
        key="ma5_slope", label="五日均线斜率", reading=f"MA5 较前一交易日 {slope:+.2f}%",
        direction=_direction(_scaled(slope, SCALES["ma5_slope"])),
        score=round(_scaled(slope, SCALES["ma5_slope"]), 4), weight=WEIGHTS["ma5_slope"],
        note="均线斜率描述最近 5 日均价的重心变化，单日波动会放大读数。",
    ))
    bias = analysis.price_vs_ma5_pct or 0.0
    signals.append(TrendSignal(
        key="price_vs_ma5", label="收盘价相对五日均线", reading=f"收盘价相对 MA5 {bias:+.2f}%",
        direction=_direction(_scaled(bias, SCALES["price_vs_ma5"])),
        score=round(_scaled(bias, SCALES["price_vs_ma5"]), 4), weight=WEIGHTS["price_vs_ma5"],
        note="偏离过大时更容易出现均值回归，不等同于继续上涨。",
    ))
    if analysis.ma5_vs_ma20_pct is not None:
        spread = analysis.ma5_vs_ma20_pct
        signals.append(TrendSignal(
            key="ma5_vs_ma20", label=f"MA5 相对 {long_label}",
            reading=f"MA5 相对 {long_label} {spread:+.2f}%",
            direction=_direction(_scaled(spread, SCALES["ma5_vs_ma20"])),
            score=round(_scaled(spread, SCALES["ma5_vs_ma20"]), 4), weight=WEIGHTS["ma5_vs_ma20"],
            note=f"{long_label} 反映更长周期的持仓成本，交叉本身不是买卖信号。",
        ))
    else:
        analysis.notes.append("交易日不足 10 个，中长期均线未参与判断。")
    if analysis.three_day.available and analysis.three_day.change_pct is not None:
        change = analysis.three_day.change_pct
        signals.append(TrendSignal(
            key="three_day", label="连续三日上升高度",
            reading=f"近 3 个交易日累计 {change:+.2f}%",
            direction=_direction(_scaled(change, SCALES["three_day"])),
            score=round(_scaled(change, SCALES["three_day"]), 4), weight=WEIGHTS["three_day"],
            note="三日急涨可能来自事件驱动，需要结合成交与公告核实。",
        ))
    up_days = sum(1 for index in range(len(closes) - 5, len(closes))
                  if index > 0 and closes[index] > closes[index - 1])
    consistency = (up_days - 2.5) / 2.5
    signals.append(TrendSignal(
        key="consistency", label="近五日方向一致性", reading=f"近 5 个交易日中 {up_days} 天上涨",
        direction=_direction(consistency),
        score=round(_clip(consistency), 4), weight=WEIGHTS["consistency"],
        note="方向一致性低说明涨跌交替，趋势判断可信度下降。",
    ))
    excess, common_days = _relative_strength(series, benchmark_series)
    if excess is not None and benchmark_name:
        analysis.relative_strength_pct = round(excess, 2)
        signals.append(TrendSignal(
            key="relative_strength", label="相对基准强弱",
            reading=f"近 5 个交易日相对 {benchmark_name} {excess:+.2f}%",
            direction=_direction(_scaled(excess, SCALES["relative_strength"])),
            score=round(_scaled(excess, SCALES["relative_strength"]), 4),
            weight=WEIGHTS["relative_strength"],
            note=f"按 {common_days} 个共同交易日对齐计算，不抬升单一标的的绝对收益预期。",
        ))
    elif benchmark_name and benchmark_series is not None:
        analysis.notes.append(f"与{benchmark_name}的共同交易日不足，未计算相对强弱。")

    total_weight = sum(signal.weight for signal in signals)
    composite = sum(signal.score * signal.weight for signal in signals) / total_weight if total_weight else 0.0
    score = round(50 + 50 * composite, 1)
    strong_up = any(signal.score >= CONFLICT_THRESHOLD for signal in signals)
    strong_down = any(signal.score <= -CONFLICT_THRESHOLD for signal in signals)
    neutralized = strong_up and strong_down
    verdict = "震荡" if neutralized else _verdict(score)
    agreement = 1 - (sum(abs(signal.score - composite) for signal in signals) / len(signals)) / 2
    data_factor = min(1.0, (len(series) - 5) / 15)
    confidence = round(max(0.0, min(1.0, 0.45 * data_factor + 0.55 * _clip(agreement, 1.0))), 2)

    analysis.score = score
    analysis.verdict = verdict
    analysis.neutralized = neutralized
    analysis.confidence = confidence
    analysis.signals = signals

    analysis.notes.append(
        f"参与计算 {len(series)} 个交易日（{series.points[0][0]} 至 {series.points[-1][0]}），"
        f"综合分 {score:.1f} 分；分数由 {len(signals)} 个信号加权得到，可逐项复算。"
    )
    if analysis.three_day.available:
        direction_word = "上涨" if (analysis.three_day.change_pct or 0) > 0 else "下跌" if (
            analysis.three_day.change_pct or 0) < 0 else "持平"
        analysis.notes.append(
            f"最近三个交易日{direction_word} {abs(analysis.three_day.change_pct or 0):.2f}%，"
            f"上升高度 {analysis.three_day.height:+.2f}（与行情同一计价单位）。"
        )
    if neutralized:
        analysis.notes.append("偏强与偏弱信号同时成立，按中和原则不给出单边结论，降级为震荡。")
    elif abs(composite) < 0.2:
        analysis.notes.append("各信号方向接近，综合分靠近中轴，趋势判断的区分度有限。")
    if len(series) < 20:
        analysis.notes.append(f"样本仅 {len(series)} 个交易日，判断属于短周期观察，不能外推到中长期。")
    return analysis
