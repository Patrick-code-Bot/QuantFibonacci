"""
回测主入口（NautilusTrader BacktestEngine 低级 API）

- 载入 CSV / Parquet 历史 K 线
- 注入主 1H bar + 其他时间框架（2H/4H/6H/8H/12H/1D/1W）
- 运行策略 + 输出净值曲线、胜率、最大回撤、盈亏比、斐波日志
- 参数优化：嵌套循环扫描（fibo 容差、BOLL 容差、头仓比例）
"""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import pandas as pd
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.persistence.wranglers import BarDataWrangler
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from config import DEFAULT_RUN_CONFIG, StrategyRunConfig, VenueConfig
from strategy import FibonacciStrategy, FibonacciStrategyConfig


# -----------------------------------------------------------------------------
# 工具
# -----------------------------------------------------------------------------
def _as_dict(run_cfg: StrategyRunConfig) -> dict:
    return {
        "fibo": asdict(run_cfg.fibo),
        "boll": asdict(run_cfg.boll),
        "position": asdict(run_cfg.position),
        "enable_short_when_breakout": run_cfg.enable_short_when_breakout,
        "breakout_lookback_hours": run_cfg.breakout_lookback_hours,
        "long_use_stop_loss": run_cfg.long_use_stop_loss,
        "short_use_stop_loss": run_cfg.short_use_stop_loss,
        "verbose_log": run_cfg.verbose_log,
    }


def load_bars_from_csv(
    csv_path: str,
    instrument,
    bar_type: BarType,
) -> list:
    """
    CSV 预期列：timestamp, open, high, low, close, volume
    timestamp 支持 ms / ISO 字符串
    """
    df = pd.read_csv(csv_path)
    if "timestamp" in df.columns:
        if df["timestamp"].dtype.kind in ("i", "f"):
            df.index = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        else:
            df.index = pd.to_datetime(df["timestamp"], utc=True)
    df = df[["open", "high", "low", "close", "volume"]]
    wrangler = BarDataWrangler(bar_type=bar_type, instrument=instrument)
    return wrangler.process(df)


def resample_bars(df_1h: pd.DataFrame, rule: str) -> pd.DataFrame:
    """把 1H OHLCV 重采样到 {rule}（pandas 规则：2H/4H/6H/8H/12H/1D/1W）"""
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return df_1h.resample(rule).agg(agg).dropna()


