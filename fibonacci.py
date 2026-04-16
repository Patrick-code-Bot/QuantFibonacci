"""
斐波那契计算模块

对应 @Fibonacci 框架：
- 框架 3.入场逻辑 · 斐波那契回撤规则（核心引擎）
- 框架 5.点位计算与压力支撑 · 标准化流程
- 框架 6.规则：支撑已破 → 切换新波段

公式（严格对应作者原话）：
    涨幅差 = High - Low
    回撤位 = High - (涨幅差 × 比例)
三档比例：
    0.382 → 第一做多 / 补仓点（空单止盈1）
    0.5   → 第二做多点（空单止盈2）
    0.618 → 最强抄底 / 日线调整最低点锁定
额外：
    时间斐波：下跌天数 × 0.618 → 判断最后一跌
    扩展 1.618：预测下一轮牛市高点 = 最低点 + (上一轮底盘 × 1.618)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from config import FiboRuleConfig


# -----------------------------------------------------------------------------
# 基础函数（纯函数，便于单元测试与复用）
# -----------------------------------------------------------------------------
def calculate_fib_retracement(
    swing_low: float,
    swing_high: float,
    decimals: int = 2,
    rule: FiboRuleConfig | None = None,
) -> dict:
    """
    计算标准斐波回撤三档 + 最低点锁定。

    对应框架：
        "确定日线/大波段：起涨点（Swing Low）→ 最高点（Swing High）"
        "涨幅差 = High - Low"
        "回撤位 = High - (涨幅差 × 比例)"

    Returns:
        {
            0.382: 第一支撑（做多点1 / 空单止盈1）,
            0.5:   第二支撑（做多点2 / 空单止盈2）,
            0.618: 最强支撑（最强抄底点 / 日线最低点锁定）,
            "lowest_target": 0.618 位，作为锁定"日线调整最低点"
            "range": 涨幅差,
        }
    """
    if swing_high <= swing_low:
        raise ValueError(
            f"swing_high ({swing_high}) 必须大于 swing_low ({swing_low})，"
            f"请检查波段起涨点与最高点"
        )

    rule = rule or FiboRuleConfig()
    price_range = swing_high - swing_low

    def _calc(ratio: float) -> float:
        return round(swing_high - price_range * ratio, decimals)

    result = {
        rule.level_1: _calc(rule.level_1),
        rule.level_2: _calc(rule.level_2),
        rule.level_3: _calc(rule.level_3),
        "lowest_target": _calc(rule.level_3),   # 最强支撑锁定"日线最低点"
        "range": round(price_range, decimals),
        "swing_low": round(swing_low, decimals),
        "swing_high": round(swing_high, decimals),
    }
    return result


def calculate_time_fib(days: int) -> float:
    """
    时间斐波：下跌天数 × 0.618 → 判断"最后一跌"

    对应框架 5.额外：
        "下跌天数 × 0.618 → 判断最后一跌"
    用法：假设已下跌 N 天，预计最后一跌还剩 N × 0.382 或总共 N × 1.618 天内结束。
    这里直接返回 days × 0.618，由策略判定。
    """
    if days < 0:
        raise ValueError("days 必须 >= 0")
    return round(days * 0.618, 2)


def calculate_fib_extension(previous_low: float, previous_base: float) -> float:
    """
    斐波扩展 1.618：预测下一轮牛市高点

    对应框架：
        "预测下一轮牛市高点 = 最低点 + (上一轮底盘 × 1.618)"

    Args:
        previous_low: 当前周期的最低点（最低点）
        previous_base: 上一轮底盘（即上一轮的波段幅度 High - Low）

    Returns:
        下一轮高点预测值
    """
    if previous_base <= 0:
        raise ValueError("previous_base 必须 > 0（为上一轮波段的绝对高度）")
    return round(previous_low + previous_base * 1.618, 2)


# -----------------------------------------------------------------------------
# 波段容器 + 动态切换
# -----------------------------------------------------------------------------
@dataclass
class FibSwing:
    """
    单个波段（动态波段容器）。

    对应框架 6.纪律：
        "支撑已破 → 下一次抄底必须依据当前波段的斐波点进场"
        "盘面支撑点基本跟斐波点一一对应"
    """
    swing_low: float
    swing_high: float
    low_ts: Optional[datetime] = None
    high_ts: Optional[datetime] = None
    broken: bool = False                    # 0.618 被跌破 → 波段作废
    fib_levels: dict = field(default_factory=dict)

    def compute(self, rule: FiboRuleConfig | None = None) -> dict:
        self.fib_levels = calculate_fib_retracement(
            self.swing_low, self.swing_high, rule=rule
        )
        return self.fib_levels

    def is_price_at_level(
        self, price: float, ratio: float, tolerance: float = 0.01
    ) -> bool:
        """
        价格是否"触及"某斐波位（默认 ±1% 视为触及）。
        对应框架："回踩不足 0.382 即可补仓"——用 tolerance 缓冲。
        """
        level = self.fib_levels.get(ratio)
        if level is None:
            return False
        return abs(price - level) / level <= tolerance


class SwingManager:
    """
    波段管理器：动态切换"当前波段"。

    对应框架 6.规则：
        "支撑已破 → 下一次抄底必须依据当前波段的斐波点进场"
    逻辑：
        1. 若价格跌破 0.618 → 当前波段失效（broken）→ 切换新波段
        2. 新波段起点：当前最低价（low），终点：最近一次高点（high）
    """

    def __init__(self, rule: FiboRuleConfig | None = None):
        self.rule = rule or FiboRuleConfig()
        self.current: Optional[FibSwing] = None
        self.history: list[FibSwing] = []

    def set_swing(
        self,
        swing_low: float,
        swing_high: float,
        low_ts: datetime | None = None,
        high_ts: datetime | None = None,
    ) -> FibSwing:
        swing = FibSwing(
            swing_low=swing_low,
            swing_high=swing_high,
            low_ts=low_ts,
            high_ts=high_ts,
        )
        swing.compute(self.rule)
        if self.current and not self.current.broken:
            self.history.append(self.current)
        self.current = swing
        return swing

    def on_price_update(
        self,
        price: float,
        timestamp: datetime | None = None,
    ) -> Optional[FibSwing]:
        """
        每根新 bar 调用一次。若 0.618 被跌破则标记波段已破。
        返回：若发生波段切换，返回新的当前波段；否则 None。
        """
        if self.current is None:
            return None
        level_618 = self.current.fib_levels.get(self.rule.level_3)
        if level_618 is None:
            return None
        if price < level_618 * (1 - self.rule.touch_tolerance):
            self.current.broken = True
            # 支撑已破：策略应当以新的 swing_low = price 构造下一个波段，
            # 新 swing_high 需由策略侧传入（通常是最近 N 根 bar 的最高价）
            # 这里只做标记，切换动作留给策略层触发 set_swing()
        return self.current

    def get_active_levels(self) -> dict | None:
        if self.current and not self.current.broken:
            return self.current.fib_levels
        return None
