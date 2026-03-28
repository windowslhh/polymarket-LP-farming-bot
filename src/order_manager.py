"""Order lifecycle management.

Tracks active orders, handles cancel-replace cycles,
and detects fills for inventory tracking.

Dual-BID strategy: Instead of splitting USDC into YES+NO tokens
and placing ASK orders, we place BID orders on both YES and NO tokens.
  - BID on YES token at bid_price  → buy side (uses USDC)
  - BID on NO token at (1 - ask_price)  → equivalent to selling YES (uses USDC)
This means both sides only need USDC — no on-chain split/merge needed!
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

    def update_orders(
        self,
        yes_token_id: str,
        no_token_id: str | None,
        quotes: QuotePair,
        condition_id: str | None = None,
        midpoint: float = 0.5,
    ):
        """Cancel existing orders and place new ones.

        Dual-BID strategy:
          - BID quotes → placed on YES token (buy YES)
          - ASK quotes → converted to BID on NO token (buy NO = sell YES)
        Both sides only require USDC collateral.
        """
        # Step 1: Cancel existing orders for YES and NO tokens
        self._cancel_token_orders(yes_token_id)
        if no_token_id:
            self._cancel_token_orders(no_token_id)

        # Step 2: Place BID orders on YES token (buy side)
        new_orders = []
        for quote in quotes.bids:
            order = self._place_order(yes_token_id, quote)
            if order:
                new_orders.append(order)

        # Step 3: Place BID orders on NO token (= sell YES side)
        no_orders = []
        if no_token_id and quotes.asks:
            for ask_quote in quotes.asks:
                # Selling YES at ask_price = Buying NO at (1 - ask_price)
                no_price = round(1.0 - ask_quote.price, 4)
                if no_price <= 0.0 or no_price >= 1.0:
                    logger.warning(f"Invalid NO price {no_price} from ASK {ask_quote.price}, skipping")
                    continue
                # Share size: use same USDC notional as the ask
                # ASK was: sell ask_shares YES at ask_price → notional = ask_shares * ask_price
                # NO BID: buy no_shares NO at no_price → notional = no_shares * no_price
                # Keep same USDC notional: no_shares = (ask_shares * ask_price) / no_price
                usdc_notional = ask_quote.size * ask_quote.price
                no_shares = round(usdc_notional / no_price, 2)
                no_quote = Quote(price=no_price, size=no_shares, side="BUY")
                order = self._place_order(no_token_id, no_quote)
                if order:
                    no_orders.append(order)
        elif not no_token_id and quotes.asks:
            logger.warning("No complement token ID — cannot place sell-side orders")

        self.active_orders[yes_token_id] = new_orders
        if no_token_id:
            self.active_orders[no_token_id] = no_orders

        bid_count = len(new_orders)
        ask_count = len(no_orders)
        logger.info(
            f"Updated orders for {yes_token_id[:8]}...: "
            f"{bid_count} YES bids, {ask_count} NO bids (sell-side)"
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
