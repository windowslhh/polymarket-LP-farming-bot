"""Main bot loop - orchestrates all components.

Runs the conservative LP farming strategy:
1. Select best markets periodically
2. Fetch orderbooks (WebSocket or HTTP fallback)
3. Calculate conservative quotes
4. Apply risk checks
5. Place/update orders
6. Track PnL and log status
"""

import time

from loguru import logger

from src.client import PolymarketClient
from src.market_selector import ScoredMarket, select_markets
from src.order_manager import OrderManager
from src.pnl_tracker import PnLTracker
from src.risk_manager import RiskManager
from src.strategy import calculate_quotes, check_reward_compliance, should_requote
from src.websocket_client import PolymarketWebSocket


class LPFarmingBot:
    """Conservative LP farming bot for Polymarket airdrop."""

    def __init__(self, client: PolymarketClient, config: dict,
                 dry_run: bool = False):
        self.client = client
        self.config = config
        self.dry_run = dry_run

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
        self.pnl_tracker = PnLTracker()
        self.ws_client = PolymarketWebSocket()

        self.active_markets: list[ScoredMarket] = []
        self.last_midpoints: dict[str, float] = {}
        self.last_market_refresh: float = 0
        self._tick_count = 0
        self._running = False
        # Price history for volatility estimation (token_id -> list of midpoints)
        self._price_history: dict[str, list[float]] = {}
        self._max_price_history = 288  # ~1 day at 5-min intervals

    def run(self):
        """Main bot loop."""
        mode = "DRY RUN" if self.dry_run else "LIVE"
        logger.info("=" * 60)
        logger.info(f"Polymarket LP Farming Bot starting [{mode}]")
        logger.info(f"Strategy: spread={self.spread_bps}bps, size=${self.order_size}, levels={self.order_levels}")
        logger.info("=" * 60)

        if self.dry_run:
            logger.info("DRY RUN mode: orders will be calculated but NOT placed")

        # Verify connection
        if not self.client.check_connection():
            logger.error("Cannot connect to Polymarket. Exiting.")
            return

        # Start WebSocket for real-time data (optional)
        self.ws_client.start()

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
        self._tick_count += 1

        # Refresh market selection periodically
        if time.time() - self.last_market_refresh > self.market_refresh_interval:
            self._refresh_markets()

        if not self.active_markets:
            logger.warning("No markets selected. Waiting for next refresh...")
            return

        # Check fills from previous cycle
        self._check_fills()

        # Update quotes for each market
        for market in self.active_markets:
            try:
                self._update_market(market)
            except Exception as e:
                logger.error(f"Error updating {market.question[:30]}...: {e}")

        # Update PnL tracker
        self.pnl_tracker.update_active_markets(len(self.active_markets))

        # Log status every 10 ticks (~2.5 min at 15s interval)
        if self._tick_count % 10 == 0:
            self._log_status()

        # Save PnL data every 60 ticks (~15 min)
        if self._tick_count % 60 == 0:
            self.pnl_tracker.save()

    def _refresh_markets(self):
        """Fetch and score markets, select top ones.

        Enhanced pipeline: fetches orderbooks for competition assessment
        and uses cached price history for volatility estimation.
        """
        logger.info("Refreshing market selection...")
        try:
            # Use sampling markets (reward-eligible only, has full field data)
            raw_markets = self.client.get_sampling_markets()
            if not raw_markets:
                logger.warning("No markets returned from API")
                return

            logger.info(f"Fetched {len(raw_markets)} sampling markets")

            # Merge risk config into market selection config
            selection_config = {
                **self.market_cfg,
                "min_probability": self.risk_manager.min_probability,
                "max_probability": self.risk_manager.max_probability,
                "min_days_to_expiry": self.risk_manager.min_days_to_expiry,
            }

            # Pass 1: Quick score all markets (no API calls) to find candidates
            from src.market_selector import _quick_score_markets
            candidates = _quick_score_markets(raw_markets, selection_config, top_n=30)

            # Pass 2: Fetch orderbooks only for top candidates
            orderbooks = {}
            price_histories = {}
            for market in candidates:
                tokens = market.get("tokens", [])
                if tokens:
                    tid = tokens[0].get("token_id", "")
                    if tid:
                        try:
                            ob = self.client.get_orderbook(tid)
                            orderbooks[tid] = {
                                "bids": [{"price": b.price, "size": b.size} for b in ob.bids],
                                "asks": [{"price": a.price, "size": a.size} for a in ob.asks],
                            }
                        except Exception:
                            pass  # Skip markets without orderbooks
                        # Use cached midpoint history for volatility
                        if tid in self._price_history:
                            price_histories[tid] = self._price_history[tid]

            # Pass 3: Re-score with competition data
            new_markets = select_markets(
                raw_markets,
                max_markets=self.market_cfg.get("max_markets", 5),
                config=selection_config,
                orderbooks=orderbooks,
                price_histories=price_histories,
            )

            # Cancel orders and clean up inventory for markets we're no longer in
            old_market_map = {m.token_id: m for m in self.active_markets}
            new_tokens = {m.token_id for m in new_markets}
            for token_id, old_market in old_market_map.items():
                if token_id in new_tokens:
                    continue
                if not self.dry_run:
                    self.order_manager.cancel_all_token_orders(token_id)
                    # Merge remaining YES+NO pairs back to USDC
                    self._try_merge_inventory(old_market)
                self.ws_client.unsubscribe(token_id)
                logger.info(f"Exited market {token_id[:8]}...")

            # Subscribe new markets to WebSocket
            for token_id in new_tokens - old_tokens:
                self.ws_client.subscribe(token_id)

            self.active_markets = new_markets
            self.last_market_refresh = time.time()

        except Exception as e:
            logger.error(f"Market refresh failed: {e}")

    def _update_market(self, market: ScoredMarket):
        """Update quotes for a single market."""
        token_id = market.token_id

        # Get current midpoint (prefer WebSocket, fallback to HTTP)
        midpoint = self._get_midpoint(token_id)

        # Risk check
        allowed, reason = self.risk_manager.can_trade(
            token_id, midpoint, market.days_to_expiry
        )
        if not allowed:
            if not self.dry_run:
                self.order_manager.cancel_all_token_orders(token_id)
            logger.debug(f"Skipping {market.question[:30]}...: {reason}")
            return

        # Check if we need to requote
        last_mid = self.last_midpoints.get(token_id, 0)
        if not should_requote(midpoint, last_mid, threshold_bps=50):
            return  # Price hasn't moved enough, keep existing orders

        # Determine effective order size and spread to meet reward requirements
        effective_size = self.order_size
        effective_spread = self.spread_bps
        if market.reward_info.has_rewards and midpoint > 0:
            # Use highest quote price (ask side) so even ask orders meet min_shares
            # ask_price ≈ midpoint + spread/2
            worst_price = midpoint + effective_spread / 20000
            min_usdc = market.reward_info.min_shares * worst_price
            if min_usdc > effective_size:
                effective_size = min_usdc
                logger.debug(
                    f"Sizing up for {market.question[:30]}...: "
                    f"${effective_size:.0f} (min {market.reward_info.min_shares:.0f} shares @ {midpoint:.2f})"
                )
            # Clamp spread to max_spread requirement (convert to bps)
            max_spread_bps = int(market.reward_info.max_spread * 10000)
            if effective_spread > max_spread_bps:
                effective_spread = max_spread_bps

        # Calculate new quotes
        skew = self.risk_manager.get_inventory_skew(token_id)
        quotes = calculate_quotes(
            midpoint=midpoint,
            spread_bps=effective_spread,
            order_size=effective_size,
            order_levels=self.order_levels,
            inventory_skew=skew,
        )

        if self.dry_run:
            # Verify reward compliance
            compliant = True
            compliance_note = "OK"
            if market.reward_info.has_rewards:
                from src.strategy import check_reward_compliance
                compliant, compliance_note = check_reward_compliance(
                    quotes, market.reward_info.max_spread, market.reward_info.min_shares
                )

            # Calculate actual spread from quotes
            if quotes.bids and quotes.asks:
                actual_spread_bps = int((quotes.asks[0].price - quotes.bids[0].price) * 10000)
                max_spread_bps = int(market.reward_info.max_spread * 10000) if market.reward_info.has_rewards else 9999
                spread_ok = "✓" if actual_spread_bps <= max_spread_bps else "✗"
            else:
                actual_spread_bps = 0
                spread_ok = "?"

            bid_sizes = [q.size for q in quotes.bids]
            ask_sizes = [q.size for q in quotes.asks]
            min_size = min(bid_sizes + ask_sizes) if (bid_sizes or ask_sizes) else 0
            size_ok = "✓" if min_size >= market.reward_info.min_shares else "✗"

            logger.info(
                f"[DRY] {market.question[:45]}\n"
                f"       mid={midpoint:.3f}  spread={actual_spread_bps}bps{spread_ok}(max={max_spread_bps}bps)"
                f"  size={min_size:.0f}{size_ok}(min={market.reward_info.min_shares:.0f})"
                f"  reward={market.reward_info.total_rewards:.0f}/day"
                f"  compliant={'YES' if compliant else 'NO: '+compliance_note}"
            )
            for q in quotes.bids:
                logger.info(f"         BUY  {q.size:6.1f} shares @ {q.price:.4f}  (${q.size * q.price:.1f} USDC)")
            for q in quotes.asks:
                logger.info(f"         SELL {q.size:6.1f} shares @ {q.price:.4f}  (${q.size * q.price:.1f} USDC)")
        else:
            self.order_manager.update_orders(
                token_id,
                quotes,
                condition_id=market.condition_id,
                midpoint=midpoint,
            )

        self.last_midpoints[token_id] = midpoint

        # Track price history for volatility estimation
        if token_id not in self._price_history:
            self._price_history[token_id] = []
        self._price_history[token_id].append(midpoint)
        if len(self._price_history[token_id]) > self._max_price_history:
            self._price_history[token_id] = self._price_history[token_id][-self._max_price_history:]

    def _try_merge_inventory(self, market: "ScoredMarket"):
        """After exiting a market, merge remaining YES+NO pairs back to USDC."""
        try:
            yes_bal = self.client.get_conditional_balance(market.token_id)
            if market.complement_token_id:
                no_bal = self.client.get_conditional_balance(market.complement_token_id)
            else:
                no_bal = yes_bal  # assume balanced if no complement info

            # Can only merge the amount we have on both sides
            mergeable = min(yes_bal, no_bal)
            if mergeable >= 1.0:  # Only worth merging if at least $1 recoverable
                self.client.merge_positions(market.condition_id, mergeable)
            elif mergeable > 0:
                logger.debug(
                    f"Skipping merge for {market.question[:30]}...: "
                    f"only {mergeable:.2f} pairs (< $1)"
                )
        except Exception as e:
            logger.warning(f"Merge inventory failed for {market.token_id[:8]}...: {e}")

    def _get_midpoint(self, token_id: str) -> float:
        """Get midpoint from WebSocket cache or HTTP fallback."""
        ws_mid = self.ws_client.get_midpoint(token_id)
        if ws_mid is not None:
            return ws_mid
        return self.client.get_midpoint(token_id)

    def _check_fills(self):
        """Check for recent fills and update inventory/PnL."""
        if self.dry_run:
            return

        try:
            trades = self.client.get_trades()
            if not trades:
                return

            # Build map of market names for logging
            market_names = {m.token_id: m.question for m in self.active_markets}

            for trade in trades:
                token_id = trade.get("asset_id", "")
                if token_id not in market_names:
                    continue

                side = trade.get("side", "").upper()
                price = float(trade.get("price", 0))
                size = float(trade.get("size", 0))

                if price <= 0 or size <= 0:
                    continue

                midpoint = self.last_midpoints.get(token_id, price)

                # Record in PnL tracker
                self.pnl_tracker.record_fill(
                    token_id=token_id,
                    market_name=market_names.get(token_id, "Unknown"),
                    side=side,
                    price=price,
                    size=size,
                    midpoint=midpoint,
                )

                # Record in risk manager
                usdc_amount = price * size
                self.risk_manager.record_fill(
                    token_id=token_id,
                    side=side,
                    usdc_amount=usdc_amount,
                    fill_price=price,
                    midpoint_at_fill=midpoint,
                )

        except Exception as e:
            logger.debug(f"Fill check error: {e}")

    def _log_status(self):
        """Log current bot status."""
        risk_status = self.risk_manager.get_status()
        order_count = self.order_manager.get_active_order_count()
        cumulative = self.pnl_tracker.get_cumulative_stats()

        logger.info(
            f"Status: markets={len(self.active_markets)}, "
            f"orders={order_count}, "
            f"daily_pnl=${risk_status['daily_pnl']:.2f}, "
            f"total_pos=${risk_status['total_position']:.0f}, "
            f"cumulative_pnl=${cumulative['total_pnl']:.2f}, "
            f"days_active={cumulative['days_active']}"
        )

    def _shutdown(self):
        """Clean shutdown: cancel all orders, save data."""
        logger.info("Shutting down...")

        # Stop WebSocket
        self.ws_client.stop()

        # Cancel all orders
        if not self.dry_run:
            self.order_manager.cancel_everything()

        # Save PnL data
        self.pnl_tracker.save()

        # Print daily report
        report = self.pnl_tracker.get_daily_report()
        logger.info(report)

        # Print cumulative stats
        cumulative = self.pnl_tracker.get_cumulative_stats()
        logger.info(f"Cumulative: {cumulative}")

        logger.info("Bot stopped.")