# -----------------------------------------------------------------------------
# 单次回测
# -----------------------------------------------------------------------------
def run_backtest(
    csv_1h_path: str,
    venue_cfg: VenueConfig,
    run_cfg: StrategyRunConfig,
    initial_swing_low: float,
    initial_swing_high: float,
    trader_id: str = "FIBO-001",
    log_level: str = "INFO",
) -> dict:
    """单次回测 —— 返回性能指标 dict"""
    engine = BacktestEngine(
        config=BacktestEngineConfig(trader_id=trader_id, log_level=log_level)
    )
    venue = Venue(venue_cfg.venue)
    engine.add_venue(
        venue=venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        base_currency=None,
        starting_balances=[
            Money(Decimal("100000"), USDT),
        ],
    )

    # 构造加密永续合约（BINANCE 风格）
    instrument_id = InstrumentId.from_str(f"{venue_cfg.symbol}.{venue_cfg.venue}")
    instrument = TestInstrumentProvider.btcusdt_binance()  # 内置 BTCUSDT.BINANCE
    engine.add_instrument(instrument)

    # 1H 主 bar
    primary_bar_type = BarType.from_str(f"{instrument.id}-1-HOUR-LAST-EXTERNAL")
    bars_1h = load_bars_from_csv(csv_1h_path, instrument, primary_bar_type)
    engine.add_data(bars_1h)

    # 派生其他时间框架
    df = pd.read_csv(csv_1h_path)
    if df["timestamp"].dtype.kind in ("i", "f"):
        df.index = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    else:
        df.index = pd.to_datetime(df["timestamp"], utc=True)
    df = df[["open", "high", "low", "close", "volume"]]

    extra_bar_types: list[BarType] = []
    for rule, label in (
        ("2H", "2-HOUR"),
        ("4H", "4-HOUR"),
        ("6H", "6-HOUR"),
        ("8H", "8-HOUR"),
        ("12H", "12-HOUR"),
        ("1D", "1-DAY"),
        ("1W", "1-WEEK"),
    ):
        df_tf = resample_bars(df, rule)
        if df_tf.empty:
            continue
        bt = BarType.from_str(f"{instrument.id}-{label}-LAST-EXTERNAL")
        wrangler = BarDataWrangler(bar_type=bt, instrument=instrument)
        bars_tf = wrangler.process(df_tf)
        engine.add_data(bars_tf)
        extra_bar_types.append(bt)

    # 策略配置
    strat_cfg = FibonacciStrategyConfig(
        instrument_id=instrument.id,
        primary_bar_type=primary_bar_type,
        extra_bar_types=tuple(extra_bar_types),
        initial_swing_low=initial_swing_low,
        initial_swing_high=initial_swing_high,
        run_config_dict=_as_dict(run_cfg),
    )
    strategy = FibonacciStrategy(strat_cfg)
    engine.add_strategy(strategy)

    engine.run()

    # -------- 取性能指标 --------
    report = {}
    account = engine.portfolio.account(venue)
    if account is not None:
        report["final_balance"] = str(account.balance_total())

    # 成交 / 持仓统计
    trades = engine.trader.generate_order_fills_report()
    positions = engine.trader.generate_positions_report()
    report["n_fills"] = len(trades)
    report["n_positions"] = len(positions)
    if not positions.empty and "realized_pnl" in positions.columns:
        wins = (positions["realized_pnl"].astype(float) > 0).sum()
        losses = (positions["realized_pnl"].astype(float) < 0).sum()
        total = wins + losses
        report["win_rate"] = round(wins / total, 4) if total else 0.0
        gross_win = positions.loc[
            positions["realized_pnl"].astype(float) > 0, "realized_pnl"
        ].astype(float).sum()
        gross_loss = -positions.loc[
            positions["realized_pnl"].astype(float) < 0, "realized_pnl"
        ].astype(float).sum()
        report["profit_factor"] = (
            round(gross_win / gross_loss, 3) if gross_loss > 0 else float("inf")
        )

    # 最大回撤（基于账户权益曲线）
    try:
        equity_curve = engine.trader.generate_account_report(venue)
        if not equity_curve.empty and "total" in equity_curve.columns:
            eq = equity_curve["total"].astype(float)
            dd = (eq / eq.cummax() - 1.0).min()
            report["max_drawdown"] = round(float(dd), 4)
    except Exception as e:
        report["max_drawdown_error"] = str(e)

    engine.dispose()
    return report


# -----------------------------------------------------------------------------
# 参数优化
# -----------------------------------------------------------------------------
def optimize(
    csv_1h_path: str,
    venue_cfg: VenueConfig,
    swing_low: float,
    swing_high: float,
    grid: dict | None = None,
) -> list[dict]:
    """
    参数网格扫描 —— 斐波容差 / BOLL 容差 / 头仓比例。
    返回按净值排序的结果列表。
    """
    from config import BollConfig, FiboRuleConfig, PositionConfig

    grid = grid or {
        "touch_tolerance": [0.005, 0.01, 0.02],
        "resonance_tolerance": [0.003, 0.005, 0.01],
        "head_position_pct": [0.01, 0.02, 0.03],
    }
    results = []
    for tt in grid["touch_tolerance"]:
        for rt in grid["resonance_tolerance"]:
            for hp in grid["head_position_pct"]:
                run_cfg = StrategyRunConfig(
                    fibo=FiboRuleConfig(touch_tolerance=tt),
                    boll=BollConfig(resonance_tolerance=rt),
                    position=PositionConfig(head_position_pct=hp),
                )
                try:
                    rep = run_backtest(
                        csv_1h_path=csv_1h_path,
                        venue_cfg=venue_cfg,
                        run_cfg=run_cfg,
                        initial_swing_low=swing_low,
                        initial_swing_high=swing_high,
                        trader_id=f"OPT-tt{tt}-rt{rt}-hp{hp}",
                        log_level="WARNING",
                    )
                    rep["params"] = {"tt": tt, "rt": rt, "hp": hp}
                    results.append(rep)
                except Exception as e:
                    results.append({"error": str(e), "params": {"tt": tt, "rt": rt, "hp": hp}})

    results.sort(
        key=lambda r: float(
            str(r.get("final_balance", "0")).split()[0].replace(",", "")
        )
        if "final_balance" in r
        else 0,
        reverse=True,
    )
    return results
