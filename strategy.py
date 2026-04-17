"""
Delta-Neutral Random Strategy for the Variational Omni Trading Bot.

Strategy overview (volume-farming, ~50% winrate):
  1. No open position -> randomly choose LONG or SHORT (coin flip).
  2. Fetch Variational quote (bid/ask/spread).
  3. If spread <= threshold -> MARKET order (instant fill, counts as volume).
     Else -> LIMIT order at mid-price, wait for fill up to timeout.
  4. Once filled -> monitor price via quotes every poll_interval.
  5. When price crosses TP or SL level -> close with MARKET order.
  6. Log result, cooldown, repeat.

No reliance on /tpsl endpoint - we self-monitor and close via market orders.
This is more reliable and every close = additional volume for points.
"""

from __future__ import annotations

import logging
import random
import time

from config import TradingConfig
from models import (
    Order,
    OrderStatus,
    OrderType,
    Position,
    PositionStatus,
    Side,
    TradeRecord,
    TpSlPair,
)
from risk_manager import RiskManager
from variational_client import VariationalClient

logger = logging.getLogger("variational_bot.strategy")

# Maximum spread (%) to use market order instead of limit
MARKET_ORDER_SPREAD_THRESHOLD = 0.02  # 0.02% -- Variational typically ~0.007%


