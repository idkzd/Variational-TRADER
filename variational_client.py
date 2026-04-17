"""
Variational Omni API Client.

Uses curl_cffi to impersonate Chrome's TLS fingerprint, which is required
to pass Cloudflare's bot protection on omni.variational.io.

Verified endpoints (April 2026):
  GET  /portfolio                     -> { balance, upnl }
  GET  /positions                     -> [ { position_info, price_info, value, upnl, ... } ]
  POST /quotes/indicative             -> { bid, ask, mark_price, index_price, ... }
  POST /orders/new/market             -> order result
  POST /orders/new/limit              -> order result (assumed)
  GET  /orders/v2                     -> { pagination, result: [ orders ] }
  GET  /orders/v2?status=pending      -> pending orders
  POST /settlement_pools/leverage     -> { "BTC": { current, limits } }
  GET  /metadata/supported_assets     -> { "BTC": [...], ... }
  POST /tpsl                          -> TP/SL submission
  GET  /ping                          -> health check (POST only)

Instrument format:
  {
    "instrument_type": "perpetual_future",
    "underlying": "BTC",
    "funding_interval_s": 3600,
    "settlement_asset": "USDC"
  }
"""

from __future__ import annotations

import logging
import time
from typing import Any

from curl_cffi import requests as curl_requests

from config import ApiConfig, WalletConfig
from exceptions import (
    ApiError,
    AuthenticationError,
    OrderRejectedError,
    RateLimitError,
)
from models import (
    AccountInfo,
    MarketInfo,
    Order,
    OrderStatus,
    OrderType,
    Position,
    PositionStatus,
    Quote,
    Side,
)

logger = logging.getLogger("variational_bot.api_client")

# ─────────────────────────────────────────────────────────────
#  Instrument definition for BTC-USDC perpetual on Omni
# ─────────────────────────────────────────────────────────────

BTC_USDC_INSTRUMENT: dict[str, Any] = {
    "instrument_type": "perpetual_future",
    "underlying": "BTC",
    "funding_interval_s": 3600,
    "settlement_asset": "USDC",
}


def make_instrument(underlying: str = "BTC", settlement: str = "USDC") -> dict[str, Any]:
    """Build the instrument object expected by the Variational API."""
    return {
        "instrument_type": "perpetual_future",
        "underlying": underlying,
        "funding_interval_s": 3600,
        "settlement_asset": settlement,
    }


