"""
斐波那契量化主策略（NautilusTrader Strategy）

对应 @Fibonacci 完整框架：
    1.整体哲学：逢低做多为主、不反弹就睡觉、趋势单
    2.多时间框架优先：周/日 > 4H/2H/1H
    3.入场：
        - 主：2H+ 无背离 + 回踩斐波位（0.382/0.5/0.618）+ BOLL 共振 → 分批多
        - 辅：突破 24-36h 前高 → 短空
    4.出场：
        - 多单不止损，只分批止盈（BOLL 中轨 / EMA30 / 斐波位）
        - 空单严格止损 + 斐波位分批止盈
        - 反弹浪第二个斐波点 → 强压力（降低多单仓位）
    5.仓位：头仓 2% + 分批补仓 + 总仓位上限
    6.纪律：支撑跌破 → 切换新波段
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict
from decimal import Decimal

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

from boll_resonance import (
    aggregate_resonance,
    check_multi_timeframe_resonance,
)
from config import (
    BollConfig,
    FiboRuleConfig,
    PositionConfig,
    StrategyRunConfig,
)
from fibonacci import SwingManager
from indicators import ALL_TIMEFRAMES, MultiTimeframeIndicatorHub
from notifier import get_notifier


# -----------------------------------------------------------------------------
# NautilusTrader StrategyConfig（必须 frozen）
# -----------------------------------------------------------------------------
class FibonacciStrategyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    # 主 bar（用于驱动价格事件 + 触发风控；建议 1H）
    primary_bar_type: BarType
    # 斐波初始波段：由外部传入（第一次启动时的 swing_low/high）
    initial_swing_low: float
    initial_swing_high: float
    # 其他所有时间框架的 BarType（6H/8H/12H/1D/2H/4H/1W）
    extra_bar_types: tuple[BarType, ...] = ()
    # 全部策略开关/参数序列化后传入（NautilusTrader config 要求可序列化）
    run_config_dict: dict | None = None


# -----------------------------------------------------------------------------
# 主策略
# -----------------------------------------------------------------------------
class FibonacciStrategy(Strategy):
    """
    一共维护三层状态：
        1) 多时间框架指标 hub（MACD/BOLL/EMA 全套）
        2) 波段管理器（SwingManager） —— 纪律：支撑跌破就切换
        3) 仓位分批记账 —— 头仓/补仓/止盈
    """

    def __init__(self, config: FibonacciStrategyConfig) -> None:
        super().__init__(config)

        # 组装纯 Python 配置对象
        run_dict = config.run_config_dict or {}
        self.run_cfg = self._build_run_cfg(run_dict)

        # 波段管理器（动态切换引擎）
        self.swing_mgr = SwingManager(rule=self.run_cfg.fibo)
        self.swing_mgr.set_swing(
            swing_low=config.initial_swing_low,
            swing_high=config.initial_swing_high,
        )

        # 多时间框架指标中枢
        self.hub = MultiTimeframeIndicatorHub(self.run_cfg.boll)

        # 突破检测滑窗（记录 24-36h 前高，默认 30 根 1H bar）
        self.recent_highs: deque[float] = deque(
            maxlen=self.run_cfg.breakout_lookback_hours
        )

        # 分批仓位账本：记录每次开/补仓金额比例
        self.position_ledger: list[dict] = []
        self.total_position_pct: float = 0.0

        # 已触发的斐波位（用于避免重复加仓）
        self._hit_levels_this_swing: set[float] = set()

        # Telegram 告警（无环境变量则静默降级）
        self.notifier = get_notifier()

    # -------------------- 生命周期 --------------------
    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            self.log.error(f"找不到 instrument {self.config.instrument_id}")
            return

        # 注册主 bar 指标（1H 作为事件驱动）
        self._register_indicators(self.config.primary_bar_type)
        self.subscribe_bars(self.config.primary_bar_type)

        # 注册其他时间框架
        for bt in self.config.extra_bar_types:
            self._register_indicators(bt)
            self.subscribe_bars(bt)

        self._log_swing("策略启动 —— 初始波段")
        # Telegram 启动通知
        swing = self.swing_mgr.current
        if swing is not None:
            self.notifier.on_start(
                symbol=str(self.config.instrument_id),
                swing_low=swing.swing_low,
                swing_high=swing.swing_high,
                levels=swing.fib_levels,
                mode="live",
            )

    def _register_indicators(self, bar_type: BarType) -> None:
        # 把 NautilusTrader 指标对象注册给对应 BarType，由引擎自动喂数据
        # 注意：我们每个时间框架一套，按 bar_type 分流 —— 交给 hub.tf_of_bar
        # 为保证注册覆盖，这里对 hub 里所有 tf 都尝试 register（仅当 BarType 匹配时有效）
        # 简化做法：NautilusTrader 允许把任何 indicator 注册给任何 bar_type，
        # 但我们按 bar_type.step+agg 手动分流。这里为每个 tf 单独注册一份。
        for tf in ALL_TIMEFRAMES:
            ind = self.hub.indicators[tf]
            # 只为匹配的 tf 注册（避免把 1D 的 MACD 用 1H 的数据喂）
            if self.hub.tf_of_bar_type(bar_type) == tf:
                self.register_indicator_for_bars(bar_type, ind.macd)
                self.register_indicator_for_bars(bar_type, ind.boll)
                self.register_indicator_for_bars(bar_type, ind.ema30)

    def on_stop(self) -> None:
        self.cancel_all_orders(self.config.instrument_id)
        self.close_all_positions(self.config.instrument_id)

    def on_position_closed(self, event) -> None:
        """NautilusTrader 回调：仓位平仓时推送 PnL"""
        try:
            pnl = float(event.realized_pnl) if event.realized_pnl else 0.0
            self.notifier.on_position_closed(pnl=pnl)
        except Exception as e:
            self.log.warning(f"on_position_closed notify err: {e}")

    # -------------------- 主循环 --------------------
    def on_bar(self, bar: Bar) -> None:
        tf = self.hub.on_bar(bar)
        if tf is None:
            return

        # 只在主 bar（1H）上做交易决策，其他只更新指标
        if bar.bar_type != self.config.primary_bar_type:
            return

        price = float(bar.close)
        self.recent_highs.append(float(bar.high))

        # 波段纪律：若跌破 0.618 → 自动作废 + 切换
        self.swing_mgr.on_price_update(price)
        if self.swing_mgr.current and self.swing_mgr.current.broken:
            self._switch_swing_on_break(price)

        levels = self.swing_mgr.get_active_levels()
        if not levels:
            return

        # 仅当 2H+ 无背离才考虑入场
        can_enter_long = self.hub.is_entry_divergence_free()

        # 决策：多单（主策略）
        if can_enter_long:
            self._try_long_entry(price, levels)

        # 决策：辅助空单（突破前高）
        if self.run_cfg.enable_short_when_breakout:
            self._try_short_breakout(price)

        # 多单分批止盈（不止损）
        self._manage_long_take_profit(price, levels)

    # -------------------- 多单逻辑 --------------------
    def _try_long_entry(self, price: float, levels: dict) -> None:
        """
        入场条件：
          1) 回踩到 0.382 / 0.5 / 0.618（优先顺序一致）
          2) BOLL 共振（6H/8H/12H/1D 任一时间框架的下轨/中轨接近）
          3) 总仓位未超上限
        """
        if self.total_position_pct >= self.run_cfg.position.max_total_pct:
            return

        snapshots = self.hub.current_boll_snapshots()
        fibo_cfg: FiboRuleConfig = self.run_cfg.fibo
        tolerance = fibo_cfg.touch_tolerance

        for ratio in (fibo_cfg.level_1, fibo_cfg.level_2, fibo_cfg.level_3):
            level_price = levels[ratio]
            # 1) 触及判定
            if abs(price - level_price) / level_price > tolerance:
                continue
            if ratio in self._hit_levels_this_swing:
                continue
            # 2) BOLL 共振
            res_map = check_multi_timeframe_resonance(
                level_price, snapshots, self.run_cfg.boll
            )
            final = aggregate_resonance(res_map)

            # 3) 仓位：头仓 or 补仓
            is_head = not self.position_ledger
            pct = (
                self.run_cfg.position.head_position_pct
                if is_head
                else self.run_cfg.position.add_position_pct
            )
            # 反弹浪第二个斐波点视为强压力 → 降低仓位
            if ratio == fibo_cfg.level_2 and final.level != "strong":
                pct *= self.run_cfg.position.second_fib_reduce_factor

            if not final.passed:
                self.log.info(
                    f"[跳过] 斐波 {ratio}@{level_price} 无 BOLL 共振 "
                    f"(最近距离 {final.distance_pct:.3%})"
                )
                continue

            self._submit_long(pct, reason=f"fib={ratio} reso={final.level}@{final.timeframe}")
            self._hit_levels_this_swing.add(ratio)
            break  # 一根 bar 只触发一次

    def _submit_long(self, pct: float, reason: str) -> None:
        """按账户权益比例下多单（市价）"""
        qty = self._qty_from_pct(pct)
        if qty is None or qty <= 0:
            return
        order = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=OrderSide.BUY,
            quantity=self.instrument.make_qty(qty),
            time_in_force=TimeInForce.GTC,
        )
        self.submit_order(order)
        self.position_ledger.append({"side": "long", "pct": pct, "reason": reason})
        self.total_position_pct += pct
        self.log.info(
            f"[开多] pct={pct:.2%} total={self.total_position_pct:.2%} "
            f"qty={qty} reason={reason}"
        )
        # Telegram 推送
        last_bars = self.cache.bars(self.config.primary_bar_type)
        last_price = float(last_bars[0].close) if last_bars else 0.0
        self.notifier.on_long_open(
            price=last_price, pct=pct,
            total_pct=self.total_position_pct, reason=reason,
        )

    def _manage_long_take_profit(self, price: float, levels: dict) -> None:
        """
        多单分批止盈（框架 4.出场）：
            - 醒来涨 → 止盈一点
            - 压力位：BOLL 中轨 / EMA30 / 斐波位
        实现：若当前净多仓 > 0 且 price 触及以下任一 → 部分止盈 20%
            a) 当前波段的 0.382/0.5/0.618 上方反弹至原波段高 × (1 - ratio)
            b) 1D BOLL 中轨
            c) 1D EMA30
        """
        if self.portfolio.is_flat(self.config.instrument_id):
            return
        if not self.portfolio.is_net_long(self.config.instrument_id):
            return

        daily = self.hub.indicators.get("1D")
        targets: list[tuple[str, float]] = []
        if daily and daily.boll.initialized:
            targets.append(("1D BOLL 中轨", float(daily.boll.middle)))
        if daily and daily.ema30.initialized:
            targets.append(("1D EMA30", float(daily.ema30.value)))
        # 斐波位向上 —— 反弹浪中第二个斐波点 = level_2，视为强压力
        second_fib = levels.get(self.run_cfg.fibo.level_2)
        if second_fib:
            targets.append(("斐波第二点（强压力）", second_fib))
        swing_high = levels.get("swing_high")
        if swing_high:
            targets.append(("波段高点", swing_high))

        for name, tp_price in targets:
            if price >= tp_price and tp_price > 0:
                self._partial_close_long(0.2, reason=f"止盈@{name} {tp_price}")
                break  # 每根 bar 只执行一次

    def _partial_close_long(self, fraction: float, reason: str) -> None:
        pos_list = self.cache.positions_open(instrument_id=self.config.instrument_id)
        if not pos_list:
            return
        pos = pos_list[0]
        close_qty = Decimal(str(float(pos.quantity) * fraction))
        if close_qty <= 0:
            return
        order = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=OrderSide.SELL,
            quantity=self.instrument.make_qty(close_qty),
            time_in_force=TimeInForce.GTC,
        )
        self.submit_order(order)
        self.log.info(f"[止盈] fraction={fraction:.0%} reason={reason}")
        last_bars = self.cache.bars(self.config.primary_bar_type)
        last_price = float(last_bars[0].close) if last_bars else 0.0
        self.notifier.on_long_take_profit(
            price=last_price, fraction=fraction, reason=reason
        )

    # -------------------- 空单（辅助）--------------------
    def _try_short_breakout(self, price: float) -> None:
        """
        突破 24-36h 前高 → 新压力位附近做超短空（必须带止损）。
        """
        if len(self.recent_highs) < self.recent_highs.maxlen:
            return
        prior_high = max(list(self.recent_highs)[:-1])  # 排除当前 bar
        if price > prior_high * 1.001:
            qty = self._qty_from_pct(self.run_cfg.position.head_position_pct)
            if qty is None or qty <= 0:
                return
            entry = self.order_factory.market(
                instrument_id=self.config.instrument_id,
                order_side=OrderSide.SELL,
                quantity=self.instrument.make_qty(qty),
            )
            self.submit_order(entry)
            # 严格止损（框架纪律：空单必须带止损）
            stop_price = price * (1 + self.run_cfg.position.short_stop_loss_pct)
            stop = self.order_factory.stop_market(
                instrument_id=self.config.instrument_id,
                order_side=OrderSide.BUY,
                quantity=self.instrument.make_qty(qty),
                trigger_price=self.instrument.make_price(stop_price),
            )
            self.submit_order(stop)
            self.log.info(
                f"[开空] 突破前高 prior={prior_high:.2f} entry={price:.2f} "
                f"stop={stop_price:.2f}"
            )
            self.notifier.on_short_open(
                price=price, stop=stop_price,
                reason=f"突破 {self.run_cfg.breakout_lookback_hours}h 前高 {prior_high:.2f}",
            )

    # -------------------- 波段纪律 --------------------
    def _switch_swing_on_break(self, price: float) -> None:
        """
        支撑已破 → 切换到新波段。
        新 swing_low = 当前价；新 swing_high = 最近 200 根 bar 最高价。
        """
        one_h = self.hub.indicators.get("1H")
        if one_h is None or not one_h.price_hist:
            return
        new_high = max(one_h.price_hist)
        if new_high <= price:
            return  # 防御：没有更高的参考
        self.swing_mgr.set_swing(swing_low=price, swing_high=new_high)
        self._hit_levels_this_swing.clear()
        self._log_swing("波段已切换（支撑跌破）")
        new_swing = self.swing_mgr.current
        if new_swing is not None:
            self.notifier.on_swing_switch(
                new_low=new_swing.swing_low,
                new_high=new_swing.swing_high,
                levels=new_swing.fib_levels,
            )

    # -------------------- 工具 --------------------
    def _qty_from_pct(self, pct: float) -> Decimal | None:
        """根据账户余额 × pct / 当前价 → 下单张数"""
        account = self.portfolio.account(self.instrument.venue)
        if account is None:
            return None
        balances = account.balances_total()
        if not balances:
            return None
        # 取第一个报价币种的余额（如 USDT）
        bal = list(balances.values())[0].as_decimal()
        price = self.cache.price(self.config.instrument_id, price_type=None)
        if price is None:
            # 退化：使用最后一根 bar 的收盘价
            bars = self.cache.bars(self.config.primary_bar_type)
            if not bars:
                return None
            price = Decimal(str(float(bars[0].close)))
        qty = (bal * Decimal(str(pct))) / Decimal(str(price))
        return qty

    def _log_swing(self, prefix: str) -> None:
        s = self.swing_mgr.current
        if s is None:
            return
        self.log.info(
            f"[{prefix}] low={s.swing_low} high={s.swing_high} "
            f"levels={s.fib_levels}"
        )

    @staticmethod
    def _build_run_cfg(d: dict) -> StrategyRunConfig:
        """把 dict 还原为 StrategyRunConfig dataclass"""
        if not d:
            return StrategyRunConfig()
        fibo = FiboRuleConfig(**d.get("fibo", {}))
        boll = BollConfig(**d.get("boll", {}))
        position = PositionConfig(**d.get("position", {}))
        top = {k: v for k, v in d.items() if k not in ("fibo", "boll", "position")}
        return StrategyRunConfig(fibo=fibo, boll=boll, position=position, **top)


# 为 hub 补一个 BarType → tf 的辅助方法（register 时使用）
def _hub_tf_of_bar_type(self, bar_type: BarType) -> str | None:
    spec = bar_type.spec
    step = spec.step
    agg = spec.aggregation.name
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


MultiTimeframeIndicatorHub.tf_of_bar_type = _hub_tf_of_bar_type  # type: ignore[attr-defined]
