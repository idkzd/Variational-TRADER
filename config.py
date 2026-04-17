"""
Configuration module for the Variational Omni Trading Bot.

Loads settings from two sources:
  1. config.txt  — all tunable bot parameters (trading, timing, logging)
  2. .env         — secrets only (cookies, tokens, private keys)
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

# Load .env for secrets
load_dotenv()

# ─────────────────────────────────────────────────────────────
#  Parse config.txt
# ─────────────────────────────────────────────────────────────

_CONFIG_PATH = Path(__file__).parent / "config.txt"
_settings: dict[str, str] = {}


def _load_config_txt() -> dict[str, str]:
    """Parse config.txt into a key-value dict. Ignores comments and blanks."""
    result: dict[str, str] = {}
    if not _CONFIG_PATH.exists():
        return result
    for line in _CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip inline comments
        if "  #" in value:
            value = value[: value.index("  #")].strip()
        result[key] = value
    return result


_settings = _load_config_txt()


def _get(key: str, default: str = "") -> str:
    """Get a config value: config.txt first, then env var, then default."""
    return _settings.get(key, os.getenv(key.upper(), default))


def _getf(key: str, default: float = 0.0) -> float:
    """Get a float config value."""
    raw = _get(key, str(default))
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default


def _geti(key: str, default: int = 0) -> int:
    """Get an int config value."""
    raw = _get(key, str(default))
    try:
        return int(float(raw))
    except (ValueError, TypeError):
        return default


# ─────────────────────────────────────────────────────────────
#  Dataclasses
# ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class WalletConfig:
    """Wallet and auth — loaded from .env only (secrets)."""
    private_key: str = field(default_factory=lambda: os.getenv("PRIVATE_KEY", ""))
    wallet_address: str = field(default_factory=lambda: os.getenv("WALLET_ADDRESS", ""))
    session_cookie: str = field(default_factory=lambda: os.getenv("VARIATIONAL_COOKIE", ""))
    auth_token: str = field(default_factory=lambda: os.getenv("VARIATIONAL_AUTH_TOKEN", ""))


@dataclass(frozen=True)
class ApiConfig:
    """API endpoints and request settings."""
    base_url: str = field(
        default_factory=lambda: os.getenv("VARIATIONAL_BASE_URL", "https://omni.variational.io/api")
    )

    # Endpoints (not user-configurable)
    endpoint_portfolio: str = "/portfolio"
    endpoint_positions: str = "/positions"
    endpoint_quote: str = "/quotes/indicative"
    endpoint_order_market: str = "/orders/new/market"
    endpoint_order_limit: str = "/orders/new/limit"
    endpoint_orders: str = "/orders/v2"
    endpoint_tpsl: str = "/tpsl"
    endpoint_leverage: str = "/settlement_pools/leverage"
    endpoint_supported_assets: str = "/metadata/supported_assets"

    request_timeout: int = 30
    max_retries: int = 3
    retry_delay: float = 2.0


@dataclass(frozen=True)
class TradingConfig:
    """Core trading parameters — loaded from config.txt."""
    symbol: str = field(default_factory=lambda: _get("symbol", "BTC-USDC"))
    position_size_usdc: float = field(default_factory=lambda: _getf("position_size_usdc", 10.0))
    leverage: float = field(default_factory=lambda: _getf("leverage", 2.0))
    tp_sl_distance_pct: float = field(default_factory=lambda: _getf("tp_sl_distance_pct", 0.003))
    market_spread_threshold: float = field(default_factory=lambda: _getf("market_spread_threshold", 0.02))
    min_balance_usdc: float = field(default_factory=lambda: _getf("min_balance_usdc", 5.0))
    slippage_tolerance: float = field(default_factory=lambda: _getf("slippage_tolerance", 0.005))
    poll_interval: float = field(default_factory=lambda: _getf("poll_interval", 1.0))
    order_fill_timeout: float = field(default_factory=lambda: _getf("order_fill_timeout", 60.0))
    cycle_cooldown: float = field(default_factory=lambda: _getf("cycle_cooldown", 3.0))
    max_consecutive_errors: int = field(default_factory=lambda: _geti("max_consecutive_errors", 10))


@dataclass(frozen=True)
class PriceFeedConfig:
    """External price feed configuration."""
    binance_ticker_url: str = "https://api.binance.com/api/v3/ticker/bookTicker"
    binance_symbol: str = "BTCUSDC"
    coingecko_url: str = (
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=bitcoin&vs_currencies=usd"
    )
    timeout: int = 10


@dataclass(frozen=True)
class LoggingConfig:
    """Logging configuration — loaded from config.txt."""
    level: str = field(default_factory=lambda: _get("log_level", "INFO"))
    log_file: str = field(default_factory=lambda: _get("log_file", "variational_bot.log"))
    fmt: str = "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s"
    datefmt: str = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class BotConfig:
    """Top-level configuration aggregating all sub-configs."""
    wallet: WalletConfig = field(default_factory=WalletConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    price_feed: PriceFeedConfig = field(default_factory=PriceFeedConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    def validate(self) -> list[str]:
        """Return a list of configuration issues (empty = all good)."""
        issues: list[str] = []

        if not self.wallet.private_key and not self.wallet.auth_token and not self.wallet.session_cookie:
            issues.append(
                "Set at least one of: VARIATIONAL_COOKIE, VARIATIONAL_AUTH_TOKEN, or PRIVATE_KEY in .env"
            )

        if not self.wallet.wallet_address and not self.wallet.session_cookie:
            issues.append("WALLET_ADDRESS must be set in .env (or use VARIATIONAL_COOKIE).")

        if self.trading.leverage < 1 or self.trading.leverage > 50:
            issues.append("leverage must be between 1 and 50.")

        if self.trading.position_size_usdc <= 0:
            issues.append("position_size_usdc must be positive.")

        if self.trading.tp_sl_distance_pct <= 0 or self.trading.tp_sl_distance_pct > 0.1:
            issues.append("tp_sl_distance_pct must be between 0.0001 and 0.1.")

        return issues
