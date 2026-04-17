"""
Custom exceptions for the Variational Omni Trading Bot.

Centralised exception hierarchy makes error handling precise and readable.
"""


class BotError(Exception):
    """Base exception for all bot errors."""
    pass


# ── API / Network Errors ──────────────────────────────────────

class ApiError(BotError):
    """Raised when the Variational API returns an unexpected response."""

    def __init__(self, message: str, status_code: int | None = None, body: str = ""):
        self.status_code = status_code
        self.body = body
        super().__init__(f"API Error (HTTP {status_code}): {message}")


class RateLimitError(ApiError):
    """Raised when we hit Variational's rate-limit (HTTP 429)."""

    def __init__(self, retry_after: float = 60.0):
        self.retry_after = retry_after
        super().__init__(
            f"Rate limited — retry after {retry_after}s",
            status_code=429,
        )


class AuthenticationError(ApiError):
    """Raised when authentication/session has expired."""

    def __init__(self):
        super().__init__("Authentication failed — token may be expired", status_code=401)


# ── Order Errors ──────────────────────────────────────────────

class OrderError(BotError):
    """Base class for order-related errors."""
    pass


class OrderNotFilledError(OrderError):
    """Raised when a limit order was not filled within the timeout window."""

    def __init__(self, order_id: str, timeout: float):
        self.order_id = order_id
        self.timeout = timeout
        super().__init__(
            f"Order {order_id} was not filled within {timeout}s — cancelled."
        )


class OrderRejectedError(OrderError):
    """Raised when the exchange rejects an order."""

    def __init__(self, order_id: str, reason: str = "unknown"):
        self.order_id = order_id
        self.reason = reason
        super().__init__(f"Order {order_id} rejected: {reason}")


# ── Risk / Balance Errors ─────────────────────────────────────

class InsufficientBalanceError(BotError):
    """Raised when account balance falls below the minimum threshold."""

    def __init__(self, balance: float, threshold: float):
        self.balance = balance
        self.threshold = threshold
        super().__init__(
            f"Balance ${balance:.2f} is below minimum threshold ${threshold:.2f}"
        )


# ── Price Feed Errors ─────────────────────────────────────────

class PriceFeedError(BotError):
    """Raised when we cannot obtain a reliable price from external feeds."""

    def __init__(self, source: str, reason: str = ""):
        self.source = source
        super().__init__(f"Price feed error from {source}: {reason}")