class VariationalClient:
    """
    HTTP client for the Variational Omni exchange.

    Uses curl_cffi with Chrome TLS impersonation to bypass Cloudflare.
    All methods return typed model objects defined in models.py.
    """

    def __init__(self, api_cfg: ApiConfig, wallet_cfg: WalletConfig) -> None:
        self._api = api_cfg
        self._wallet = wallet_cfg
        self._headers = self._build_headers()
        logger.info("VariationalClient initialised (base_url=%s)", api_cfg.base_url)

    # ─────────────────────────────────────────────────────────
    #  Headers / Auth
    # ─────────────────────────────────────────────────────────

    def _build_headers(self) -> dict[str, str]:
        """
        Construct HTTP headers mimicking the real browser session.

        Auth is done via:
          - Cookie header (includes cf_clearance + vr-token + vr-connected-address)
          - Authorization: Bearer <vr-token>
          - Vr-Connected-Address header
        """
        headers: dict[str, str] = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://omni.variational.io",
            "Referer": "https://omni.variational.io/perpetual/BTC",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

        # ── Cookie-based auth (primary) ──
        if self._wallet.session_cookie:
            headers["Cookie"] = self._wallet.session_cookie

            # Extract vr-token → Bearer
            vr_token = self._extract_cookie_value(
                self._wallet.session_cookie, "vr-token"
            )
            if vr_token:
                headers["Authorization"] = f"Bearer {vr_token}"

            # Extract wallet address from cookie
            vr_addr = self._extract_cookie_value(
                self._wallet.session_cookie, "vr-connected-address"
            )
            if vr_addr:
                headers["Vr-Connected-Address"] = vr_addr

        # ── Explicit overrides ──
        if self._wallet.auth_token:
            headers["Authorization"] = f"Bearer {self._wallet.auth_token}"

        if self._wallet.wallet_address:
            headers["Vr-Connected-Address"] = self._wallet.wallet_address

        return headers

    @staticmethod
    def _extract_cookie_value(cookie_string: str, name: str) -> str:
        """Extract a single cookie value by name from a cookie header string."""
        for part in cookie_string.split(";"):
            part = part.strip()
            if part.startswith(f"{name}="):
                return part[len(name) + 1:]
        return ""

    # ─────────────────────────────────────────────────────────
    #  Low-level request (curl_cffi + Chrome impersonation)
    # ─────────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Execute an HTTP request via curl_cffi with Cloudflare bypass.

        Args:
            method:    HTTP verb (GET, POST, DELETE).
            path:      URL path relative to base_url (e.g. "/portfolio").
            params:    Query string parameters.
            json_body: JSON request body.

        Returns:
            Parsed JSON response as a dict.
            If the API returns a bare list, it's wrapped as {"_list": [...]}.

        Raises:
            AuthenticationError: On 401.
            RateLimitError:      On 429.
            ApiError:            On 403 (Cloudflare) or any other non-2xx.
        """
        url = f"{self._api.base_url}{path}"
        last_exc: Exception | None = None

        for attempt in range(1, self._api.max_retries + 1):
            try:
                response = curl_requests.request(
                    method,
                    url,
                    headers=self._headers,
                    params=params,
                    json=json_body,
                    timeout=self._api.request_timeout,
                    impersonate="chrome",
                )

                # ── Handle error status codes ──
                if response.status_code == 401:
                    raise AuthenticationError()
                if response.status_code == 429:
                    retry_after = float(
                        response.headers.get("Retry-After", "60")
                    )
                    raise RateLimitError(retry_after=retry_after)
                if response.status_code == 403:
                    # Cloudflare challenge — cookies have expired
                    raise ApiError(
                        message=(
                            "Cloudflare 403 — cookies expired. "
                            "Re-login in browser and update VARIATIONAL_COOKIE in .env"
                        ),
                        status_code=403,
                    )
                if response.status_code >= 400:
                    raise ApiError(
                        message=response.text[:500],
                        status_code=response.status_code,
                        body=response.text,
                    )

                # ── Parse JSON ──
                data = response.json()

                # The API sometimes returns bare lists (e.g. /positions)
                if isinstance(data, list):
                    return {"_list": data}
                return data

            except (RateLimitError, AuthenticationError):
                raise  # don't retry these

            except ApiError:
                raise  # don't retry API errors (4xx)

            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Request %s %s failed (attempt %d/%d): %s",
                    method, path, attempt, self._api.max_retries, exc,
                )
                if attempt < self._api.max_retries:
                    time.sleep(self._api.retry_delay * attempt)

        raise ApiError(
            f"All {self._api.max_retries} retries exhausted: {last_exc}"
        )

    # ─────────────────────────────────────────────────────────
    #  Account / Portfolio
    # ─────────────────────────────────────────────────────────

    def get_account_info(self) -> AccountInfo:
        """
        Fetch account balance and unrealized PnL.

        GET /portfolio -> { "balance": "19.79518781", "upnl": "-0.01403619" }
        """
        data = self._request("GET", "/portfolio")
        balance = float(data.get("balance", 0))
        upnl = float(data.get("upnl", 0))

        info = AccountInfo(
            balance_usdc=balance,
            equity_usdc=balance + upnl,
            unrealized_pnl=upnl,
            available_margin=balance + upnl,
            open_position_count=0,  # checked separately when needed
        )
        logger.info(
            "Account: balance=$%.4f  upnl=$%.4f  equity=$%.4f",
            info.balance_usdc,
            info.unrealized_pnl,
            info.equity_usdc,
        )
        return info

    # ─────────────────────────────────────────────────────────
    #  Quotes (bid/ask from OLP)
    # ─────────────────────────────────────────────────────────

    def get_quote(self, underlying: str = "BTC", qty: str = "0.0001", side: str = "buy") -> Quote:
        """
        Fetch an indicative quote (bid/ask/mark/index) from the OLP.

        POST /quotes/indicative
        Body: { "instrument": {...}, "side": "buy", "qty": "0.0001" }
        Response: { "bid", "ask", "mark_price", "index_price", "quote_id", ... }
        """
        instrument = make_instrument(underlying)

        data = self._request(
            "POST",
            "/quotes/indicative",
            json_body={
                "instrument": instrument,
                "side": side,
                "qty": qty,
            },
        )

        bid = float(data.get("bid", 0))
        ask = float(data.get("ask", 0))
        quote_id = str(data.get("quote_id", ""))

        quote = Quote.from_bid_ask(bid, ask, quote_id=quote_id)

        logger.debug(
            "Quote BTC: bid=$%.2f  ask=$%.2f  mid=$%.2f  quote_id=%s",
            bid, ask, quote.mid_price, quote_id[:16],
        )
        return quote

    # ─────────────────────────────────────────────────────────
    #  Market / Leverage Info
    # ─────────────────────────────────────────────────────────

    def get_market_info(self, symbol: str) -> MarketInfo:
        """
        Fetch leverage limits for a given underlying.

        POST /settlement_pools/leverage
        Body: { "assets": ["BTC"] }
        Response: { "BTC": { "current": "50", "limits": [...] } }
        """
        underlying = symbol.split("-")[0] if "-" in symbol else symbol

        data = self._request(
            "POST",
            "/settlement_pools/leverage",
            json_body={"assets": [underlying]},
        )

        asset_data = data.get(underlying, {})
        max_leverage = float(asset_data.get("current", 50))

        return MarketInfo(
            market_id=underlying,
            symbol=symbol,
            base_asset=underlying,
            quote_asset="USDC",
            min_order_size=1.0,
            max_leverage=max_leverage,
        )

    # ─────────────────────────────────────────────────────────
    #  Orders — Limit
    # ─────────────────────────────────────────────────────────

    def place_limit_order(
        self,
        market_id: str,
        side: Side,
        size_usdc: float,
        price: float,
        leverage: float,
    ) -> Order:
        """
        Submit a limit order to Variational Omni.

        POST /orders/new/limit
        Body (estimated from market order format):
        {
            "instrument": { ... },
            "side": "buy" | "sell",
            "qty": "0.000130",        <- in BTC, not USDC
            "limit_price": "77000.00",
            "slippage_limit": "0.005",
            "is_reduce_only": false,
            "tpsl": null
        }

        Args:
            market_id:  Underlying symbol (e.g. "BTC").
            side:       Side.LONG (buy) or Side.SHORT (sell).
            size_usdc:  Desired position size in USDC.
            price:      Limit price.
            leverage:   Leverage multiplier (used to compute qty).

        Returns:
            An Order object with remote_order_id populated.
        """
        # Convert USDC notional to BTC quantity
        qty_asset = size_usdc / price
        qty_str = f"{qty_asset:.6f}"
        side_str = "buy" if side == Side.LONG else "sell"

        instrument = make_instrument(market_id)

        # Get fresh quote_id
        q = self.get_quote(market_id, qty=qty_str, side=side_str)

        payload = {
            "instrument": instrument,
            "side": side_str,
            "qty": qty_str,
            "quote_id": q.quote_id,
            "limit_price": f"{price:.2f}",
            "max_slippage": 0.005,
            "is_reduce_only": False,
            "tpsl": None,
        }

        logger.info(
            "Placing LIMIT %s: $%.2f notional = %s BTC @ $%.2f (lev=%sx)",
            side.value.upper(), size_usdc, qty_str, price, leverage,
        )

        data = self._request("POST", "/orders/new/limit", json_body=payload)

        # Parse response (same format as market orders from /orders/v2)
        remote_id = str(
            data.get("order_id", data.get("id", data.get("trade_id", "")))
        )
        status_str = str(data.get("status", data.get("clearing_status", "pending"))).lower()

        if status_str in ("rejected", "error", "failed"):
            reason = data.get("error_message", data.get("cancel_reason", "unknown"))
            raise OrderRejectedError(remote_id, str(reason))

        order = Order(
            symbol=f"{market_id}-USDC",
            side=side,
            order_type=OrderType.LIMIT,
            size_usdc=size_usdc,
            price=price,
            leverage=leverage,
            status=OrderStatus.PENDING,
            remote_order_id=remote_id,
        )
        logger.info("Limit order submitted — remote_id=%s  status=%s", remote_id, status_str)
        return order

    # ─────────────────────────────────────────────────────────
    #  Orders — Market (for closing positions if needed)
    # ─────────────────────────────────────────────────────────

    def place_market_order(
        self,
        market_id: str,
        side: Side,
        size_usdc: float,
        price: float,
        reduce_only: bool = False,
        quote_id: str = "",
        qty_override: str = "",
    ) -> Order:
        """
        Submit a market order.

        Requires a fresh quote_id from get_quote().
        If no quote_id provided, fetches one automatically.
        If qty_override is set, use that exact BTC qty string instead of
        recalculating from size_usdc / price (avoids rounding mismatch).
        """
        if qty_override:
            qty_str = qty_override
        else:
            qty_asset = size_usdc / price
            qty_str = f"{qty_asset:.6f}"
        side_str = "buy" if side == Side.LONG else "sell"
        instrument = make_instrument(market_id)

        # Get fresh quote_id if not provided
        if not quote_id:
            q = self.get_quote(market_id, qty=qty_str, side=side_str)
            quote_id = q.quote_id

        payload = {
            "instrument": instrument,
            "side": side_str,
            "qty": qty_str,
            "quote_id": quote_id,
            "max_slippage": 0.005,
            "is_reduce_only": reduce_only,
            "tpsl": None,
        }

        logger.info("Placing MARKET %s: %s BTC (~$%.2f)", side_str.upper(), qty_str, size_usdc)
        data = self._request("POST", "/orders/new/market", json_body=payload)

        # API returns {"rfq_id": "...", "take_profit_rfq_id": null, "stop_loss_rfq_id": null}
        remote_id = str(data.get("rfq_id", data.get("order_id", "")))

        order = Order(
            symbol=f"{market_id}-USDC",
            side=side,
            order_type=OrderType.MARKET,
            size_usdc=size_usdc,
            price=price,
            leverage=1.0,
            status=OrderStatus.FILLED,  # market orders on Variational are fill-or-kill
            fill_price=price,
            remote_order_id=remote_id,
            filled_at=time.time(),
        )
        logger.info("Market order accepted — rfq_id=%s", remote_id[:16])
        return order

    # ─────────────────────────────────────────────────────────
    #  TP / SL Orders
    # ─────────────────────────────────────────────────────────

    def place_tp_sl_orders(
        self,
        market_id: str,
        side: Side,
        size_usdc: float,
        tp_price: float,
        sl_price: float,
        leverage: float,
        slippage_tolerance: float = 0.005,
    ) -> tuple[Order, Order]:
        """
        Submit Take-Profit and Stop-Loss trigger orders.

        POST /tpsl (assumed endpoint from DevTools network tab)

        On Variational, TP/SL are trigger orders that submit market orders
        when the mark/quote price crosses the trigger level.

        Args:
            market_id:  Underlying (e.g. "BTC").
            side:       The POSITION side (TP/SL will close it).
            size_usdc:  Position size to close.
            tp_price:   Take-profit trigger price.
            sl_price:   Stop-loss trigger price.
            leverage:   Position leverage.
            slippage_tolerance: Max slippage on fill.

        Returns:
            Tuple of (tp_order, sl_order).
        """
        close_side = Side.SHORT if side == Side.LONG else Side.LONG
        close_side_str = "sell" if side == Side.LONG else "buy"
        instrument = make_instrument(market_id)
        qty_asset = size_usdc / tp_price  # approximate
        qty_str = f"{qty_asset:.6f}"

        # ── Take-Profit ──
        tp_payload = {
            "instrument": instrument,
            "side": close_side_str,
            "qty": qty_str,
            "order_type": "take_profit",
            "trigger_price": f"{tp_price:.2f}",
            "slippage_limit": f"{slippage_tolerance:.12f}",
            "use_mark_price": False,
            "is_auto_resize": True,
            "is_reduce_only": True,
        }
        logger.info(
            "Placing TP: %s %s BTC @ trigger $%.2f",
            close_side_str.upper(), qty_str, tp_price,
        )
        tp_data = self._request("POST", "/tpsl", json_body=tp_payload)

        tp_order = Order(
            symbol=f"{market_id}-USDC",
            side=close_side,
            order_type=OrderType.TRIGGER,
            size_usdc=size_usdc,
            price=tp_price,
            leverage=leverage,
            status=OrderStatus.PENDING,
            slippage_tolerance=slippage_tolerance,
            remote_order_id=str(
                tp_data.get("order_id", tp_data.get("id", ""))
            ),
        )

        # ── Stop-Loss ──
        sl_payload = {
            "instrument": instrument,
            "side": close_side_str,
            "qty": qty_str,
            "order_type": "stop_loss",
            "trigger_price": f"{sl_price:.2f}",
            "slippage_limit": f"{slippage_tolerance:.12f}",
            "use_mark_price": False,
            "is_auto_resize": True,
            "is_reduce_only": True,
        }
        logger.info(
            "Placing SL: %s %s BTC @ trigger $%.2f",
            close_side_str.upper(), qty_str, sl_price,
        )
        sl_data = self._request("POST", "/tpsl", json_body=sl_payload)

        sl_order = Order(
            symbol=f"{market_id}-USDC",
            side=close_side,
            order_type=OrderType.TRIGGER,
            size_usdc=size_usdc,
            price=sl_price,
            leverage=leverage,
            status=OrderStatus.PENDING,
            slippage_tolerance=slippage_tolerance,
            remote_order_id=str(
                sl_data.get("order_id", sl_data.get("id", ""))
            ),
        )

        logger.info(
            "TP/SL submitted — TP=%s  SL=%s",
            tp_order.remote_order_id, sl_order.remote_order_id,
        )
        return tp_order, sl_order

    # ─────────────────────────────────────────────────────────
    #  Order Status Polling
    # ─────────────────────────────────────────────────────────

    def get_order_status(self, order_id: str) -> dict[str, Any]:
        """
        Check the status of an order by searching recent orders.

        GET /orders/v2 -> { "pagination": {...}, "result": [ ... ] }

        Each order has:
          - order_id, status ("cleared", "pending", "cancelled", ...)
          - clearing_status ("success_trades_booked_into_pool", ...)
          - price (fill price), qty, side, ...
        """
        data = self._request("GET", "/orders/v2")
        for order in data.get("result", []):
            if not isinstance(order, dict):
                continue
            oid = str(order.get("order_id", ""))
            if oid == order_id:
                # Map Variational status to our status
                raw_status = str(order.get("status", "")).lower()
                clearing = str(order.get("clearing_status", "")).lower()

                if "cleared" in raw_status or "success" in clearing:
                    return {
                        "status": "filled",
                        "fillPrice": order.get("price"),
                        "fill_price": order.get("price"),
                        **order,
                    }
                elif "cancel" in raw_status:
                    return {"status": "cancelled", **order}
                elif "reject" in raw_status or "failed" in clearing:
                    return {"status": "rejected", **order}
                else:
                    return {"status": "pending", **order}

        return {"status": "pending", "order_id": order_id}

    def get_pending_orders(self, instrument: str = "P-BTC-USDC-3600") -> list[dict[str, Any]]:
        """
        Fetch only pending orders.

        GET /orders/v2?status=pending&instrument=P-BTC-USDC-3600
        """
        data = self._request(
            "GET", "/orders/v2",
            params={"status": "pending", "instrument": instrument},
        )
        return data.get("result", [])

    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel a pending order.

        DELETE /orders/v2/{order_id}  (estimated — verify in DevTools)

        If DELETE doesn't work, the bot will simply wait for timeout.
        """
        try:
            self._request("DELETE", f"/orders/v2/{order_id}")
            logger.info("Order %s cancelled", order_id)
            return True
        except ApiError as exc:
            logger.warning("Failed to cancel order %s: %s", order_id, exc)
            # Try POST cancel as fallback
            try:
                self._request("POST", f"/orders/{order_id}/cancel")
                logger.info("Order %s cancelled (fallback)", order_id)
                return True
            except Exception:
                pass
            return False

    # ─────────────────────────────────────────────────────────
    #  Positions
    # ─────────────────────────────────────────────────────────

    def get_open_positions(self) -> list[Position]:
        """
        Fetch all open positions.

        GET /positions -> [
          {
            "position_info": {
              "instrument": { "underlying": "BTC", ... },
              "qty": "0.000089",
              "avg_entry_price": "77284.45",
              ...
            },
            "price_info": { "price": "77223.49", ... },
            "value": "6.87289061",
            "upnl": "-0.00542544",
            ...
          }
        ]
        """
        data = self._request("GET", "/positions")
        raw_list: list[Any] = data.get("_list", [])
        positions: list[Position] = []

        for item in raw_list:
            if not isinstance(item, dict):
                continue

            pos_info = item.get("position_info", {})
            instrument = pos_info.get("instrument", {})
            underlying = instrument.get("underlying", "?")
            settlement = instrument.get("settlement_asset", "USDC")

            qty = float(pos_info.get("qty", 0))
            entry_price = float(pos_info.get("avg_entry_price", 0))
            notional = abs(qty * entry_price)
            upnl = float(item.get("upnl", 0))

            # Positive qty = long, negative = short
            side = Side.LONG if qty >= 0 else Side.SHORT

            positions.append(
                Position(
                    symbol=f"{underlying}-{settlement}",
                    side=side,
                    entry_price=entry_price,
                    size_usdc=notional,
                    leverage=1.0,  # leverage is account-level on Omni
                    status=PositionStatus.OPEN,
                    unrealized_pnl=upnl,
                    remote_position_id=str(
                        pos_info.get("pool_location", "")
                    ),
                    raw_qty=str(pos_info.get("qty", "0")),
                )
            )

        return positions

    # ─────────────────────────────────────────────────────────
    #  Utility / Health
    # ─────────────────────────────────────────────────────────

    def ping(self) -> bool:
        """
        Health check — verify the connection and auth are working.

        Tests by fetching /portfolio (lightweight endpoint).
        Returns True if successful.
        """
        try:
            data = self._request("GET", "/portfolio")
            balance = float(data.get("balance", 0))
            logger.info("Ping OK — balance=$%.4f", balance)
            return True
        except Exception as exc:
            logger.error("Ping FAILED: %s", exc)
            return False

    def get_leverage_info(self, underlying: str = "BTC") -> dict[str, Any]:
        """
        POST /settlement_pools/leverage { "assets": ["BTC"] }
        -> { "BTC": { "current": "50", "limits": [...] } }
        """
        data = self._request(
            "POST",
            "/settlement_pools/leverage",
            json_body={"assets": [underlying]},
        )
        return data.get(underlying, {})

    def close(self) -> None:
        """Nothing to close — curl_cffi uses per-request sessions."""
        logger.info("VariationalClient closed")
