"""
Data models used throughout the Variational Omni Trading Bot.

These Pydantic-style dataclasses represent orders, positions, and market data.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ─────────────────────────────────────────────────────────────
#  Enumerations
# ─────────────────────────────────────────────────────────────

class Side(str, Enum):
    """Trade direction."""
    LONG = "long"
    SHORT = "short"


class OrderType(str, Enum):
    """Order type."""
    MARKET = "market"
    LIMIT = "limit"
    TRIGGER = "trigger"          # used for TP/SL


class OrderStatus(str, Enum):
    """Lifecycle status of an order."""
    PENDING = "pending"          # submitted, awaiting fill
    FILLED = "filled"            # fully executed
    CANCELLED = "cancelled"      # cancelled by user or timeout
    REJECTED = "rejected"        # rejected by the exchange
    EXPIRED = "expired"          # expired without fill


class PositionStatus(str, Enum):
    """Lifecycle status of a position."""
    OPEN = "open"
    CLOSED = "closed"
    LIQUIDATED = "liquidated"


# ─────────────────────────────────────────────────────────────
#  Data Classes
# ─────────────────────────────────────────────────────────────

@dataclass
class MarketInfo:
    """Static information about a tradable market on Omni."""
    market_id: str               # internal Variational market identifier
    symbol: str                  # e.g. "BTC-USDC"
    base_asset: str              # e.g. "BTC"
    quote_asset: str             # e.g. "USDC"
    min_order_size: float        # minimum notional in USDC
    max_leverage: float          # maximum leverage allowed


@dataclass
class Quote:
    """Current best bid/ask from the OLP (Omni Liquidity Provider)."""
    bid_price: float             # best bid (price to sell)
    ask_price: float             # best ask (price to buy)
    mid_price: float             # (bid + ask) / 2
    quote_id: str = ""           # server-assigned quote ID (required for orders)
    timestamp: float = field(default_factory=time.time)

    @classmethod
    def from_bid_ask(cls, bid: float, ask: float, quote_id: str = "") -> "Quote":
        """Construct a Quote from raw bid/ask prices."""
        return cls(
            bid_price=bid,
            ask_price=ask,
            mid_price=(bid + ask) / 2.0,
            quote_id=quote_id,
        )


@dataclass
class Order:
    """Represents a single order submitted to Variational Omni."""
    order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    symbol: str = ""
    side: Side = Side.LONG
    order_type: OrderType = OrderType.LIMIT
    size_usdc: float = 0.0       # notional size in USDC
    price: float = 0.0           # limit price or trigger price
    leverage: float = 1.0
    status: OrderStatus = OrderStatus.PENDING
    fill_price: Optional[float] = None
    slippage_tolerance: float = 0.005
    created_at: float = field(default_factory=time.time)
    filled_at: Optional[float] = None
    error_message: Optional[str] = None

    # ── Variational-specific fields ──
    remote_order_id: Optional[str] = None   # ID returned by Variational API

    @property
    def is_terminal(self) -> bool:
        """Returns True if the order is in a final state."""
        return self.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        )


@dataclass
class TpSlPair:
    """Paired Take-Profit and Stop-Loss orders attached to a position."""
    tp_order: Order
    sl_order: Order
    entry_price: float           # price at which the parent position was entered
    tp_distance_pct: float       # e.g. 0.003 for 0.3%
    sl_distance_pct: float       # e.g. 0.003 for 0.3%


@dataclass
class Position:
    """Represents an open (or closed) position on Variational Omni."""
    position_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    symbol: str = ""
    side: Side = Side.LONG
    entry_price: float = 0.0
    size_usdc: float = 0.0
    leverage: float = 1.0
    status: PositionStatus = PositionStatus.OPEN
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    tp_sl: Optional[TpSlPair] = None
    opened_at: float = field(default_factory=time.time)
    closed_at: Optional[float] = None

    # ── Variational-specific fields ──
    remote_position_id: Optional[str] = None
    raw_qty: str = ""  # exact BTC qty string from API (e.g. "0.006451")

    @property
    def is_open(self) -> bool:
        return self.status == PositionStatus.OPEN


@dataclass
class AccountInfo:
    """Snapshot of the trading account on Variational Omni."""
    balance_usdc: float = 0.0
    equity_usdc: float = 0.0
    unrealized_pnl: float = 0.0
    available_margin: float = 0.0
    open_position_count: int = 0
    total_volume_usdc: float = 0.0


@dataclass
class TradeRecord:
    """Immutable log entry for a completed trade cycle."""
    trade_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    symbol: str = ""
    side: Side = Side.LONG
    entry_price: float = 0.0
    exit_price: float = 0.0
    size_usdc: float = 0.0
    leverage: float = 1.0
    pnl_usdc: float = 0.0
    outcome: str = ""            # "TP_HIT", "SL_HIT", "MANUAL_CLOSE", "LIQUIDATED"
    duration_seconds: float = 0.0
    opened_at: float = 0.0
    closed_at: float = field(default_factory=time.time)
