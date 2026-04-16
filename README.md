# QuantFibonacci

基于 **NautilusTrader** 的加密货币斐波那契量化交易框架，完整实现 @Fibonacci 交易员的逢低做多+分批补仓+波段切换交易体系。

- 支持 BTC / ETH / SOL 等主流币
- 支持 Binance / OKX / Bybit / Hyperliquid 等交易所（NautilusTrader 原生集成）
- 回测 / 模拟盘 / 实盘代码完全一致，一键切换

---

## 目录结构

```
QuantFibonacci/
├── config.py           # 全局配置（斐波参数 / BOLL 容差 / 仓位纪律）
├── fibonacci.py        # 斐波那契计算模块（回撤 + 时间斐波 + 1.618 扩展 + 波段动态切换）
├── boll_resonance.py   # BOLL 共振验证（6H / 8H / 12H / 1D 下轨 / 中轨）
├── indicators.py       # 多时间框架指标（MACD + BOLL + EMA30）+ 归零金叉 / 背离检测
├── strategy.py         # NautilusTrader 主策略（入场 / 止盈 / 止损 / 波段切换）
├── backtest.py         # 回测引擎 + 参数网格优化
├── main.py             # CLI 入口（demo / backtest / optimize / paper / live）
└── requirements.txt
```

---

## 环境要求

| 项目 | 要求 |
|---|---|
| Python | 3.12 – 3.14 |
| OS | Linux (glibc ≥ 2.35) / macOS 15+ / Windows Server 2022+ |
| 内存 | ≥ 4 GB |

## 安装

```bash
cd QuantFibonacci
pip install -r requirements.txt
# 或使用 uv（NautilusTrader 官方推荐）
# uv pip install -r requirements.txt
```

验证安装：
```bash
python main.py demo
```
若终端打印出两个波段的三档斐波位，即安装成功。

---

## 快速上手

### 1. 斐波那契演示（无需数据，立即可跑）

```bash
python main.py demo
```

输出帖文两个经典波段的计算结果：
- 上涨波段 **BTC 64918 → 73773**
- 下跌波段 **BTC 75998 → 65569**

### 2. 回测

准备 1 小时 K 线 CSV，列：`timestamp,open,high,low,close,volume`（`timestamp` 支持毫秒或 ISO 字符串）。

```bash
python main.py backtest btc_1h.csv 64918 73773
```

参数说明：
- `btc_1h.csv` — 1H 历史数据
- `64918` — 初始波段起涨点（Swing Low）
- `73773` — 初始波段最高点（Swing High）

回测自动把 1H 重采样为 2H / 4H / 6H / 8H / 12H / 1D / 1W 并注入 BacktestEngine。

输出指标：最终余额、胜率、盈亏比、最大回撤、成交笔数。

### 3. 参数优化

网格扫描斐波容差 × BOLL 容差 × 头仓比例，输出 Top 10：

```bash
python main.py optimize btc_1h.csv 64918 73773
```

### 4. 模拟盘（Binance Testnet）

```bash
export BINANCE_API_KEY=<your_testnet_key>
export BINANCE_API_SECRET=<your_testnet_secret>
# 可选：指定币种，默认 BTCUSDT
export FIBO_SYMBOL=BTCUSDT
# 可选：Telegram 告警（见下方章节）
export TELEGRAM_BOT_TOKEN=<your_bot_token>
export TELEGRAM_CHAT_ID=<your_chat_id>
python main.py paper
```

### 5. 实盘

```bash
export BINANCE_API_KEY=<your_live_key>
export BINANCE_API_SECRET=<your_live_secret>
python main.py live
```

> 启动时会自动从 Binance 拉 300 根 4H K 线，自动识别当前波段 (Swing Low→High)，**无需手动填写**。
> ⚠️ 实盘前强烈建议先在 testnet 上跑 1–2 周，确认波段切换 / 仓位账本 / 止损挂单行为符合预期。

---

## 自动波段识别（swing_detector.py）

框架强调"起涨点（Swing Low）→ 最高点（Swing High）"，本模块用 Pivot 算法自动找到当前活跃波段：

- 扫描最近 300 根 4H K 线
- `window=5`：一根 bar 的 high/low 若是左右各 5 根内的最值 → 视为 Pivot
- 取"最近的 Pivot High + 其之前最近的 Pivot Low"作为当前波段
- 数据源：Binance 公共 REST（无需 API key）

单独调用：
```python
from swing_detector import auto_detect_swing
swing = auto_detect_swing(symbol="BTCUSDT", interval="4h")
print(swing.summary())
```

---

## Telegram 下单告警（notifier.py）

配置两个环境变量即可启用；不设置则静默降级，不影响交易。

**拿到 bot_token / chat_id**：
1. Telegram 搜 `@BotFather` → `/newbot` → 得到 `bot_token`
2. 把 bot 加到一个群，群里随意发一条消息
3. 浏览器访问 `https://api.telegram.org/bot<token>/getUpdates` → 找 `"chat":{"id":-100xxx}` 即 `chat_id`

```bash
export TELEGRAM_BOT_TOKEN=123456:ABCDEF...
export TELEGRAM_CHAT_ID=-1001234567890
```

