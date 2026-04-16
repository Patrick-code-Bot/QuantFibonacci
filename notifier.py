"""
Telegram 下单告警（异步非阻塞）

使用 stdlib urllib，无需额外依赖。
环境变量：
    TELEGRAM_BOT_TOKEN=123456:ABCDEF...
    TELEGRAM_CHAT_ID=-1001234567890

如何拿到这两个值（一次性配置）：
    1. 在 Telegram 搜 @BotFather → /newbot → 得到 bot_token
    2. 把你的 bot 加到一个群，在群里随意发一条消息
    3. 浏览器打开：https://api.telegram.org/bot<token>/getUpdates
       找 "chat":{"id": -100xxx} → 即 chat_id
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger("TelegramNotifier")


class TelegramNotifier:
    """
    异步推送 —— 下单主循环不会被网络阻塞。
    失败只打日志，不抛异常（绝不影响交易）。
    """

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
        enabled: bool | None = None,
        prefix: str = "🤖 FIBO",
    ):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self.prefix = prefix
        if enabled is None:
            enabled = bool(self.bot_token and self.chat_id)
        self.enabled = enabled
        if not self.enabled:
            log.info(
                "TelegramNotifier 未启用（缺少 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID）"
            )

    # ---------- 基础发送 ----------
    def send(self, text: str, parse_mode: str = "Markdown") -> None:
        if not self.enabled:
            return
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        full = f"{self.prefix}  `{ts}`\n{text}"
        threading.Thread(
            target=self._post, args=(full, parse_mode), daemon=True
        ).start()

    def _post(self, text: str, parse_mode: str) -> None:
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = json.dumps(
                {
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=5).read()
        except Exception as e:
            log.warning(f"Telegram 推送失败: {e}")

    # ---------- 业务事件（格式化消息）----------
    def on_start(self, symbol: str, swing_low: float, swing_high: float,
                 levels: dict, mode: str) -> None:
        self.send(
            "*策略启动*\n"
            f"• 模式: `{mode}`\n"
            f"• 标的: `{symbol}`\n"
            f"• 波段: `{swing_low:.2f} → {swing_high:.2f}`\n"
            f"• 0.382: `{levels.get(0.382)}`\n"
            f"• 0.5: `{levels.get(0.5)}`\n"
            f"• 0.618: `{levels.get(0.618)}` (最强抄底)"
        )

    def on_long_open(self, price: float, pct: float, total_pct: float,
                     reason: str) -> None:
        self.send(
            "🟢 *开多*\n"
            f"• 价格: `{price:.2f}`\n"
            f"• 本次仓位: `{pct:.2%}`\n"
            f"• 总仓位: `{total_pct:.2%}`\n"
            f"• 原因: {reason}"
        )

    def on_long_take_profit(self, price: float, fraction: float,
                            reason: str) -> None:
        self.send(
            "💰 *多单止盈*\n"
            f"• 价格: `{price:.2f}`\n"
            f"• 平仓比例: `{fraction:.0%}`\n"
            f"• 触发: {reason}"
        )

    def on_short_open(self, price: float, stop: float, reason: str) -> None:
        self.send(
            "🔴 *开空（超短）*\n"
            f"• 价格: `{price:.2f}`\n"
            f"• 止损: `{stop:.2f}`\n"
            f"• 原因: {reason}"
        )

    def on_swing_switch(self, new_low: float, new_high: float,
                        levels: dict) -> None:
        self.send(
            "🔄 *波段切换（支撑已破）*\n"
            f"• 新波段: `{new_low:.2f} → {new_high:.2f}`\n"
            f"• 新 0.382: `{levels.get(0.382)}`\n"
            f"• 新 0.5: `{levels.get(0.5)}`\n"
            f"• 新 0.618: `{levels.get(0.618)}`"
        )

    def on_position_closed(self, pnl: float, pnl_pct: float | None = None) -> None:
        emoji = "✅" if pnl >= 0 else "❌"
        pct_part = f" ({pnl_pct:.2%})" if pnl_pct is not None else ""
        self.send(f"{emoji} *仓位平仓* PnL = `{pnl:.2f}`{pct_part}")

    def on_error(self, err: str) -> None:
        self.send(f"⚠️ *错误*\n`{err[:500]}`")


# 默认单例（模块级，方便策略内直接 import）
DEFAULT_NOTIFIER: TelegramNotifier | None = None


def get_notifier() -> TelegramNotifier:
    global DEFAULT_NOTIFIER
    if DEFAULT_NOTIFIER is None:
        DEFAULT_NOTIFIER = TelegramNotifier()
    return DEFAULT_NOTIFIER
