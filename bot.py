"""
Main Bot Orchestrator for the Variational Omni Trading Bot.

This is the top-level loop that:
  1. Initialises all components (client, price feed, risk manager, strategy).
  2. Runs continuous trade cycles.
  3. Handles errors gracefully and respects the circuit breaker.
  4. Logs every trade to both the console/file and a CSV trade log.
"""

from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timezone

from config import BotConfig
from exceptions import (
    AuthenticationError,
    InsufficientBalanceError,
    PriceFeedError,
    RateLimitError,
)
from logger import get_trade_logger, setup_logging
from price_feed import PriceFeed
from risk_manager import BotPausedError, RiskManager
from strategy import DeltaNeutralStrategy
from variational_client import VariationalClient

logger: logging.Logger  # initialised in run()


class TradingBot:
    """
    Top-level orchestrator for the Variational Omni volume-farming bot.

    Usage:
        bot = TradingBot(config)
        bot.run()
    """

    def __init__(self, config: BotConfig) -> None:
        self._cfg = config
        self._running = False

        # Components (created in _initialise)
        self._client: VariationalClient | None = None
        self._price_feed: PriceFeed | None = None
        self._risk_mgr: RiskManager | None = None
        self._strategy: DeltaNeutralStrategy | None = None
        self._trade_logger: logging.Logger | None = None

        # Counters
        self._cycle_count = 0

    # ─────────────────────────────────────────────────────────
    #  Lifecycle
    # ─────────────────────────────────────────────────────────

    def run(self) -> None:
        """
        Start the bot's main trading loop.

        Blocks until interrupted (Ctrl+C) or until the circuit breaker trips.
        """
        global logger
        logger = setup_logging(self._cfg.logging)
        self._trade_logger = get_trade_logger()

        logger.info("=" * 60)
        logger.info("  Variational Omni Trading Bot — Starting Up")
        logger.info("  Pair:      %s", self._cfg.trading.symbol)
        logger.info("  Size:      $%.2f", self._cfg.trading.position_size_usdc)
        logger.info("  Leverage:  %.1fx", self._cfg.trading.leverage)
        logger.info("  TP/SL:     ±%.2f%%", self._cfg.trading.tp_sl_distance_pct * 100)
        logger.info("  Min Bal:   $%.2f", self._cfg.trading.min_balance_usdc)
        logger.info("=" * 60)

        # ── Validate config ──
        issues = self._cfg.validate()
        if issues:
            for issue in issues:
                logger.error("Config issue: %s", issue)
            logger.critical("Fix configuration issues before running the bot.")
            return

        # ── Initialise components ──
        self._initialise()

        # ── Register graceful shutdown on SIGINT / SIGTERM ──
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        # ── Main loop ──
        self._running = True
        logger.info("Bot is LIVE — entering main loop.")

        while self._running:
            try:
                self._run_one_cycle()
            except InsufficientBalanceError as exc:
                logger.critical("STOPPING — %s", exc)
                break
            except AuthenticationError:
                logger.critical(
                    "STOPPING — Authentication failed. "
                    "Refresh your VARIATIONAL_COOKIE or VARIATIONAL_AUTH_TOKEN and restart."
                )
                break
            except RateLimitError as exc:
                logger.warning("Rate limited — sleeping %.0fs…", exc.retry_after)
                time.sleep(exc.retry_after)
                continue
            except KeyboardInterrupt:
                logger.info("KeyboardInterrupt received — shutting down.")
                break
            except Exception as exc:
                logger.exception("Unhandled error in main loop: %s", exc)
                assert self._risk_mgr is not None
                if self._risk_mgr.record_error():
                    logger.critical("Circuit breaker tripped — shutting down.")
                    break
                time.sleep(self._cfg.trading.cycle_cooldown)

        self._shutdown()

    # ─────────────────────────────────────────────────────────
    #  One trade cycle
    # ─────────────────────────────────────────────────────────

    def _run_one_cycle(self) -> None:
        """Execute a single trade cycle with full error handling."""
        assert self._client is not None
        assert self._risk_mgr is not None
        assert self._strategy is not None

        self._cycle_count += 1
        logger.info(
            "─── Cycle #%d ─── %s",
            self._cycle_count,
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        )

        # ── Pre-trade checks ──
        try:
            account = self._client.get_account_info()
            self._risk_mgr.check_pre_trade(account)
        except BotPausedError as exc:
            logger.info("Cycle skipped: %s — cooling down %.0fs.", exc, self._cfg.trading.cycle_cooldown)
            time.sleep(self._cfg.trading.cycle_cooldown)
            return

        # ── Execute trade ──
        record = self._strategy.execute_trade_cycle()

        if record is not None:
            # Log to CSV
            assert self._trade_logger is not None
            self._trade_logger.info(
                "%s,%s,%s,%s,%.2f,%.2f,%.2f,%.1f,%+.6f,%s,%.1f",
                datetime.fromtimestamp(record.closed_at, tz=timezone.utc)
                    .strftime("%Y-%m-%d %H:%M:%S"),
                record.trade_id,
                record.symbol,
                record.side.value,
                record.entry_price,
                record.exit_price,
                record.size_usdc,
                record.leverage,
                record.pnl_usdc,
                record.outcome,
                record.duration_seconds,
            )

            logger.info("📊 Stats: %s", self._risk_mgr.stats_summary)
            self._risk_mgr.reset_errors()
        else:
            logger.info("Cycle produced no trade (order not filled).")

        # ── Cooldown ──
        logger.info("Cooling down %.0fs before next cycle…", self._cfg.trading.cycle_cooldown)
        time.sleep(self._cfg.trading.cycle_cooldown)

    # ─────────────────────────────────────────────────────────
    #  Initialisation
    # ─────────────────────────────────────────────────────────

    def _initialise(self) -> None:
        """Create and wire up all bot components."""
        logger.info("Initialising components…")

        self._client = VariationalClient(self._cfg.api, self._cfg.wallet)
        self._price_feed = PriceFeed(self._cfg.price_feed)
        self._risk_mgr = RiskManager(self._cfg.trading)

        # ── Resolve market ID ──
        try:
            market_info = self._client.get_market_info(self._cfg.trading.symbol)
            market_id = market_info.market_id
            logger.info(
                "Market resolved: %s → id=%s  maxLeverage=%sx",
                market_info.symbol, market_id, market_info.max_leverage,
            )
        except Exception as exc:
            logger.warning(
                "Could not resolve market from API (%s). "
                "Using symbol as market_id fallback.",
                exc,
            )
            market_id = self._cfg.trading.symbol

        self._strategy = DeltaNeutralStrategy(
            client=self._client,
            price_feed=self._price_feed,
            risk_mgr=self._risk_mgr,
            cfg=self._cfg.trading,
            market_id=market_id,
        )

        logger.info("All components initialised ✓")

    # ─────────────────────────────────────────────────────────
    #  Shutdown
    # ─────────────────────────────────────────────────────────

    def _shutdown(self) -> None:
        """Cleanly shut down all components."""
        logger.info("Shutting down…")

        if self._risk_mgr:
            logger.info("Final stats: %s", self._risk_mgr.stats_summary)

        if self._client:
            self._client.close()
        if self._price_feed:
            self._price_feed.close()

        logger.info("Bot stopped. Goodbye! 👋")

    def _signal_handler(self, signum: int, frame: object) -> None:
        """Handle OS signals for graceful shutdown."""
        logger.info("Received signal %d — stopping after current cycle.", signum)
        self._running = False
