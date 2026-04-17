"""
Smart Trading Strategy — Cross-exchange latency + momentum signals.

Core edge (0% fees environment):
  Binance price LEADS Variational by 100-500ms. When Binance moves,
  we enter on Variational BEFORE it catches up. Even a tiny statistical
  edge (51%) prints money with 0% fees and high frequency.

Signal stack (all must align for entry):
  1. PRICE DISLOCATION: Binance mid vs Variational mid divergence
     → If Binance > Variational → LONG (Var will catch up)
     → If Binance < Variational → SHORT
  2. MICRO-MOMENTUM: Last N Binance ticks confirm the direction
     (not just noise — sustained move over ~5-10 seconds)
  3. VOLATILITY FILTER: Only trade when 1-min volatility is in sweet spot
     (too low = no moves; too high = random chop)
  4. MEAN-REVERSION GUARD: Skip if the move is already too extended
     (measured by z-score of dislocation vs rolling average)

TP/SL:
  - Asymmetric: TP = 1.2× distance, SL = 1.0× distance → positive expectancy
  - Dynamic: wider in high-vol, tighter in low-vol
"""

from __future__ import annotations

import collections
import logging
import math
import random
import statistics
import time

from config import TradingConfig
from models import (
    Order,
    OrderStatus,
    OrderType,
    Side,
    TradeRecord,
)
from risk_manager import RiskManager
from variational_client import VariationalClient
from price_feed import PriceFeed

logger = logging.getLogger("variational_bot.strategy")


# ═════════════════════════════════════════════════════════════
#  Signal Engine
# ═════════════════════════════════════════════════════════════

class SignalEngine:
    """
    Collects Binance + Variational prices and produces a directional signal.

    Maintains rolling windows of prices for momentum, volatility, and
    dislocation calculations.
    """

    def __init__(
        self,
        lookback: int = 30,         # ticks to keep (at ~1s polling = 30s window)
        dislocation_threshold_bps: float = 0.5,  # min bps divergence to act
        momentum_window: int = 8,   # ticks for momentum confirmation
        momentum_min_bps: float = 0.3,  # min momentum in bps
        vol_min_bps: float = 0.5,   # min 1-min volatility (bps) to trade
        vol_max_bps: float = 15.0,  # max 1-min volatility (bps) — too choppy
        zscore_max: float = 2.5,    # max z-score of dislocation (mean-rev guard)
    ):
        self._lookback = lookback
        self._dislocation_thresh = dislocation_threshold_bps
        self._momentum_window = momentum_window
        self._momentum_min = momentum_min_bps
        self._vol_min = vol_min_bps
        self._vol_max = vol_max_bps
        self._zscore_max = zscore_max

        # Rolling price buffers
        self._binance_prices: collections.deque[float] = collections.deque(maxlen=lookback)
        self._var_prices: collections.deque[float] = collections.deque(maxlen=lookback)
        self._dislocations: collections.deque[float] = collections.deque(maxlen=lookback * 3)
        self._timestamps: collections.deque[float] = collections.deque(maxlen=lookback)

    def update(self, binance_mid: float, var_mid: float) -> None:
        """Feed a new price pair into the engine."""
        self._binance_prices.append(binance_mid)
        self._var_prices.append(var_mid)
        self._timestamps.append(time.time())

        # Dislocation in bps: positive = Binance above Variational
        if var_mid > 0:
            disloc = (binance_mid - var_mid) / var_mid * 10000  # bps
            self._dislocations.append(disloc)

    def get_signal(self) -> tuple[Side | None, float, dict]:
        """
        Analyze accumulated data and return a trading signal.

        Returns:
            (direction, confidence, debug_info)
            direction: Side.LONG, Side.SHORT, or None (no trade)
            confidence: 0.0-1.0 strength of signal
            debug_info: dict with signal components for logging
        """
        info: dict = {}

        # Need minimum data
        if len(self._binance_prices) < self._momentum_window + 2:
            return None, 0.0, {"reason": "warming_up", "ticks": len(self._binance_prices)}

        # ── 1. Price Dislocation ──
        current_disloc = self._dislocations[-1] if self._dislocations else 0.0
        info["dislocation_bps"] = round(current_disloc, 3)

        # ── 2. Micro-Momentum (Binance direction over last N ticks) ──
        recent_bin = list(self._binance_prices)[-self._momentum_window:]
        momentum_bps = (recent_bin[-1] - recent_bin[0]) / recent_bin[0] * 10000
        info["momentum_bps"] = round(momentum_bps, 3)

        # ── 3. Volatility (std of returns over window) ──
        if len(self._binance_prices) >= 10:
            prices = list(self._binance_prices)[-20:]
            returns = [(prices[i] - prices[i-1]) / prices[i-1] * 10000
                       for i in range(1, len(prices))]
            vol_bps = statistics.stdev(returns) if len(returns) > 1 else 0.0
        else:
            vol_bps = 0.0
        info["volatility_bps"] = round(vol_bps, 3)

        # ── 4. Z-score of dislocation (mean-reversion guard) ──
        if len(self._dislocations) >= 10:
            mean_d = statistics.mean(self._dislocations)
            std_d = statistics.stdev(self._dislocations)
            zscore = (current_disloc - mean_d) / std_d if std_d > 0 else 0.0
        else:
            zscore = 0.0
        info["zscore"] = round(zscore, 3)

        # ═══ Decision Logic ═══

        # Volatility filter
        if vol_bps < self._vol_min:
            info["reason"] = "vol_too_low"
            return None, 0.0, info
        if vol_bps > self._vol_max:
            info["reason"] = "vol_too_high"
            return None, 0.0, info

        # Mean-reversion guard: don't chase extreme dislocations
        if abs(zscore) > self._zscore_max:
            info["reason"] = "zscore_extreme"
            return None, 0.0, info

        # Determine direction from dislocation + momentum agreement
        disloc_long = current_disloc > self._dislocation_thresh   # Binance above Var
        disloc_short = current_disloc < -self._dislocation_thresh  # Binance below Var
        mom_long = momentum_bps > self._momentum_min
        mom_short = momentum_bps < -self._momentum_min

        if disloc_long and mom_long:
            # Strong: dislocation AND momentum both say LONG
            confidence = min(1.0, (abs(current_disloc) / 3.0 + abs(momentum_bps) / 3.0) / 2)
            info["reason"] = "disloc+momentum_LONG"
            return Side.LONG, confidence, info

        elif disloc_short and mom_short:
            confidence = min(1.0, (abs(current_disloc) / 3.0 + abs(momentum_bps) / 3.0) / 2)
            info["reason"] = "disloc+momentum_SHORT"
            return Side.SHORT, confidence, info

        elif disloc_long and not mom_short:
            # Weaker: dislocation says long, momentum neutral (not opposing)
            confidence = min(0.6, abs(current_disloc) / 4.0)
            info["reason"] = "disloc_LONG"
            return Side.LONG, confidence, info

        elif disloc_short and not mom_long:
            confidence = min(0.6, abs(current_disloc) / 4.0)
            info["reason"] = "disloc_SHORT"
            return Side.SHORT, confidence, info

        info["reason"] = "no_signal"
        return None, 0.0, info


