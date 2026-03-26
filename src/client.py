"""Polymarket CLOB client wrapper.

Wraps py-clob-client with retry logic, error handling,
and a simplified interface for the bot.
"""

import time
from dataclasses import dataclass

from loguru import logger
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.constants import POLYGON


@dataclass
class MarketInfo:
    """Simplified market data."""

    token_id: str
    condition_id: str
    question: str
    outcome: str  # "Yes" or "No"
    active: bool
    end_date_iso: str
    min_tick_size: float
    # Complementary token for the other side
    complement_token_id: str | None = None


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    midpoint: float


class PolymarketClient:
    """Wrapper around py-clob-client with retry and error handling."""

    MAX_RETRIES = 3
    RETRY_DELAY = 2  # seconds, doubles each retry

    def __init__(self, host: str, private_key: str, chain_id: int = POLYGON,
                 signature_type: int = 0, funder: str | None = None):
        self.host = host
        self._client = ClobClient(
            host,
            key=private_key,
            chain_id=chain_id,
            signature_type=signature_type,
            funder=funder,
        )
        self._api_creds = None

    def authenticate(self):
        """Derive L2 API credentials from private key."""
        logger.info("Deriving API credentials...")
        self._api_creds = self._client.create_or_derive_api_creds()
        self._client.set_api_creds(self._api_creds)
        logger.info("API credentials set successfully")

    def _retry(self, func, *args, **kwargs):
        """Execute with exponential backoff retry."""
        last_error = None
        for attempt in range(self.MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_error = e
                if attempt < self.MAX_RETRIES - 1:
                    delay = self.RETRY_DELAY * (2 ** attempt)
                    logger.warning(
                        f"Request failed (attempt {attempt + 1}/{self.MAX_RETRIES}): {e}. "
                        f"Retrying in {delay}s..."
                    )
                    time.sleep(delay)
        raise last_error

    # -- Market data (no auth required) --

    def get_markets(self, max_pages: int = 5) -> list[dict]:
        """Get all available markets with pagination."""
        return self._paginate(self._client.get_markets, max_pages)

    def get_simplified_markets(self, max_pages: int = 5) -> list[dict]:
        """Get simplified market listing with pagination."""
        return self._paginate(self._client.get_simplified_markets, max_pages)

    def get_sampling_simplified_markets(self, max_pages: int = 3) -> list[dict]:
        """Get sampling/rewards-eligible simplified markets."""
        return self._paginate(self._client.get_sampling_simplified_markets, max_pages)

    def get_sampling_markets(self, max_pages: int = 3) -> list[dict]:
        """Get sampling/rewards-eligible markets (full detail)."""
        return self._paginate(self._client.get_sampling_markets, max_pages)

    def _paginate(self, api_func, max_pages: int = 5) -> list[dict]:
        """Fetch paginated results from a CLOB API endpoint."""
        all_data = []
        cursor = "MA=="
        for page in range(max_pages):
            result = self._retry(api_func, cursor)
            if isinstance(result, list):
                all_data.extend(result)
                break  # No pagination info
            data = result.get("data", [])
            if not data:
                break
            all_data.extend(data)
            cursor = result.get("next_cursor", "")
            if not cursor or cursor == "MA==":
                break
            logger.debug(f"Fetched page {page + 1}, {len(data)} markets (total: {len(all_data)})")
        return all_data

    def get_orderbook(self, token_id: str) -> OrderBook:
        """Get order book for a token."""
        raw = self._retry(self._client.get_order_book, token_id)
        bids = [OrderBookLevel(float(b["price"]), float(b["size"])) for b in raw.get("bids", [])]
        asks = [OrderBookLevel(float(a["price"]), float(a["size"])) for a in raw.get("asks", [])]

        # Calculate midpoint
        best_bid = bids[0].price if bids else 0.0
        best_ask = asks[0].price if asks else 1.0
        midpoint = (best_bid + best_ask) / 2

        return OrderBook(bids=bids, asks=asks, midpoint=midpoint)

    def get_midpoint(self, token_id: str) -> float:
        """Get midpoint price for a token."""
        result = self._retry(self._client.get_midpoint, token_id)
        if isinstance(result, dict):
            return float(result.get("mid", 0.5))
        return float(result)

    def get_price(self, token_id: str, side: str = "buy") -> float:
        """Get current price on one side."""
        result = self._retry(self._client.get_price, token_id, side)
        if isinstance(result, dict):
            return float(result.get("price", 0.5))
        return float(result)

    # -- Trading (auth required) --

    def place_limit_order(self, token_id: str, price: float, size: float,
                          side: str) -> dict:
        """Place a limit order. side: 'BUY' or 'SELL'."""
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
        )
        signed_order = self._retry(self._client.create_order, order_args)
        result = self._retry(self._client.post_order, signed_order, OrderType.GTC)
        logger.debug(f"Placed {side} order: {size} @ {price} on {token_id[:8]}...")
        return result

    def cancel_order(self, order_id: str) -> dict:
        """Cancel a single order."""
        result = self._retry(self._client.cancel, order_id)
        logger.debug(f"Cancelled order {order_id}")
        return result

    def cancel_all_orders(self) -> dict:
        """Cancel all open orders."""
        result = self._retry(self._client.cancel_all)
        logger.info("Cancelled all orders")
        return result

    def get_open_orders(self, market: str | None = None) -> list[dict]:
        """Get open orders, optionally filtered by market."""
        params = {}
        if market:
            params["market"] = market
        return self._retry(self._client.get_orders, params)

    def get_trades(self) -> list[dict]:
        """Get trade history."""
        return self._retry(self._client.get_trades)

    # -- Utilities --

    def check_connection(self) -> bool:
        """Verify API connection."""
        try:
            result = self._client.get_ok()
            ok = result == "OK" or (isinstance(result, dict) and result.get("status") == "OK")
            if ok:
                logger.info("Connected to Polymarket CLOB")
            return ok
        except Exception as e:
            logger.error(f"Connection check failed: {e}")
            return False
