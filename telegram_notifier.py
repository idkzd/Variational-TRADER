"""
Telegram Notifier — hourly PnL summaries from real API data.

Runs in a background thread. Fetches balance, uPnL, rPnL, positions
directly from Variational API and sends a formatted message to Telegram.

Setup:
  1. Create a bot via @BotFather → get TELEGRAM_BOT_TOKEN
  2. Send /start to the bot, then get chat id via https://api.telegram.org/bot<TOKEN>/getUpdates
  3. Add to .env:
       TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
       TELEGRAM_CHAT_ID=123456789
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger("variational_bot.telegram")


class TelegramNotifier:
    """Sends periodic PnL reports to Telegram using real API data."""

    def __init__(
        self,
        client,  # VariationalClient instance
        interval_seconds: int = 3600,
        bot_token: str = "",
        chat_id: str = "",
    ):
        self._client = client
        self._interval = interval_seconds
        self._token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self._running = False
        self._thread: threading.Thread | None = None
        self._start_balance: float | None = None
        self._start_time: float = time.time()

    @property
    def is_configured(self) -> bool:
        return bool(self._token and self._chat_id)

    def start(self):
        """Start the background reporting thread."""
        if not self.is_configured:
            logger.warning(
                "Telegram not configured — set TELEGRAM_BOT_TOKEN and "
                "TELEGRAM_CHAT_ID in .env to enable notifications."
            )
            return

        self._running = True
        self._start_time = time.time()

        # Record starting balance
        try:
            data = self._client._request("GET", "/portfolio")
            self._start_balance = float(data.get("balance", 0))
        except Exception:
            self._start_balance = None

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

        self.send_message("🟢 *Variational Bot запущен*\n" + self._build_report())
        logger.info("Telegram notifier started (every %ds)", self._interval)

    def stop(self):
        """Stop the background thread and send final report."""
        self._running = False
        if self.is_configured:
            try:
                self.send_message("🔴 *Variational Bot остановлен*\n" + self._build_report())
            except Exception:
                pass

    def _loop(self):
        """Background loop — send report every interval."""
        while self._running:
            time.sleep(self._interval)
            if not self._running:
                break
            try:
                report = self._build_report()
                self.send_message(f"📊 *Часовой отчёт*\n{report}")
            except Exception as exc:
                logger.warning("Telegram report failed: %s", exc)

    def _build_report(self) -> str:
        """Fetch real data from API and format a report."""
        try:
            # Portfolio: balance + unrealized PnL
            portfolio = self._client._request("GET", "/portfolio")
            balance = float(portfolio.get("balance", 0))
            upnl_total = float(portfolio.get("upnl", 0))

            # Positions: per-position details
            pos_data = self._client._request("GET", "/positions")
            positions = pos_data.get("_list", [])

            # Sum realized PnL and funding from positions
            total_rpnl = 0.0
            total_funding = 0.0
            pos_lines = []

            for item in positions:
                info = item.get("position_info", {})
                underlying = info.get("instrument", {}).get("underlying", "?")
                qty = float(info.get("qty", 0))
                entry = float(info.get("avg_entry_price", 0))
                upnl = float(item.get("upnl", 0))
                rpnl = float(item.get("rpnl", 0))
                funding = float(item.get("cum_funding", 0))
                value = float(item.get("value", 0))

                total_rpnl += rpnl
                total_funding += funding

                side_emoji = "🟢" if qty > 0 else "🔴"
                side_text = "LONG" if qty > 0 else "SHORT"

                pos_lines.append(
                    f"{side_emoji} {underlying} {side_text} "
                    f"${value:.2f} @ ${entry:.2f}\n"
                    f"   uPnL: ${upnl:+.4f} | rPnL: ${rpnl:+.4f}"
                )

            # Session PnL (balance change since start)
            session_pnl = ""
            if self._start_balance is not None:
                diff = balance - self._start_balance
                session_pnl = f"\n💰 *Сессия PnL:* `${diff:+.4f}`"

            # Uptime
            elapsed = int(time.time() - self._start_time)
            h, rem = divmod(elapsed, 3600)
            m, s = divmod(rem, 60)
            uptime = f"{h}ч {m}мин"

            # Equity
            equity = balance + upnl_total

            lines = [
                f"",
                f"💵 *Баланс:* `${balance:.4f}`",
                f"📈 *Equity:* `${equity:.4f}`",
                f"📊 *uPnL:* `${upnl_total:+.4f}`",
                f"✅ *rPnL:* `${total_rpnl:+.4f}`",
                f"🔄 *Фандинг:* `${total_funding:+.4f}`",
                session_pnl,
                f"⏱ *Аптайм:* {uptime}",
            ]

            if pos_lines:
                lines.append(f"\n*Позиции ({len(pos_lines)}):*")
                lines.extend(pos_lines)
            else:
                lines.append("\n_Нет открытых позиций_")

            return "\n".join(lines)

        except Exception as exc:
            logger.warning("Failed to build report: %s", exc)
            return f"\n⚠️ Ошибка получения данных: `{exc}`"

    def send_message(self, text: str):
        """Send a message via Telegram Bot API."""
        if not self.is_configured:
            return

        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }

        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code != 200:
                logger.warning("Telegram API error: %s %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.warning("Telegram send failed: %s", exc)

    def send_trade_alert(self, outcome: str, side: str, entry: float, exit_: float,
                         pnl: float, size: float):
        """Send instant alert on trade close."""
        if not self.is_configured:
            return

        emoji = "✅" if pnl >= 0 else "❌"
        side_emoji = "🟢" if side == "long" else "🔴"

        text = (
            f"{emoji} *{outcome}*\n"
            f"{side_emoji} {side.upper()} ${size:.2f}\n"
            f"Вход: `${entry:.2f}` → Выход: `${exit_:.2f}`\n"
            f"PnL: `${pnl:+.4f}`"
        )
        self.send_message(text)
