"""
Risk Manager for the Variational Omni Trading Bot.

Responsible for:
  - Pre-trade balance validation
  - Position sizing
  - Leverage clamping
  - Consecutive-error tracking and circuit-breaker
"""

from __future__ import annotations

import logging

from config import TradingConfig
from exceptions import InsufficientBalanceError
from models import AccountInfo

logger = logging.getLogger("variational_bot.risk_manager")


class RiskManager:
    """
    Guards the bot against excessive losses and configuration mistakes.

    Call check_pre_trade() before every new trade cycle.
    """

    def __init__(self, cfg: TradingConfig) -> None:
        self._cfg = cfg
        self._consecutive_errors: int = 0
        self._total_trades: int = 0
        self._total_wins: int = 0
        self._total_losses: int = 0
        self._cumulative_pnl: float = 0.0

    # ─────────────────────────────────────────────────────────
    #  Pre-trade checks
    # ─────────────────────────────────────────────────────────

    def check_pre_trade(self, account: AccountInfo) -> None:
        """
        Validate that it is safe to open a new position.

        Raises:
            InsufficientBalanceError: If the balance is below the threshold.
        """
        if account.balance_usdc < self._cfg.min_balance_usdc:
            raise InsufficientBalanceError(
                balance=account.balance_usdc,
                threshold=self._cfg.min_balance_usdc,
            )

        logger.info(
            "Pre-trade OK — balance=$%.2f  equity=$%.2f  margin=$%.2f",
            account.balance_usdc, account.equity_usdc, account.available_margin,
        )

    # ─────────────────────────────────────────────────────────
    #  Position sizing
    # ─────────────────────────────────────────────────────────

    def compute_position_size(self, account: AccountInfo) -> float:
        """
        Return the notional size (USDC) for the next trade.

        position_size_usdc in config is the desired NOTIONAL value.
        The margin required = notional / leverage.
        We clamp so that margin used <= 90% of available margin.
        """
        leverage = self.compute_leverage()
        max_notional = account.available_margin * leverage * 0.9
        size = min(self._cfg.position_size_usdc, max_notional)
        size = max(size, 1.0)  # floor at $1
        logger.info(
            "Position size: $%.2f notional (margin=$%.2f, avail=$%.2f, lev=%.0fx)",
            size, size / leverage, account.available_margin, leverage,
        )
        return size

    def compute_leverage(self) -> float:
        """Return the leverage for the next trade (from config, clamped)."""
        return max(1.0, min(self._cfg.leverage, 50.0))

    # ─────────────────────────────────────────────────────────
    #  TP / SL price calculation
    # ─────────────────────────────────────────────────────────

    def compute_tp_sl(
        self, entry_price: float, side: str
    ) -> tuple[float, float]:
        """
        Calculate symmetric TP and SL prices around entry.

        For a LONG:
            TP = entry * (1 + distance)
            SL = entry * (1 - distance)

        For a SHORT:
            TP = entry * (1 - distance)
            SL = entry * (1 + distance)

        Args:
            entry_price: The fill price of the entry order.
            side:        "long" or "short".

        Returns:
            (tp_price, sl_price) tuple.
        """
        d = self._cfg.tp_sl_distance_pct

        if side == "long":
            tp = entry_price * (1.0 + d)
            sl = entry_price * (1.0 - d)
        else:
            tp = entry_price * (1.0 - d)
            sl = entry_price * (1.0 + d)

        logger.info(
            "TP/SL for %s entry=$%.2f → TP=$%.2f  SL=$%.2f  (±%.2f%%)",
            side.upper(), entry_price, tp, sl, d * 100,
        )
        return tp, sl

    # ─────────────────────────────────────────────────────────
    #  Error tracking / Circuit breaker
    # ─────────────────────────────────────────────────────────

    def record_error(self) -> bool:
        """
        Increment the consecutive-error counter.

        Returns:
            True if the bot should shut down (too many errors in a row).
        """
        self._consecutive_errors += 1
        if self._consecutive_errors >= self._cfg.max_consecutive_errors:
            logger.critical(
                "Circuit breaker triggered — %d consecutive errors!",
                self._consecutive_errors,
            )
            return True
        return False

    def reset_errors(self) -> None:
        """Reset the consecutive-error counter after a successful cycle."""
        self._consecutive_errors = 0

    # ─────────────────────────────────────────────────────────
    #  Trade statistics
    # ─────────────────────────────────────────────────────────

    def record_trade(self, pnl: float) -> None:
        """Record the outcome of a completed trade for statistics."""
        self._total_trades += 1
        self._cumulative_pnl += pnl
        if pnl >= 0:
            self._total_wins += 1
        else:
            self._total_losses += 1

    @property
    def stats_summary(self) -> str:
        """Human-readable trading stats."""
        wr = (
            (self._total_wins / self._total_trades * 100)
            if self._total_trades > 0
            else 0.0
        )
        return (
            f"Trades: {self._total_trades} | "
            f"Wins: {self._total_wins} | Losses: {self._total_losses} | "
            f"Winrate: {wr:.1f}% | "
            f"Cumulative PnL: ${self._cumulative_pnl:+.2f}"
        )


class BotPausedError(Exception):
    """Raised when a pre-trade check suggests the bot should skip this cycle."""
    pass
