"""
全局配置模块

对应 @Fibonacci 框架要点：
- 支持 Binance / OKX 等主流所（NautilusTrader 原生 integrations）
- 仓位纪律：头仓 2% + 分批补仓
- 模拟盘 / 实盘开关
"""
from dataclasses import dataclass, field


# ---------- 交易模式 ----------
class RunMode:
    BACKTEST = "backtest"   # 历史回测
    PAPER = "paper"         # 模拟盘（交易所 testnet）
    LIVE = "live"           # 实盘


@dataclass(frozen=True)
class FiboRuleConfig:
    """
    斐波那契规则参数（对应框架 3.入场逻辑 / 5.点位计算）
    全部可在参数优化中扫描
    """
    # 三档回撤比例（框架核心：0.382 第一多、0.5 第二多、0.618 最强抄底）
    level_1: float = 0.382
    level_2: float = 0.500
    level_3: float = 0.618
    # 扩展比例（1.618 预测牛市高点）
    extension: float = 1.618
    # 回踩不足 0.382 即可补仓（允许 1% 缓冲即视为触及）
    touch_tolerance: float = 0.01


@dataclass(frozen=True)
class BollConfig:
    """BOLL 共振参数（对应框架 5.BOLL 共振引擎）"""
    period: int = 20
    num_std: float = 2.0
    # 斐波位与 BOLL 下轨/中轨的接近容差（默认 0.5%）
    resonance_tolerance: float = 0.005


@dataclass(frozen=True)
class PositionConfig:
    """
    仓位管理（对应框架 6.纪律 + 3.入场仓位执行）
    头仓 2% → 继续回踩下方支撑补 2-3% → 浮盈 1-2 点追补仓
    """
    head_position_pct: float = 0.02          # 头仓 2%
    add_position_pct: float = 0.02           # 每次补仓 2%
    max_total_pct: float = 0.20              # 总仓位上限 20%（趋势单纪律）
    # 短空纪律：必须带止损
    short_stop_loss_pct: float = 0.03        # 空单止损 3%
    # 反弹浪中第二个斐波点视为强压力 → 多单减仓比例
    second_fib_reduce_factor: float = 0.5


@dataclass(frozen=True)
class StrategyRunConfig:
    """完整运行配置"""
    fibo: FiboRuleConfig = field(default_factory=FiboRuleConfig)
    boll: BollConfig = field(default_factory=BollConfig)
    position: PositionConfig = field(default_factory=PositionConfig)
    # 主策略：逢低多；辅助：突破 24-36h 前高可做超短空
    enable_short_when_breakout: bool = True
    breakout_lookback_hours: int = 30        # 24-36h 前高，取中值 30h
    # 多单不止损（只分批止盈）；空单必须带止损 —— 框架纪律
    long_use_stop_loss: bool = False
    short_use_stop_loss: bool = True
    # 日志开关
    verbose_log: bool = True


# 交易所相关（使用 NautilusTrader integrations）
@dataclass(frozen=True)
class VenueConfig:
    """
    - BINANCE / OKX 等大写为 NautilusTrader 的 Venue 标识
    - InstrumentId 格式：BTCUSDT.BINANCE / BTC-USDT.OKX
    """
    venue: str = "BINANCE"
    symbol: str = "BTCUSDT"
    base_currency: str = "USDT"
    starting_balance: str = "100_000 USDT"


DEFAULT_RUN_CONFIG = StrategyRunConfig()
DEFAULT_VENUE_CONFIG = VenueConfig()
