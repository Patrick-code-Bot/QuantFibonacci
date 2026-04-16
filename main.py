"""
主程序入口

功能：
1) 演示斐波那契计算（用帖文提供的两个经典波段）
    - BTC 64918 → 73773（上涨波段）
    - BTC 75998 → 65569（下跌波段，计算反弹回撤压力）
2) 回测示例（需要本地 CSV 数据）
3) 实盘/模拟盘切换入口

运行：
    python main.py demo              # 仅演示斐波计算
    python main.py backtest  data.csv 64918 73773
    python main.py optimize  data.csv 64918 73773
    python main.py paper             # 模拟盘（需配置交易所 API key）
    python main.py live              # 实盘（需配置交易所 API key）
"""
from __future__ import annotations

import sys
from pprint import pprint

from config import DEFAULT_RUN_CONFIG, DEFAULT_VENUE_CONFIG, RunMode
from fibonacci import (
    calculate_fib_extension,
    calculate_fib_retracement,
    calculate_time_fib,
)


# -----------------------------------------------------------------------------
# 演示：帖文两个经典波段
# -----------------------------------------------------------------------------
def demo_fibonacci() -> None:
    print("=" * 70)
    print("波段 1：BTC 上涨波段 64918 → 73773")
    print("=" * 70)
    up = calculate_fib_retracement(64918, 73773)
    # 对应含义：
    #   0.382 ≈ 70390 → 第一做多点（回踩补仓）
    #   0.5   ≈ 69345 → 第二做多点
    #   0.618 ≈ 68300 → 最强抄底点 / 日线调整最低点锁定
    pprint(up)

    print()
    print("=" * 70)
    print("波段 2：BTC 下跌波段 75998 → 65569（反向用法：反弹压力）")
    print("=" * 70)
    # 下跌波段反算：把 75998 视为 swing_high，65569 视为 swing_low
    # 反弹回撤位 = 压力位（空单止盈点）
    down = calculate_fib_retracement(65569, 75998)
    # 对应含义：
    #   0.382 ≈ 72014 → 空单止盈1
    #   0.5   ≈ 70784 → 空单止盈2（第二个斐波点=强压力）
    #   0.618 ≈ 69555 → 最强反弹压力
    pprint(down)

    print()
    print("=" * 70)
    print("时间斐波：假设已下跌 15 天")
    print("=" * 70)
    print(f"15 × 0.618 = {calculate_time_fib(15)} 天（预测'最后一跌'时间窗）")

    print()
    print("=" * 70)
    print("斐波扩展 1.618：预测下一轮牛市高点")
    print("  假设当前最低 = 48000，上一轮波段底盘 = (73773 - 15476) = 58297")
    print("=" * 70)
    pred = calculate_fib_extension(48000, 58297)
    print(f"预测下一轮高点 = {pred}")


# -----------------------------------------------------------------------------
# 回测入口
# -----------------------------------------------------------------------------
def cli_backtest(argv: list[str]) -> None:
    if len(argv) < 4:
        print("用法：python main.py backtest <csv_path> <swing_low> <swing_high>")
        sys.exit(1)
    csv_path = argv[1]
    swing_low = float(argv[2])
    swing_high = float(argv[3])
    from backtest import run_backtest  # 延迟导入（避免 demo 也加载 Nautilus）

    rep = run_backtest(
        csv_1h_path=csv_path,
        venue_cfg=DEFAULT_VENUE_CONFIG,
        run_cfg=DEFAULT_RUN_CONFIG,
        initial_swing_low=swing_low,
        initial_swing_high=swing_high,
    )
    print("\n===== 回测结果 =====")
    pprint(rep)