class DeltaNeutralStrategy:
    """
    Random delta-neutral volume farming strategy.

    Full lifecycle: direction -> entry -> monitor -> exit -> record.
    """

    def __init__(
        self,
        client: VariationalClient,
        price_feed: object,  # PriceFeed -- used as fallback only
        risk_mgr: RiskManager,
        cfg: TradingConfig,
        market_id: str,
    ) -> None:
        self._client = client
        self._price = price_feed  # fallback price source
        self._risk = risk_mgr
        self._cfg = cfg
        self._market_id = market_id  # e.g. "BTC"

    # ---------------------------------------------------------
    #  Full trade cycle
    # ---------------------------------------------------------

    def execute_trade_cycle(self) -> TradeRecord | None:
        """
        Run one complete trade cycle (entry -> monitor -> exit).

        Returns:
            A TradeRecord on success, or None if the cycle was aborted.
        """
        # -- Step 1: Random direction --
        side = self._choose_direction()
        logger.info("=== NEW CYCLE ===  Direction: %s", side.value.upper())

        # -- Step 2: Position sizing (need size before quote for correct qty) --
        account = self._client.get_account_info()
        size = self._risk.compute_position_size(account)
        leverage = self._risk.compute_leverage()

        # -- Step 3: Get Variational quote with real qty and side --
        side_str = "buy" if side == Side.LONG else "sell"
        # Estimate qty in BTC for the quote request
        rough_price = 80000.0  # rough BTC price for qty estimation
        try:
            rough_quote = self._client.get_quote(self._market_id)
            rough_price = rough_quote.mid_price or 80000.0
        except Exception:
            pass
        qty_str = f"{size / rough_price:.6f}"

        quote = self._client.get_quote(self._market_id, qty=qty_str, side=side_str)
        bid, ask, mid = quote.bid_price, quote.ask_price, quote.mid_price
        spread_pct = (ask - bid) / mid * 100 if mid > 0 else 999

        logger.info(
            "Quote: bid=$%.2f  ask=$%.2f  mid=$%.2f  spread=%.4f%%  qid=%s",
            bid, ask, mid, spread_pct, quote.quote_id[:12],
        )

        # -- Step 4: Place entry order with quote_id --
        use_market = spread_pct <= self._cfg.market_spread_threshold
        entry_order = self._place_entry(side, mid, size, leverage, use_market, quote.quote_id)

        if entry_order is None:
            return None

        entry_price = entry_order.fill_price or mid
        entry_time = entry_order.filled_at or time.time()

        logger.info(
            "ENTRY %s -- %s @ $%.2f  size=$%.2f  lev=%.0fx",
            "MARKET" if use_market else "LIMIT",
            side.value.upper(), entry_price, size, leverage,
        )

        # -- Step 5: Compute TP/SL levels --
        tp_price, sl_price = self._risk.compute_tp_sl(entry_price, side.value)

        # -- Step 6: Monitor price and close at TP or SL --
        outcome, exit_price = self._monitor_and_close(
            side, entry_price, tp_price, sl_price, size
        )

        closed_at = time.time()
        duration = closed_at - entry_time

        # -- Step 7: Calculate PnL --
        pnl = self._calculate_pnl(side, entry_price, exit_price, size, leverage)

        record = TradeRecord(
            symbol=self._cfg.symbol,
            side=side,
            entry_price=entry_price,
            exit_price=exit_price,
            size_usdc=size,
            leverage=leverage,
            pnl_usdc=pnl,
            outcome=outcome,
            duration_seconds=duration,
            opened_at=entry_time,
            closed_at=closed_at,
        )

        logger.info(
            "=== CLOSED ===  %s  entry=$%.2f  exit=$%.2f  "
            "PnL=$%+.4f  time=%.1fs",
            outcome, entry_price, exit_price, pnl, duration,
        )

        self._risk.record_trade(pnl)
        return record

    # ---------------------------------------------------------
    #  Direction (coin flip)
    # ---------------------------------------------------------

    @staticmethod
    def _choose_direction() -> Side:
        """50/50 random LONG or SHORT."""
        return random.choice([Side.LONG, Side.SHORT])

    # ---------------------------------------------------------
    #  Entry: market or limit
    # ---------------------------------------------------------

    def _place_entry(
        self,
        side: Side,
        mid_price: float,
        size: float,
        leverage: float,
        use_market: bool,
        quote_id: str = "",
    ) -> Order | None:
        """
        Place entry order. Market if spread is tiny, limit otherwise.

        Returns filled Order or None if limit timed out.
        """
        if use_market:
            logger.info("Spread tight -> using MARKET order")
            order = self._client.place_market_order(
                market_id=self._market_id,
                side=side,
                size_usdc=size,
                price=mid_price,
                quote_id=quote_id,
            )
            # Market orders fill instantly on Variational (fill-or-kill)
            if order.status != OrderStatus.FILLED:
                order = self._confirm_fill(order, timeout=15.0)
            return order if order and order.status == OrderStatus.FILLED else None

        else:
            logger.info("Wider spread -> using LIMIT order at mid=$%.2f", mid_price)
            order = self._client.place_limit_order(
                market_id=self._market_id,
                side=side,
                size_usdc=size,
                price=mid_price,
                leverage=leverage,
            )
            filled = self._wait_for_fill(order)
            if filled is None:
                logger.warning("Limit order timed out -- cycle aborted.")
            return filled

    def _confirm_fill(self, order: Order, timeout: float = 15.0) -> Order | None:
        """Poll briefly to confirm a market order fill."""
        remote_id = order.remote_order_id or order.order_id
        deadline = time.time() + timeout

        while time.time() < deadline:
            try:
                data = self._client.get_order_status(remote_id)
                status = str(data.get("status", "")).lower()
                if status == "filled":
                    order.status = OrderStatus.FILLED
                    order.fill_price = float(
                        data.get("fill_price", data.get("fillPrice", order.price))
                    )
                    order.filled_at = time.time()
                    return order
                if status in ("cancelled", "rejected"):
                    logger.warning("Market order %s: %s", remote_id, status)
                    return None
            except Exception as exc:
                logger.warning("Error confirming fill: %s", exc)
            time.sleep(2.0)

        logger.warning("Market order fill confirmation timed out -- assuming filled")
        # Assume filled (Variational market orders are fill-or-kill)
        order.status = OrderStatus.FILLED
        order.fill_price = order.price
        order.filled_at = time.time()
        return order

    def _wait_for_fill(self, order: Order) -> Order | None:
        """
        Poll until limit order fills or timeout expires.
        Cancels on timeout.
        """
        deadline = time.time() + self._cfg.order_fill_timeout
        remote_id = order.remote_order_id or order.order_id

        logger.info(
            "Waiting up to %.0fs for limit fill (%s)...",
            self._cfg.order_fill_timeout, remote_id,
        )

        while time.time() < deadline:
            try:
                data = self._client.get_order_status(remote_id)
                status = str(data.get("status", "")).lower()

                if status == "filled":
                    order.status = OrderStatus.FILLED
                    order.fill_price = float(
                        data.get("fill_price", data.get("fillPrice", order.price))
                    )
                    order.filled_at = time.time()
                    return order

                if status in ("cancelled", "rejected", "expired"):
                    logger.warning("Order %s -> %s", remote_id, status)
                    return None

            except Exception as exc:
                logger.warning("Error polling order: %s", exc)

            time.sleep(self._cfg.poll_interval)

        # Timeout -> cancel
        logger.warning("Limit order timed out -> cancelling %s", remote_id)
        self._client.cancel_order(remote_id)
        return None

    # ---------------------------------------------------------
    #  Monitor price -> close at TP/SL via market order
    # ---------------------------------------------------------

    def _monitor_and_close(
        self,
        side: Side,
        entry_price: float,
        tp_price: float,
        sl_price: float,
        size_usdc: float,
    ) -> tuple[str, float]:
        """
        Continuously poll Variational quotes. When price crosses TP or SL,
        close the position with a market order using exact position qty.

        Returns:
            (outcome, exit_price) -- "TP_HIT" or "SL_HIT".
        """
        close_side = Side.SHORT if side == Side.LONG else Side.LONG

        logger.info(
            "Monitoring: TP=$%.2f  SL=$%.2f  (entry=$%.2f, %s)",
            tp_price, sl_price, entry_price, side.value.upper(),
        )

        check_count = 0
        pending_outcome: str | None = None
        pending_trigger: float = 0.0

        while True:
            check_count += 1
            try:
                quote = self._client.get_quote(self._market_id)
                current = quote.mid_price

                # Log every 10th check
                if check_count % 10 == 1:
                    logger.debug(
                        "Price #%d: $%.2f  (TP=$%.2f  SL=$%.2f)",
                        check_count, current, tp_price, sl_price,
                    )

                # -- Check TP/SL --
                hit = self._check_tp_sl_hit(side, current, tp_price, sl_price)
                if hit is not None or pending_outcome is not None:
                    if hit is not None:
                        pending_outcome, pending_trigger = hit

                    if pending_outcome and check_count == 1:
                        # Don't log again if already logged
                        pass
                    elif hit is not None:
                        logger.info(
                            "%s at $%.2f -- closing with MARKET",
                            pending_outcome, pending_trigger,
                        )

                    # Get exact position qty from API
                    positions = self._client.get_open_positions()
                    raw_qty = ""
                    actual_qty_usdc = size_usdc
                    for p in positions:
                        if p.symbol == f"{self._market_id}-USDC":
                            raw_qty = p.raw_qty  # exact string like "-0.006451"
                            actual_qty_usdc = p.size_usdc
                            break

                    if not raw_qty:
                        # Position already gone (maybe closed externally)
                        logger.warning("Position not found — assuming already closed")
                        return pending_outcome or "TP_HIT", current

                    # Use absolute value of raw_qty for the close order
                    abs_qty = f"{abs(float(raw_qty)):.6f}"

                    close_order = self._client.place_market_order(
                        market_id=self._market_id,
                        side=close_side,
                        size_usdc=actual_qty_usdc,
                        price=current,
                        reduce_only=True,
                        qty_override=abs_qty,
                    )

                    exit_price = close_order.fill_price or current
                    return pending_outcome, exit_price

            except Exception as exc:
                exc_str = str(exc)
                logger.warning("Monitoring error: %s", exc)

                # Handle 418 ban — parse wait time and sleep
                if "418" in exc_str or "banned" in exc_str:
                    wait = 30  # default
                    try:
                        import re
                        m = re.search(r'"wait_until_seconds"\s*:\s*(\d+)', exc_str)
                        if m:
                            wait = int(m.group(1)) + 2
                    except Exception:
                        pass
                    logger.warning("Rate-limited — waiting %ds before retry", wait)
                    time.sleep(wait)
                    continue

                # For qty-exceeds errors, don't spam — wait a bit
                if "exceeds position size" in exc_str:
                    time.sleep(3)
                    continue

            time.sleep(self._cfg.poll_interval)

    @staticmethod
    def _check_tp_sl_hit(
        side: Side,
        current_price: float,
        tp_price: float,
        sl_price: float,
    ) -> tuple[str, float] | None:
        """
        LONG:  TP when price >= tp_price, SL when price <= sl_price
        SHORT: TP when price <= tp_price, SL when price >= sl_price
        """
        if side == Side.LONG:
            if current_price >= tp_price:
                return "TP_HIT", current_price
            if current_price <= sl_price:
                return "SL_HIT", current_price
        else:
            if current_price <= tp_price:
                return "TP_HIT", current_price
            if current_price >= sl_price:
                return "SL_HIT", current_price
        return None

    # ---------------------------------------------------------
    #  PnL
    # ---------------------------------------------------------

    @staticmethod
    def _calculate_pnl(
        side: Side,
        entry_price: float,
        exit_price: float,
        size_usdc: float,
        leverage: float,
    ) -> float:
        """PnL calculation. size_usdc is NOTIONAL, leverage already included."""
        if entry_price == 0:
            return 0.0
        if side == Side.LONG:
            return round(size_usdc * (exit_price - entry_price) / entry_price, 6)
        else:
            return round(size_usdc * (entry_price - exit_price) / entry_price, 6)
