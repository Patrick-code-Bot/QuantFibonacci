"""
自动波段识别

对应 @Fibonacci 框架：
    "确定日线/大波段：起涨点（Swing Low）→ 最高点（Swing High）"

算法：Pivot High/Low 检测（轴点法）
    一根 bar 的 high 若是它两侧 ±window 根 bar 的最高 → Pivot High
    一根 bar 的 low  若是它两侧 ±window 根 bar 的最低 → Pivot Low
然后取"最近的 Swing Low + 其后最近的 Swing High"作为当前活跃波段，
直接喂给 SwingManager。
"""
from __future__ import annotations

import urllib.parse
import urllib.request
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd


# -----------------------------------------------------------------------------
# Pivot 检测
# -----------------------------------------------------------------------------
def find_pivots(
    highs: list[float],
    lows: list[float],
    window: int = 5,
) -> list[tuple[int, str, float]]:
    """
    返回 [(index, 'H' | 'L', price), ...]
    window = 5 表示左右各 5 根 bar 做确认（共 11 根窗口）
    """
    pivots: list[tuple[int, str, float]] = []
    n = len(highs)
    for i in range(window, n - window):
        h_window = highs[i - window : i + window + 1]
        l_window = lows[i - window : i + window + 1]
        if highs[i] == max(h_window):
            pivots.append((i, "H", highs[i]))
        if lows[i] == min(l_window):
            pivots.append((i, "L", lows[i]))
    return pivots


@dataclass
class DetectedSwing:
    swing_low: float
    swing_high: float
    swing_low_ts: datetime
    swing_high_ts: datetime
    swing_low_idx: int
    swing_high_idx: int

    def summary(self) -> str:
        return (
            f"Swing Low {self.swing_low:.2f} @ {self.swing_low_ts} → "
            f"Swing High {self.swing_high:.2f} @ {self.swing_high_ts}"
        )


def detect_latest_swing(
    df: pd.DataFrame,
    window: int = 5,
) -> Optional[DetectedSwing]:
    """
    从 OHLC DataFrame 识别"当前活跃波段"。

    规则：
        1. 找到所有 pivot
        2. 取最近的 Pivot High
        3. 在它之前找最近的 Pivot Low
        4. 该 Pivot Low → Pivot High 即为当前斐波回撤计算波段
    返回 None 表示数据不足。
    """
    if len(df) < window * 2 + 1:
        return None

    highs = df["high"].astype(float).tolist()
    lows = df["low"].astype(float).tolist()
    pivots = find_pivots(highs, lows, window)
    if not pivots:
        return None

    pivots.sort(key=lambda p: p[0])
    # 最近的 Pivot High
    last_high = next((p for p in reversed(pivots) if p[1] == "H"), None)
    if last_high is None:
        return None
    # 在该 High 之前的最近 Pivot Low
    prior_lows = [p for p in pivots if p[1] == "L" and p[0] < last_high[0]]
    if not prior_lows:
        return None
    last_low = prior_lows[-1]

    return DetectedSwing(
        swing_low=last_low[2],
        swing_high=last_high[2],
        swing_low_idx=last_low[0],
        swing_high_idx=last_high[0],
        swing_low_ts=df.index[last_low[0]].to_pydatetime(),
        swing_high_ts=df.index[last_high[0]].to_pydatetime(),
    )


# -----------------------------------------------------------------------------
# Binance REST 拉 K 线（实盘启动前自动识别波段）
# 使用公共接口，不需要 API key
# -----------------------------------------------------------------------------
BINANCE_REST = "https://api.binance.com/api/v3/klines"
BINANCE_TESTNET_REST = "https://testnet.binance.vision/api/v3/klines"


def fetch_binance_klines(
    symbol: str = "BTCUSDT",
    interval: str = "1h",
    limit: int = 300,
    testnet: bool = False,
) -> pd.DataFrame:
    """
    拉 Binance 1H K 线（默认 300 根 ≈ 12.5 天）。
    返回 DataFrame(index=UTC时间, columns=[open, high, low, close, volume])
    """
    base = BINANCE_TESTNET_REST if testnet else BINANCE_REST
    params = urllib.parse.urlencode({
        "symbol": symbol,
        "interval": interval,
        "limit": min(limit, 1000),
    })
    url = f"{base}?{params}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    df = pd.DataFrame(
        data,
        columns=[
            "ts", "open", "high", "low", "close", "volume",
            "_close_ts", "_qav", "_trades", "_tbbav", "_tbqav", "_ignore",
        ],
    )
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    return df[["open", "high", "low", "close", "volume"]]


def auto_detect_swing(
    symbol: str = "BTCUSDT",
    interval: str = "4h",
    window: int = 5,
    lookback_bars: int = 300,
    testnet: bool = False,
) -> DetectedSwing:
    """
    一键完成：拉行情 + 识别波段，供 main.py live 模式启动调用。

    默认使用 4H K 线（符合框架"抓大波段格局中线趋势"的思路），
    300 根 4H ≈ 50 天，足够覆盖一次中线波段。
    """
    df = fetch_binance_klines(
        symbol=symbol, interval=interval, limit=lookback_bars, testnet=testnet
    )
    swing = detect_latest_swing(df, window=window)
    if swing is None:
        raise RuntimeError(
            f"无法识别 {symbol} 当前波段（数据 {len(df)} 根，window={window}），"
            "请检查网络 / 增大 lookback_bars"
        )
    return swing