# ═════════════════════════════════════════════════════════════
#  Smart Strategy
# ═════════════════════════════════════════════════════════════

class SmartStrategy:
    """
    Cross-exchange latency strategy with signal-based entries.

    Lifecycle: warmup → signal scan → entry → monitor → exit → record.
    Falls back to random if no signal appears within timeout.
    """

    def __init__(
        self,
        client: VariationalClient,
        price_feed: PriceFeed,
        risk_mgr: RiskManager,
        cfg: TradingConfig,
        market_id: str,
    ) -> None:
        self._client = client
        self._price = price_feed
        self._risk = risk_mgr
        self._cfg = cfg
        self._market_id = market_id

        self._signal_engine = SignalEngine(
            lookback=30,
            dislocation_threshold_bps=0.5,
            momentum_window=8,
            momentum_min_bps=0.3,
            vol_min_bps=0.5,
            vol_max_bps=15.0,
            zscore_max=2.5,
        )

        # Stats
        self._signal_trades = 0
        self._fallback_trades = 0

    # ─────────────────────────────────────────────────────────
    #  Full trade cycle
    # ─────────────────────────────────────────────────────────

    def execute_trade_cycle(self) -> TradeRecord | None:
        """
        Run one complete trade cycle:
          1. Scan for signal (up to signal_timeout seconds)
          2. Enter position
          3. Monitor with asymmetric TP/SL
          4. Close and record
        """
        # ── Step 1: Wait for signal ──
        side, confidence, signal_info = self._wait_for_signal(timeout=45.0)

        is_signal = side is not None
        if side is None:
            # Fallback: random direction (still prints volume, ~50% WR)
            side = random.choice([Side.LONG, Side.SHORT])
            confidence = 0.0
            self._fallback_trades += 1
            logger.info(
                "=== NEW CYCLE (RANDOM) === Direction: %s  (no signal after timeout)",
                side.value.upper(),
            )
        else:
            self._signal_trades += 1
            logger.info(
                "=== NEW CYCLE (SIGNAL) === Direction: %s  confidence=%.2f  reason=%s",
                side.value.upper(), confidence, signal_info.get("reason", "?"),
            )
            logger.info(
                "  Signal details: disloc=%.3f bps  momentum=%.3f bps  vol=%.3f bps  z=%.2f",
                signal_info.get("dislocation_bps", 0),
                signal_info.get("momentum_bps", 0),
                signal_info.get("volatility_bps", 0),
                signal_info.get("zscore", 0),
            )

        # ── Step 2: Position sizing ──
        account = self._client.get_account_info()
        size = self._risk.compute_position_size(account)
        leverage = self._risk.compute_leverage()

        # ── Step 3: Get quote and enter ──
        side_str = "buy" if side == Side.LONG else "sell"
        rough_price = 80000.0
        try:
            rough_quote = self._client.get_quote(self._market_id)
            rough_price = rough_quote.mid_price or 80000.0
        except Exception:
            pass
        qty_str = f"{size / rough_price:.6f}"

        quote = self._client.get_quote(self._market_id, qty=qty_str, side=side_str)
        bid, ask, mid = quote.bid_price, quote.ask_price, quote.mid_price

        logger.info(
            "Quote: bid=$%.2f  ask=$%.2f  mid=$%.2f  qid=%s",
            bid, ask, mid, quote.quote_id[:12],
        )

        # Always market order (we need speed for latency arb)
        entry_order = self._client.place_market_order(
            market_id=self._market_id,
            side=side,
            size_usdc=size,
            price=mid,
            quote_id=quote.quote_id,
        )

        if entry_order.status != OrderStatus.FILLED:
            logger.warning("Entry order not filled — aborting cycle")
            return None

        entry_price = entry_order.fill_price or mid
        entry_time = entry_order.filled_at or time.time()

        logger.info(
            "ENTRY MARKET @ $%.2f  size=$%.2f  lev=%.0fx  (%s)",
            entry_price, size, leverage, "SIGNAL" if is_signal else "RANDOM",
        )

        # ── Step 4: Compute ASYMMETRIC TP/SL ──
        #   Signal trades: TP = 1.3× distance, SL = 1.0× (positive EV)
        #   Random trades: TP = SL = 1.0× (neutral)
        base_dist = self._cfg.tp_sl_distance_pct

        if is_signal and confidence > 0.3:
            tp_mult = 1.3  # let winners run a bit more
            sl_mult = 1.0
        else:
            tp_mult = 1.0
            sl_mult = 1.0

        if side == Side.LONG:
            tp_price = entry_price * (1.0 + base_dist * tp_mult)
            sl_price = entry_price * (1.0 - base_dist * sl_mult)
        else:
            tp_price = entry_price * (1.0 - base_dist * tp_mult)
            sl_price = entry_price * (1.0 + base_dist * sl_mult)

        logger.info(
            "TP=$%.2f (%.3f%%)  SL=$%.2f (%.3f%%)  [%s]",
            tp_price, base_dist * tp_mult * 100,
            sl_price, base_dist * sl_mult * 100,
            "asymmetric" if tp_mult != sl_mult else "symmetric",
        )

        # ── Step 5: Monitor and close ──
        outcome, exit_price = self._monitor_and_close(
            side, entry_price, tp_price, sl_price, size
        )

        closed_at = time.time()
        duration = closed_at - entry_time
        pnl = self._calculate_pnl(side, entry_price, exit_price, size)

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
            "PnL=$%+.4f  time=%.1fs  [%s]",
            outcome, entry_price, exit_price, pnl, duration,
            "SIGNAL" if is_signal else "RANDOM",
        )
        logger.info(
            "📈 Signal stats: %d signal trades / %d random trades",
            self._signal_trades, self._fallback_trades,
        )

        self._risk.record_trade(pnl)
        return record

    # ─────────────────────────────────────────────────────────
    #  Signal scanning
    # ─────────────────────────────────────────────────────────

    def _wait_for_signal(
        self, timeout: float = 45.0
    ) -> tuple[Side | None, float, dict]:
        """
        Continuously poll Binance + Variational and feed the signal engine.
        Return as soon as a valid signal appears, or None after timeout.
        """
        deadline = time.time() + timeout
        tick = 0

        while time.time() < deadline:
            tick += 1
            try:
                # Fetch both prices ~simultaneously
                binance_quote = self._price.get_quote()
                var_quote = self._client.get_quote(self._market_id)

                binance_mid = binance_quote.mid_price
                var_mid = var_quote.mid_price

                # Feed the engine
                self._signal_engine.update(binance_mid, var_mid)

                # Check for signal
                direction, confidence, info = self._signal_engine.get_signal()

                if tick % 10 == 1:
                    logger.debug(
                        "Signal scan #%d: Bin=$%.2f  Var=$%.2f  diff=%.1f bps  → %s",
                        tick, binance_mid, var_mid,
                        info.get("dislocation_bps", 0),
                        info.get("reason", "?"),
                    )

                if direction is not None:
                    return direction, confidence, info

            except Exception as exc:
                logger.debug("Signal scan error: %s", exc)

            time.sleep(0.8)  # slightly less than 1s for better resolution

        return None, 0.0, {"reason": "timeout"}

    # ─────────────────────────────────────────────────────────
    #  Monitor and close (same robust logic as before)
    # ─────────────────────────────────────────────────────────

    def _monitor_and_close(
        self,
        side: Side,
        entry_price: float,
        tp_price: float,
        sl_price: float,
        size_usdc: float,
    ) -> tuple[str, float]:
        """
        Poll Variational quotes. Close at TP or SL using exact position qty.
        """
        close_side = Side.SHORT if side == Side.LONG else Side.LONG

        logger.info(
            "Monitoring: TP=$%.2f  SL=$%.2f  (entry=$%.2f, %s)",
            tp_price, sl_price, entry_price, side.value.upper(),
        )

        check_count = 0
        pending_outcome: str | None = None
        current = entry_price  # track last known price

        while True:
            check_count += 1
            try:
                quote = self._client.get_quote(self._market_id)
                current = quote.mid_price

                if check_count % 10 == 1:
                    logger.debug(
                        "Price #%d: $%.2f  (TP=$%.2f  SL=$%.2f)",
                        check_count, current, tp_price, sl_price,
                    )

                # Also feed signal engine during monitoring (builds data for next cycle)
                try:
                    bin_q = self._price.get_quote()
                    self._signal_engine.update(bin_q.mid_price, current)
                except Exception:
                    pass

                hit = self._check_tp_sl_hit(side, current, tp_price, sl_price)
                if hit is not None or pending_outcome is not None:
                    if hit is not None:
                        pending_outcome, _ = hit

                    if hit is not None:
                        logger.info("%s at $%.2f -- closing", pending_outcome, current)

                    # Get exact position qty
                    positions = self._client.get_open_positions()
                    raw_qty = ""
                    actual_qty_usdc = size_usdc
                    for p in positions:
                        if p.symbol == f"{self._market_id}-USDC":
                            raw_qty = p.raw_qty
                            actual_qty_usdc = p.size_usdc
                            break

                    if not raw_qty:
                        logger.warning("Position not found — assuming closed")
                        return pending_outcome or "TP_HIT", current

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
                    return pending_outcome or "TP_HIT", exit_price

            except Exception as exc:
                exc_str = str(exc)
                logger.warning("Monitoring error: %s", exc)

                if "418" in exc_str or "banned" in exc_str:
                    import re
                    wait = 30
                    m = re.search(r'"wait_until_seconds"\s*:\s*(\d+)', exc_str)
                    if m:
                        wait = int(m.group(1)) + 2
                    logger.warning("Rate-limited — waiting %ds", wait)
                    time.sleep(wait)
                    continue

                if "exceeds position size" in exc_str:
                    time.sleep(3)
                    continue

                # Position already closed externally
                if "No position exists" in exc_str:
                    logger.info("Position already closed — exiting monitor")
                    return pending_outcome or "TP_HIT", current

            time.sleep(self._cfg.poll_interval)

    @staticmethod
    def _check_tp_sl_hit(
        side: Side, current: float, tp: float, sl: float,
    ) -> tuple[str, float] | None:
        if side == Side.LONG:
            if current >= tp:
                return "TP_HIT", current
            if current <= sl:
                return "SL_HIT", current
        else:
            if current <= tp:
                return "TP_HIT", current
            if current >= sl:
                return "SL_HIT", current
        return None

    @staticmethod
    def _calculate_pnl(
        side: Side, entry: float, exit_: float, size_usdc: float,
    ) -> float:
        """PnL calc. size_usdc is NOTIONAL."""
        if entry == 0:
            return 0.0
        if side == Side.LONG:
            return round(size_usdc * (exit_ - entry) / entry, 6)
        else:
            return round(size_usdc * (entry - exit_) / entry, 6)
