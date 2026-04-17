"""
External price feed for the Variational Omni Trading Bot.

Fetches the current BTC-USDC mid-price from Binance (primary) with
CoinGecko as a fallback.  Used to determine limit order prices when
Variational's own quote is unavailable.
"""

from __future__ import annotations

import logging
import time

import httpx

from config import PriceFeedConfig
from exceptions import PriceFeedError
from models import Quote

logger = logging.getLogger("variational_bot.price_feed")


class PriceFeed:
    """
    Fetches real-time BTC/USDC pricing from public APIs.

    Primary:  Binance bookTicker (best bid/ask — no auth required).
    Fallback: CoinGecko simple/price (single mid-price, no spread info).
    """

    def __init__(self, cfg: PriceFeedConfig) -> None:
        self._cfg = cfg
        self._http = httpx.Client(timeout=cfg.timeout)
        self._last_quote: Quote | None = None
        self._last_fetch_time: float = 0.0

    # ── Public interface ──────────────────────────────────────

    def get_mid_price(self) -> float:
        """
        Return the current BTC-USDC mid-price.

        Tries Binance first; falls back to CoinGecko on failure.

        Returns:
            Mid-price as a float.

        Raises:
            PriceFeedError: If all sources fail.
        """
        quote = self.get_quote()
        return quote.mid_price

    def get_quote(self) -> Quote:
        """
        Return a full Quote (bid, ask, mid) for BTC-USDC.

        Raises:
            PriceFeedError: If all sources fail.
        """
        try:
            return self._fetch_binance()
        except Exception as exc:
            logger.warning("Binance feed failed (%s), trying CoinGecko…", exc)

        try:
            return self._fetch_coingecko()
        except Exception as exc:
            logger.error("CoinGecko feed also failed: %s", exc)
            raise PriceFeedError("all_sources", "Both Binance and CoinGecko failed")

    # ── Private: Binance ──────────────────────────────────────

    def _fetch_binance(self) -> Quote:
        """
        Fetch best bid/ask from Binance's public bookTicker endpoint.

        Endpoint: GET /api/v3/ticker/bookTicker?symbol=BTCUSDC
        Response: {"symbol":"BTCUSDC","bidPrice":"67000.00","askPrice":"67001.00",...}
        """
        url = self._cfg.binance_ticker_url
        params = {"symbol": self._cfg.binance_symbol}

        response = self._http.get(url, params=params)
        response.raise_for_status()
        data = response.json()

        bid = float(data["bidPrice"])
        ask = float(data["askPrice"])

        quote = Quote.from_bid_ask(bid, ask)
        self._last_quote = quote
        self._last_fetch_time = time.time()

        logger.debug(
            "Binance BTC-USDC  bid=%.2f  ask=%.2f  mid=%.2f",
            quote.bid_price, quote.ask_price, quote.mid_price,
        )
        return quote

    # ── Private: CoinGecko (fallback) ─────────────────────────

    def _fetch_coingecko(self) -> Quote:
        """
        Fetch a single price from CoinGecko (no spread info available).

        The bid and ask are set equal to the price, so mid == price.
        """
        response = self._http.get(self._cfg.coingecko_url)
        response.raise_for_status()
        data = response.json()

        price = float(data["bitcoin"]["usd"])

        # CoinGecko only gives one price — approximate a tiny spread
        spread = price * 0.0001  # 0.01% synthetic spread
        quote = Quote.from_bid_ask(price - spread / 2, price + spread / 2)
        self._last_quote = quote
        self._last_fetch_time = time.time()

        logger.debug("CoinGecko BTC price=%.2f (synthetic spread)", price)
        return quote

    # ── Cleanup ───────────────────────────────────────────────

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()
