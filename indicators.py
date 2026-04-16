"""
多时间框架指标管理

对应 @Fibonacci 框架 2.多空判断框架：
    "优先级：周线 > 日线 > 4H/2H/1H"
    "关键信号：4H MACD 归零金叉、4-12 小时背离"
    "斐波整合：抓大波段格局中线趋势，就结合 MACD 看斐波列契"

本模块：
- 为每个时间框架维护 MACD / BOLL / EMA 三组指标
- 提供 4H MACD 归零金叉检测
- 提供 MACD 背离检测（2H+ 无背离 = 入场条件）
- 提供当前 BOLL 快照，给 boll_resonance 使用
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from nautilus_trader.indicators.averages import ExponentialMovingAverage
from nautilus_trader.indicators.trend import MovingAverageConvergenceDivergence
from nautilus_trader.indicators.volatility import BollingerBands
from nautilus_trader.model.data import Bar

from boll_resonance import BollSnapshot
from config import BollConfig


# 框架内全部关键时间框架
ALL_TIMEFRAMES = ("1H", "2H", "4H", "6H", "8H", "12H", "1D", "1W")


@dataclass
class TimeframeIndicators:
    """单一时间框架下的完整指标包"""
    timeframe: str
    macd: MovingAverageConvergenceDivergence
    boll: BollingerBands
    ema30: ExponentialMovingAverage
    # 历史队列用于背离/金叉检测
    macd_hist: deque           # 记录 MACD 线差值
    macd_signal_hist: deque    # 记录 signal 线
    price_hist: deque
    bar_count: int = 0

    @classmethod
    def create(cls, timeframe: str, boll_cfg: BollConfig) -> "TimeframeIndicators":
        return cls(
            timeframe=timeframe,
            macd=MovingAverageConvergenceDivergence(
                fast_period=12, slow_period=26
            ),
            boll=BollingerBands(period=boll_cfg.period, k=boll_cfg.num_std),
            ema30=ExponentialMovingAverage(30),
            macd_hist=deque(maxlen=200),
            macd_signal_hist=deque(maxlen=200),
            price_hist=deque(maxlen=200),
        )

    def update(self, bar: Bar) -> None:
        """
        NautilusTrader 会自动将 bar 推送给 indicators（已 register），
        这里只做历史队列维护 + 诊断字段。
        """
        if self.macd.initialized:
            self.macd_hist.append(float(self.macd.value))
        self.price_hist.append(float(bar.close))
        self.bar_count += 1

    @property
    def boll_snapshot(self) -> BollSnapshot | None:
        if not self.boll.initialized:
            return None
        return BollSnapshot(
            timeframe=self.timeframe,
            upper=float(self.boll.upper),
            middle=float(self.boll.middle),
            lower=float(self.boll.lower),
        )

    # -------- 专项信号 --------
    def is_macd_zero_golden_cross(self) -> bool:
        """
        4H MACD 归零金叉检测（框架关键信号）。
        定义：MACD 线刚从 0 附近自下而上穿越 0 → 视为归零金叉。
        实现：最近 2 根 MACD 值，前一根 < 0 且当前 > 0，同时绝对值都很小（接近 0）。
        """
        if len(self.macd_hist) < 2:
            return False
        prev, curr = self.macd_hist[-2], self.macd_hist[-1]
        # "归零"：前值在 0 附近（用价格 0.1% 为阈值近似）
        ref_price = self.price_hist[-1] if self.price_hist else 1.0
        near_zero_threshold = ref_price * 0.001
        is_near_zero = abs(prev) < near_zero_threshold
        return is_near_zero and prev < 0 <= curr

    def has_bearish_divergence(self, lookback: int = 30) -> bool:
        """
        MACD 顶背离检测：价格创新高 + MACD 未创新高 → 顶背离。
        对应框架："4-12 小时背离（仅短空参考）"
            入场条件之一："2H 及以上级别无背离"
        """
        if len(self.price_hist) < lookback or len(self.macd_hist) < lookback:
            return False
        prices = list(self.price_hist)[-lookback:]
        macds = list(self.macd_hist)[-lookback:]
        price_peak_idx = max(range(len(prices)), key=prices.__getitem__)
        macd_peak_idx = max(range(len(macds)), key=macds.__getitem__)
        # 价格新高在更晚的位置，但 MACD 峰值更早 → 顶背离
        recent_new_high = price_peak_idx >= lookback - 3
        return recent_new_high and macds[price_peak_idx] < macds[macd_peak_idx]


class MultiTimeframeIndicatorHub:
    """
    跨时间框架指标中枢。
    - 由 Strategy 在 on_start 中调用 register() 把每个时间框架的
      BarType 绑定到 NautilusTrader 的 indicator 引擎。
    - on_bar 根据 bar.bar_type 调度到对应 TimeframeIndicators.update()
    """

    def __init__(self, boll_cfg: BollConfig):
        self.boll_cfg = boll_cfg
        self.indicators: dict[str, TimeframeIndicators] = {
            tf: TimeframeIndicators.create(tf, boll_cfg) for tf in ALL_TIMEFRAMES
        }

    def tf_of_bar(self, bar: Bar) -> str | None:
        """把 Nautilus 的 BarSpecification 映射到 1H/4H/1D 等标签"""
        spec = bar.bar_type.spec
        step = spec.step
        agg = spec.aggregation.name      # MINUTE / HOUR / DAY / WEEK
        table = {
            ("HOUR", 1): "1H",
            ("HOUR", 2): "2H",
            ("HOUR", 4): "4H",
            ("HOUR", 6): "6H",
            ("HOUR", 8): "8H",
            ("HOUR", 12): "12H",
            ("DAY", 1): "1D",
            ("WEEK", 1): "1W",
        }
        return table.get((agg, step))

    def on_bar(self, bar: Bar) -> str | None:
        tf = self.tf_of_bar(bar)
        if tf is None or tf not in self.indicators:
            return None
        self.indicators[tf].update(bar)
        return tf

    def get(self, tf: str) -> TimeframeIndicators:
        return self.indicators[tf]

    def current_boll_snapshots(self) -> dict[str, BollSnapshot]:
        out: dict[str, BollSnapshot] = {}
        for tf, ind in self.indicators.items():
            snap = ind.boll_snapshot
            if snap is not None:
                out[tf] = snap
        return out

    def is_entry_divergence_free(self) -> bool:
        """
        入场条件（框架 3.主策略）：2H 及以上级别无背离。
        检查 2H / 4H / 1D / 1W 是否均无顶背离。
        """
        for tf in ("2H", "4H", "1D", "1W"):
            ind = self.indicators.get(tf)
            if ind and ind.has_bearish_divergence():
                return False
        return True
