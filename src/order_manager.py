"""Order lifecycle management.

Tracks active orders, handles cancel-replace cycles,
and detects fills for inventory tracking.
"""

import time
from dataclasses import dataclass, field

from loguru import logger

from src.client import PolymarketClient
from src.strategy import Quote, QuotePair


@dataclass
class ActiveOrder:
    """Tracks a live order."""
    order_id: str
    token_id: str
    price: float
    size: float
    side: str
    placed_at: float = field(default_factory=time.time)


class OrderManager:
    """Manages order placement, cancellation, and tracking."""

    def __init__(self, client: PolymarketClient):
        self.client = client
        # token_id -> list of ActiveOrder
        self.active_orders: dict[str, list[ActiveOrder]] = {}
        self.total_fills_buy: float = 0.0
        self.total_fills_sell: float = 0.0

    def update_orders(self, token_id: str, quotes: QuotePair):
        """Cancel existing orders and place new ones (cancel-replace cycle).

        This is the safest approach: cancel all, then place new.
        Avoids stale orders and simplifies state management.
        """
        # Step 1: Cancel existing orders for this token
        self._cancel_token_orders(token_id)

        # Step 2: Place new orders
        new_orders = []

        for quote in quotes.bids:
            order = self._place_order(token_id, quote)
            if order:
                new_orders.append(order)

        for quote in quotes.asks:
            order = self._place_order(token_id, quote)
            if order:
                new_orders.append(order)

        self.active_orders[token_id] = new_orders

        bid_count = len(quotes.bids)
        ask_count = len(quotes.asks)
        logger.info(
            f"Updated orders for {token_id[:8]}...: "
            f"{bid_count} bids, {ask_count} asks"
        )

    def cancel_all_token_orders(self, token_id: str):
        """Cancel all orders for a specific token."""
        self._cancel_token_orders(token_id)
        self.active_orders.pop(token_id, None)

    def cancel_everything(self):
        """Emergency: cancel all orders across all markets."""
        try:
            self.client.cancel_all_orders()
            self.active_orders.clear()
            logger.warning("Emergency cancel: all orders cancelled")
        except Exception as e:
            logger.error(f"Emergency cancel failed: {e}")

    def get_active_order_count(self) -> int:
        """Total number of active orders across all markets."""
        return sum(len(orders) for orders in self.active_orders.values())

    def get_market_order_count(self, token_id: str) -> int:
        """Number of active orders for a specific market."""
        return len(self.active_orders.get(token_id, []))

    def _place_order(self, token_id: str, quote: Quote) -> ActiveOrder | None:
        """Place a single order and return tracking object."""
        try:
            result = self.client.place_limit_order(
                token_id=token_id,
                price=quote.price,
                size=quote.size,
                side=quote.side,
            )
            order_id = result.get("orderID", result.get("id", "unknown"))
            return ActiveOrder(
                order_id=order_id,
                token_id=token_id,
                price=quote.price,
                size=quote.size,
                side=quote.side,
            )
        except Exception as e:
            logger.error(f"Failed to place {quote.side} {quote.size}@{quote.price}: {e}")
            return None

    def _cancel_token_orders(self, token_id: str):
        """Cancel all tracked orders for a token."""
        orders = self.active_orders.get(token_id, [])
        for order in orders:
            try:
                self.client.cancel_order(order.order_id)
            except Exception as e:
                logger.debug(f"Cancel failed for {order.order_id} (may be filled): {e}")
        if orders:
            logger.debug(f"Cancelled {len(orders)} orders for {token_id[:8]}...")
