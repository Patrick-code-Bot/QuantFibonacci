"""
BOLL 共振验证模块

对应 @Fibonacci 框架 5.点位计算与压力支撑 · BOLL 共振引擎：
    "斐波位需与对应时间框架 BOLL（6H/12H/1D/8H）下轨或中轨接近"
    "支撑点基本跟斐波点一一对应"

该模块将斐波位与不同时间框架下的 BOLL 轨道做接近度校验：
- 若斐波位落在 BOLL 下轨 ±tolerance 内 → 强共振（最高优先）
- 若斐波位落在 BOLL 中轨 ±tolerance 内 → 弱共振
- 仅斐波位独立出现 → 不共振（降权）
"""
from dataclasses import dataclass
from typing import Literal

from config import BollConfig


# 框架原文点名的时间框架：6H / 8H / 12H / 1D
RESONANCE_TIMEFRAMES = ("6H", "8H", "12H", "1D")


@dataclass(frozen=True)
class BollSnapshot:
    """某时间框架的 BOLL 当前数值快照"""
    timeframe: str
    upper: float
    middle: float
    lower: float


@dataclass(frozen=True)
class ResonanceResult:
    passed: bool
    timeframe: str
    level: Literal["strong", "weak", "none"]   # 强/弱/无共振
    matched_band: Literal["lower", "middle", "none"]
    distance_pct: float                        # 斐波位与轨道的相对距离


def check_boll_resonance(
    fib_level: float,
    timeframe: str,
    current_boll_data: BollSnapshot,
    tolerance: float = 0.005,
) -> ResonanceResult:
    """
    单一时间框架的 BOLL 共振检查。

    Args:
        fib_level: 斐波位价格
        timeframe: 时间框架标签（仅用于日志）
        current_boll_data: 该时间框架的 BOLL 数据
        tolerance: 相对容差，默认 ±0.5%

    Returns:
        ResonanceResult
    """
    if fib_level <= 0:
        return ResonanceResult(False, timeframe, "none", "none", float("inf"))

    dist_lower = abs(fib_level - current_boll_data.lower) / fib_level
    dist_middle = abs(fib_level - current_boll_data.middle) / fib_level

    if dist_lower <= tolerance:
        # 斐波位 ≈ BOLL 下轨 → 强共振（最强抄底信号）
        return ResonanceResult(True, timeframe, "strong", "lower", dist_lower)
    if dist_middle <= tolerance:
        # 斐波位 ≈ BOLL 中轨 → 弱共振
        return ResonanceResult(True, timeframe, "weak", "middle", dist_middle)

    return ResonanceResult(
        False, timeframe, "none", "none", min(dist_lower, dist_middle)
    )


def check_multi_timeframe_resonance(
    fib_level: float,
    boll_snapshots: dict[str, BollSnapshot],
    cfg: BollConfig | None = None,
) -> dict[str, ResonanceResult]:
    """
    对所有关键时间框架（6H/8H/12H/1D）同时校验共振。

    对应框架："斐波位需与对应时间框架 BOLL（6H/12H/1D/8H）下轨或中轨接近"
    只要 ≥1 个时间框架出现强共振即视为通过。
    """
    cfg = cfg or BollConfig()
    results: dict[str, ResonanceResult] = {}
    for tf in RESONANCE_TIMEFRAMES:
        if tf not in boll_snapshots:
            continue
        results[tf] = check_boll_resonance(
            fib_level, tf, boll_snapshots[tf], cfg.resonance_tolerance
        )
    return results


def aggregate_resonance(results: dict[str, ResonanceResult]) -> ResonanceResult:
    """
    汇总多时间框架结果 → 得出最终共振结论：
        出现任一 strong → 整体 strong
        没有 strong 但有 weak → 整体 weak
        都无 → none
    """
    best: ResonanceResult | None = None
    for r in results.values():
        if r.level == "strong":
            return r   # 一旦出现强共振立即返回
        if r.level == "weak" and (best is None or best.level == "none"):
            best = r
    if best is not None:
        return best
    return ResonanceResult(False, "ALL", "none", "none", float("inf"))