**推送事件**（异步非阻塞，失败只记日志）：

| 事件 | 示例 |
|---|---|
| 策略启动 | 🤖 FIBO 启动，波段 73514→75425，三档斐波位 |
| 开多 | 🟢 开多 price 68300, pct 2%, total 2%, reason fib=0.618 reso=strong@1D |
| 止盈 | 💰 多单止盈 price 72000, 平仓 20%, 触发 1D BOLL 中轨 |
| 开空 | 🔴 开空（超短）+ 止损价 |
| 波段切换 | 🔄 支撑已破，新波段 + 新三档 |
| 持仓关闭 | ✅ / ❌ PnL = xxx |

---

## 策略逻辑对应表

| 框架规则 | 代码位置 |
|---|---|
| 低多为主，周线金叉不空 | `strategy.py` 只有 `_try_short_breakout` 为超短空，默认 `enable_short_when_breakout=True` |
| 回撤位公式 `High − (High−Low) × ratio` | `fibonacci.calculate_fib_retracement` |
| 0.382 / 0.5 / 0.618 三档 | `FiboRuleConfig.level_1/2/3` |
| 回踩不足 0.382 即可补仓 | `FiboRuleConfig.touch_tolerance`（默认 1%） |
| 时间斐波 `天数 × 0.618` | `fibonacci.calculate_time_fib` |
| 斐波扩展 1.618 预测牛市高点 | `fibonacci.calculate_fib_extension` |
| 支撑跌破 → 切换新波段 | `SwingManager.on_price_update` + `strategy._switch_swing_on_break` |
| 2H+ 无背离才开多 | `MultiTimeframeIndicatorHub.is_entry_divergence_free` |
| 4H MACD 归零金叉 | `TimeframeIndicators.is_macd_zero_golden_cross` |
| BOLL 共振（6H / 8H / 12H / 1D） | `boll_resonance.check_multi_timeframe_resonance` |
| 头仓 2% + 分批补仓 | `PositionConfig.head_position_pct / add_position_pct` |
| 多单不止损，只分批止盈 | `strategy._manage_long_take_profit`，目标：1D BOLL 中轨 / 1D EMA30 / 斐波第二点 / 波段高 |
| 空单必须止损 | `strategy._try_short_breakout` 开仓后立即挂 stop_market |
| 反弹浪第二个斐波点 = 强压力 → 减仓 | `PositionConfig.second_fib_reduce_factor` |
| 总仓位上限 20% | `PositionConfig.max_total_pct` |

---

## 配置调优

所有参数集中在 `config.py`。常用调节项：

```python
# config.py
FiboRuleConfig(
    level_1=0.382, level_2=0.5, level_3=0.618,
    touch_tolerance=0.01,   # 价格触及斐波位的缓冲区（1%）
)
BollConfig(
    period=20, num_std=2.0,
    resonance_tolerance=0.005,  # 斐波位与 BOLL 轨道的共振容差（0.5%）
)
PositionConfig(
    head_position_pct=0.02,     # 头仓 2%
    add_position_pct=0.02,      # 每次补仓 2%
    max_total_pct=0.20,         # 总仓位 ≤ 20%
    short_stop_loss_pct=0.03,   # 空单止损 3%
)
```

---

## 数据来源建议

| 来源 | 用法 |
|---|---|
| Binance Public Data | [data.binance.vision](https://data.binance.vision) 提供免费 1m/1h/1d 历史 K 线 |
| CCXT 抓取 | `ccxt.binance().fetch_ohlcv('BTC/USDT', '1h', since=...)` |
| NautilusTrader Catalog | 已采集数据可存为 Parquet catalog，详见官方文档 |

CSV 示例（前几行）：
```csv
timestamp,open,high,low,close,volume
1712016000000,69234.1,69980.0,69100.5,69856.2,1283.45
1712019600000,69856.2,70450.0,69520.1,70120.8,1875.91
...
```

---

## 切换交易所

默认 Binance。改为 OKX：

1. 把 `config.py` 的 `VenueConfig.venue = "OKX"`，`symbol = "BTC-USDT"`。
2. 把 `main.py` 中 `cli_live` 里的 `BinanceDataClientConfig` / `BinanceExecClientConfig` 换成 `OKXDataClientConfig` / `OKXExecClientConfig`（NautilusTrader v1.225 已原生支持）。

---

## 已知限制 / 待增强

- 背离检测为简化版（价峰/MACD 峰比较），若需更严谨的背离算法可替换 `TimeframeIndicators.has_bearish_divergence`。
- 初始波段需手动指定；生产环境建议加一个"自动找最近 Swing Low/High"的模块。
- 多单止盈目前只取 20% 分批，可在 `strategy._manage_long_take_profit` 中改为阶梯式 30%/30%/40%。
- 尚未接入 Telegram / Feishu 告警，如需请在 `strategy` 内 `self.log.info` 位置插入 webhook。

---

## 法律声明

本项目仅作量化策略研究与教学用途，**不构成任何投资建议**。加密货币市场波动剧烈，实盘交易可能导致全部本金亏损。使用者需自行承担风险并遵守所在国家/地区法规。
