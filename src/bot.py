"""Main bot loop - orchestrates all components.

Runs the conservative LP farming strategy:
1. Select best markets periodically
2. Fetch orderbooks
3. Calculate conservative quotes
4. Apply risk checks
5. Place/update orders
6. Log status
"""

import time

from loguru import logger

from src.client import PolymarketClient
from src.market_selector import ScoredMarket, select_markets
from src.order_manager import OrderManager
from src.risk_manager import RiskManager
from src.strategy import calculate_quotes, should_requote


class LPFarmingBot:
    """Conservative LP farming bot for Polymarket airdrop."""

    def __init__(self, client: PolymarketClient, config: dict):
        self.client = client
        self.config = config

        strategy_cfg = config.get("strategy", {})
        risk_cfg = config.get("risk", {})
        self.market_cfg = config.get("market_selection", {})

        self.spread_bps = strategy_cfg.get("spread_bps", 300)
        self.order_size = strategy_cfg.get("order_size_usdc", 30)
        self.order_levels = strategy_cfg.get("order_levels", 2)
        self.refresh_interval = strategy_cfg.get("refresh_interval_sec", 15)
        self.market_refresh_interval = strategy_cfg.get("market_refresh_interval_sec", 300)

        self.order_manager = OrderManager(client)
        self.risk_manager = RiskManager(risk_cfg)

        self.active_markets: list[ScoredMarket] = []
        self.last_midpoints: dict[str, float] = {}
        self.last_market_refresh: float = 0
        self._running = False

    def run(self):
        """Main bot loop."""
        logger.info("=" * 60)
        logger.info("Polymarket LP Farming Bot starting")
        logger.info(f"Strategy: spread={self.spread_bps}bps, size=${self.order_size}, levels={self.order_levels}")
        logger.info("=" * 60)

        # Verify connection
        if not self.client.check_connection():
            logger.error("Cannot connect to Polymarket. Exiting.")
            return

        self._running = True

        try:
            while self._running:
                self._tick()
                time.sleep(self.refresh_interval)
        except KeyboardInterrupt:
            logger.info("Shutting down (Ctrl+C)...")
        except Exception as e:
            logger.exception(f"Unexpected error: {e}")
        finally:
            self._shutdown()

    def stop(self):
        """Signal the bot to stop."""
        self._running = False

    def _tick(self):
        """Single iteration of the main loop."""
        # Refresh market selection periodically
        if time.time() - self.last_market_refresh > self.market_refresh_interval:
            self._refresh_markets()

        if not self.active_markets:
            logger.warning("No markets selected. Waiting for next refresh...")
            return

        # Update quotes for each market
        for market in self.active_markets:
            try:
                self._update_market(market)
            except Exception as e:
                logger.error(f"Error updating {market.question[:30]}...: {e}")

        # Log status every 10 ticks (~2.5 min at 15s interval)
        self._log_status()

    def _refresh_markets(self):
        """Fetch and score markets, select top ones."""
        logger.info("Refreshing market selection...")
        try:
            raw_markets = self.client.get_simplified_markets()
            if not raw_markets:
                logger.warning("No markets returned from API")
                return

            # Merge risk config into market selection config
            selection_config = {
                **self.market_cfg,
                "min_probability": self.risk_manager.min_probability,
                "max_probability": self.risk_manager.max_probability,
                "min_days_to_expiry": self.risk_manager.min_days_to_expiry,
            }

            new_markets = select_markets(
                raw_markets,
                max_markets=self.market_cfg.get("max_markets", 5),
                config=selection_config,
            )

            # Cancel orders for markets we're no longer in
            old_tokens = {m.token_id for m in self.active_markets}
            new_tokens = {m.token_id for m in new_markets}
            for token_id in old_tokens - new_tokens:
                self.order_manager.cancel_all_token_orders(token_id)
                logger.info(f"Exited market {token_id[:8]}...")

            self.active_markets = new_markets
            self.last_market_refresh = time.time()

        except Exception as e:
            logger.error(f"Market refresh failed: {e}")

    def _update_market(self, market: ScoredMarket):
        """Update quotes for a single market."""
        token_id = market.token_id

        # Get current midpoint
        midpoint = self.client.get_midpoint(token_id)

        # Risk check
        allowed, reason = self.risk_manager.can_trade(
            token_id, midpoint, market.days_to_expiry
        )
        if not allowed:
            self.order_manager.cancel_all_token_orders(token_id)
            logger.debug(f"Skipping {market.question[:30]}...: {reason}")
            return

        # Check if we need to requote
        last_mid = self.last_midpoints.get(token_id, 0)
        if not should_requote(midpoint, last_mid, threshold_bps=50):
            return  # Price hasn't moved enough, keep existing orders

        # Calculate new quotes
        skew = self.risk_manager.get_inventory_skew(token_id)
        quotes = calculate_quotes(
            midpoint=midpoint,
            spread_bps=self.spread_bps,
            order_size=self.order_size,
            order_levels=self.order_levels,
            inventory_skew=skew,
        )

        # Place orders
        self.order_manager.update_orders(token_id, quotes)
        self.last_midpoints[token_id] = midpoint

    def _log_status(self):
        """Log current bot status."""
        risk_status = self.risk_manager.get_status()
        order_count = self.order_manager.get_active_order_count()

        logger.info(
            f"Status: markets={len(self.active_markets)}, "
            f"orders={order_count}, "
            f"daily_pnl=${risk_status['daily_pnl']:.2f}, "
            f"total_pos=${risk_status['total_position']:.0f}, "
            f"paused={risk_status['markets_paused']}"
        )

    def _shutdown(self):
        """Clean shutdown: cancel all orders."""
        logger.info("Cancelling all orders...")
        self.order_manager.cancel_everything()
        logger.info("Bot stopped. All orders cancelled.")