def cli_optimize(argv: list[str]) -> None:
    if len(argv) < 4:
        print("用法：python main.py optimize <csv_path> <swing_low> <swing_high>")
        sys.exit(1)
    csv_path = argv[1]
    swing_low = float(argv[2])
    swing_high = float(argv[3])
    from backtest import optimize

    results = optimize(
        csv_1h_path=csv_path,
        venue_cfg=DEFAULT_VENUE_CONFIG,
        swing_low=swing_low,
        swing_high=swing_high,
    )
    print("\n===== 参数优化（按 final_balance 降序 Top 10）=====")
    for r in results[:10]:
        pprint(r)


# -----------------------------------------------------------------------------
# 实盘 / 模拟盘入口（使用 NautilusTrader TradingNode）
# -----------------------------------------------------------------------------
def cli_live(mode: str) -> None:
    """
    mode = 'paper' → testnet
    mode = 'live'  → 主网
    必须配置环境变量：
        BINANCE_API_KEY / BINANCE_API_SECRET
    """
    import os
    from nautilus_trader.adapters.binance.config import (
        BinanceDataClientConfig,
        BinanceExecClientConfig,
    )
    from nautilus_trader.adapters.binance.factories import (
        BinanceLiveDataClientFactory,
        BinanceLiveExecClientFactory,
    )
    from nautilus_trader.config import TradingNodeConfig
    from nautilus_trader.live.node import TradingNode
    from nautilus_trader.model.data import BarType
    from nautilus_trader.model.identifiers import InstrumentId

    from strategy import FibonacciStrategy, FibonacciStrategyConfig
    from backtest import _as_dict

    is_testnet = mode == RunMode.PAPER
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")

    symbol = os.getenv("FIBO_SYMBOL", "BTCUSDT")
    instrument_id = InstrumentId.from_str(f"{symbol}.BINANCE")
    primary_bar = BarType.from_str(f"{instrument_id}-1-HOUR-LAST-EXTERNAL")
    extras = tuple(
        BarType.from_str(f"{instrument_id}-{lbl}-LAST-EXTERNAL")
        for lbl in ("2-HOUR", "4-HOUR", "6-HOUR", "8-HOUR", "12-HOUR", "1-DAY", "1-WEEK")
    )

    # 自动识别当前波段（Swing Low → Swing High），无需手动填
    from swing_detector import auto_detect_swing
    swing = auto_detect_swing(
        symbol=symbol, interval="4h", window=5, lookback_bars=300,
        testnet=is_testnet,
    )
    print(f"[Auto Swing] {swing.summary()}")

    strat_cfg = FibonacciStrategyConfig(
        instrument_id=instrument_id,
        primary_bar_type=primary_bar,
        extra_bar_types=extras,
        initial_swing_low=swing.swing_low,
        initial_swing_high=swing.swing_high,
        run_config_dict=_as_dict(DEFAULT_RUN_CONFIG),
    )

    config_node = TradingNodeConfig(
        trader_id="FIBO-LIVE-001",
        data_clients={
            "BINANCE": BinanceDataClientConfig(
                api_key=api_key,
                api_secret=api_secret,
                account_type="SPOT",
                testnet=is_testnet,
            )
        },
        exec_clients={
            "BINANCE": BinanceExecClientConfig(
                api_key=api_key,
                api_secret=api_secret,
                account_type="SPOT",
                testnet=is_testnet,
            )
        },
    )

    node = TradingNode(config=config_node)
    node.add_strategy(FibonacciStrategy(strat_cfg))
    node.add_data_client_factory("BINANCE", BinanceLiveDataClientFactory)
    node.add_exec_client_factory("BINANCE", BinanceLiveExecClientFactory)
    node.build()
    try:
        node.run()
    finally:
        node.dispose()


# -----------------------------------------------------------------------------
# CLI 分发
# -----------------------------------------------------------------------------
def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "demo":
        demo_fibonacci()
    elif cmd == "backtest":
        cli_backtest(sys.argv[1:])
    elif cmd == "optimize":
        cli_optimize(sys.argv[1:])
    elif cmd in (RunMode.PAPER, RunMode.LIVE):
        cli_live(cmd)
    else:
        print(f"未知命令：{cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
