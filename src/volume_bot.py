"""Volume farming bot — directional buying of high-probability outcomes.

Strategy: buy YES/NO tokens at 90-99% probability, hold until resolution.
Generates trading volume for airdrop eligibility while earning small edge.

Risk management:
- Kelly criterion position sizing (1/4 Kelly, 2% cap)
- Three-layer dynamic stop-loss (EV + drawdown + hard stop)
- Drift velocity monitoring (2σ emergency exit)
- Portfolio VaR control (5% max daily drawdown)
"""

import time

from loguru import logger

from src.client import PolymarketClient
from src.pnl_tracker import PnLTracker
from src.stop_loss import (
    StopLossAction,
    assess_position_risk,
    check_portfolio_var,
    compute_stop_loss_levels,
    kelly_position_size,
)
from src.strategy import estimate_fee_rate
from src.volume_market_selector import VolumeCandidate, select_volume_markets
from src.volume_position import VolumePosition, VolumePositionTracker
from src.websocket_client import PolymarketWebSocket


class VolumeFarmingBot:
    """Directional volume farming bot for Polymarket airdrop."""

    def __init__(
        self,
        client: PolymarketClient,
        config: dict,
        dry_run: bool = False,
        capital: float = 50.0,
    ):
        self.client = client
        self.config = config
        self.dry_run = dry_run
        self.capital = capital  # USDC budget for volume farming

        vf_cfg = config.get("volume_farming", {})
        self.check_interval = vf_cfg.get("check_interval_sec", 30)
        self.market_refresh_interval = vf_cfg.get("market_refresh_interval_sec", 600)
        self.exit_timeout = vf_cfg.get("exit_timeout_sec", 60)
        self.max_markets = vf_cfg.get("max_markets", 5)
        self.kelly_fraction = vf_cfg.get("kelly_fraction", 0.25)
        self.max_position_pct = vf_cfg.get("max_position_pct", 0.02)
        self.max_total_exposure_pct = vf_cfg.get("max_total_exposure_pct", 0.10)
        self.max_daily_drawdown_pct = vf_cfg.get("max_daily_drawdown_pct", 0.05)

        # Stop-loss config (passed to assess_position_risk)
        self.sl_config = {
            "hard_stop_probability": vf_cfg.get("hard_stop_probability", 0.75),
            "drift_sigma_threshold": vf_cfg.get("drift_sigma_threshold", 2.0),
            "ev_buffer": vf_cfg.get("ev_buffer", 0.02),
            "ev_persistence_minutes": vf_cfg.get("ev_persistence_minutes", 5.0),
            "pl_ratio_min": vf_cfg.get("pl_ratio_min", 0.20),
            "expiry_alert_days": vf_cfg.get("expiry_alert_days", 7),
            "drawdown_by_entry_prob": vf_cfg.get("drawdown_by_entry_prob", None),
        }

        self.tracker = VolumePositionTracker()
        self.pnl_tracker = PnLTracker()
        self.ws_client = PolymarketWebSocket()

        self.last_market_refresh: float = 0
        self._tick_count = 0
        self._running = False

    def run(self):
        """Main bot loop."""
        mode = "DRY RUN" if self.dry_run else "LIVE"
        logger.info("=" * 60)
        logger.info(f"Volume Farming Bot starting [{mode}]")
        logger.info(f"Capital: ${self.capital:.0f}, Kelly: {self.kelly_fraction}, Max pos: {self.max_position_pct:.0%}")
        logger.info("=" * 60)

        self.ws_client.start()
        self._running = True

        try:
            while self._running:
                self._tick()
                time.sleep(self.check_interval)
        except KeyboardInterrupt:
            logger.info("Volume bot shutting down (Ctrl+C)...")
        except Exception as e:
            logger.exception(f"Volume bot error: {e}")
        finally:
            self._shutdown()

    def stop(self):
        self._running = False

    def _tick(self):
        """Single iteration: monitor positions, scan for new entries."""
        self._tick_count += 1

        # 1. Monitor existing positions (stop-loss checks)
        self._monitor_positions()

        # 2. Check pending exit orders
        self._check_exit_orders()

        # 3. Refresh markets and enter new positions periodically
        if time.time() - self.last_market_refresh > self.market_refresh_interval:
            self._refresh_and_enter()

        # 4. Log status every 10 ticks (~5 min)
        if self._tick_count % 10 == 0:
            self._log_status()

        # 5. Save positions every 20 ticks (~10 min)
        if self._tick_count % 20 == 0:
            self.tracker.save()

    def _monitor_positions(self):
        """Check each open position against stop-loss rules."""
        for pos in self.tracker.get_open_positions():
            try:
                # Get current price
                current_price = self._get_midpoint(pos.token_id)
                if current_price is None or current_price <= 0:
                    continue
                pos.update_price(current_price)

                # Fee rate for this market
                fee_rate = estimate_fee_rate(pos.entry_price, pos.category)

                # Run risk assessment
                action, reason, updated_since = assess_position_risk(
                    current_prob=current_price,
                    entry_price=pos.entry_price,
                    fee_rate=fee_rate,
                    price_history=pos.price_history,
                    below_ev_since=pos.below_ev_since,
                    days_to_expiry=pos.days_to_expiry,
                    config=self.sl_config,
                )
                pos.below_ev_since = updated_since

                if action == StopLossAction.EXIT:
                    logger.warning(f"STOP-LOSS EXIT: {pos.question[:40]}... reason={reason}")
                    self._exit_position(pos, reason)
                elif action == StopLossAction.REDUCE:
                    logger.warning(f"REDUCE: {pos.question[:40]}... reason={reason}")
                    self._reduce_position(pos, reason)
                elif action == StopLossAction.HOLD:
                    logger.debug(
                        f"HOLD: {pos.question[:30]}... "
                        f"price={current_price:.4f} entry={pos.entry_price:.4f} "
                        f"PnL=${pos.unrealized_pnl():.2f}"
                    )
            except Exception as e:
                logger.error(f"Monitor error for {pos.token_id[:8]}: {e}")

    def _check_exit_orders(self):
        """Check if pending exit orders have been filled; escalate if timed out."""
        for pos in list(self.tracker.positions.values()):
            if pos.status != "exiting" or not pos.exit_order_id:
                continue

            # Check if enough time has passed for timeout
            if time.time() - pos.exit_order_time > self.exit_timeout:
                logger.warning(
                    f"Exit order timeout for {pos.question[:30]}... "
                    f"Cancelling and crossing spread"
                )
                if not self.dry_run:
                    try:
                        self.client.cancel_order(pos.exit_order_id)
                    except Exception:
                        pass
                    # Cross the spread: sell at market (place order at best_bid - 2 ticks)
                    self._force_exit(pos)

    def _refresh_and_enter(self):
        """Scan markets and enter new positions."""
        self.last_market_refresh = time.time()

        # Check VaR before looking for new positions
        can_add, var_pct = check_portfolio_var(
            self.tracker.get_open_positions(),
            self.capital,
            self.max_daily_drawdown_pct,
        )
        if not can_add:
            logger.info(f"VaR limit: {var_pct:.1%} >= {self.max_daily_drawdown_pct:.0%}, skipping scan")
            return

        # Check total exposure limit
        current_exposure = self.tracker.total_exposure()
        max_exposure = self.capital * self.max_total_exposure_pct
        if current_exposure >= max_exposure:
            logger.info(f"Exposure limit: ${current_exposure:.0f} >= ${max_exposure:.0f}, skipping scan")
            return

        # Check how many more positions we can open
        open_count = len(self.tracker.get_open_positions())
        slots_available = self.max_markets - open_count
        if slots_available <= 0:
            return

        # Fetch markets
        try:
            raw_markets = self.client.get_sampling_markets()
            if not raw_markets:
                return
        except Exception as e:
            logger.error(f"Failed to fetch markets: {e}")
            return

        # Fetch orderbooks for candidates with high probability
        vf_cfg = self.config.get("volume_farming", {})
        min_prob = vf_cfg.get("min_probability", 0.90)
        orderbooks = {}
        for market in raw_markets:
            tokens = market.get("tokens", [])
            if not tokens:
                continue
            # Quick check: does any side have high probability?
            yes_price = float(tokens[0].get("price", 0.5))
            no_price = float(tokens[1].get("price", 0.5)) if len(tokens) > 1 else (1 - yes_price)
            if yes_price < min_prob and no_price < min_prob:
                continue
            token_id = tokens[0].get("token_id", "")
            if token_id:
                try:
                    ob = self.client.get_orderbook(token_id)
                    orderbooks[token_id] = {
                        "bids": [{"price": b.price, "size": b.size} for b in ob.bids],
                        "asks": [{"price": a.price, "size": a.size} for a in ob.asks],
                    }
                except Exception:
                    pass

        # Select candidates
        candidates = select_volume_markets(raw_markets, vf_cfg, orderbooks)

        # Enter positions for new candidates (up to available slots)
        entered = 0
        for candidate in candidates:
            if entered >= slots_available:
                break
            # Skip if we already have a position in this market
            if self.tracker.get_position(candidate.token_id):
                continue
            self._enter_position(candidate, max_exposure - current_exposure)
            current_exposure = self.tracker.total_exposure()
            entered += 1

    def _enter_position(self, candidate: VolumeCandidate, remaining_budget: float):
        """Enter a new directional position."""
        # Kelly position sizing
        # For volume farming, we assume our edge = the market is slightly
        # underpricing high-probability events. Use a small edge assumption:
        # estimated_win_prob = min(probability + 0.02, 0.999)
        # This gives Kelly a positive signal while staying conservative.
        estimated_win_prob = min(candidate.probability + 0.02, 0.999)
        position_size = kelly_position_size(
            win_prob=estimated_win_prob,
            entry_price=candidate.entry_price,
            capital=self.capital,
            fraction=self.kelly_fraction,
            max_position_pct=self.max_position_pct,
        )

        if position_size <= 0:
            logger.debug(f"Kelly says skip: {candidate.question[:40]}...")
            return

        # Respect remaining budget
        position_size = min(position_size, remaining_budget)
        if position_size < 1.0:
            return  # Too small

        # Calculate shares
        shares = position_size / candidate.entry_price

        # Compute stop-loss levels
        levels = compute_stop_loss_levels(
            entry_price=candidate.entry_price,
            fee_rate=candidate.fee_rate,
            drawdown_by_prob=self.sl_config.get("drawdown_by_entry_prob"),
            hard_stop=self.sl_config["hard_stop_probability"],
        )

        if self.dry_run:
            logger.info(
                f"[DRY] ENTER {candidate.side} {candidate.question[:45]}...\n"
                f"       prob={candidate.probability:.3f} entry={candidate.entry_price:.3f}\n"
                f"       size=${position_size:.1f} ({shares:.0f} shares)\n"
                f"       kelly_f={self.kelly_fraction} fee={candidate.fee_rate:.4f}\n"
                f"       stop_levels: EV={levels['ev_threshold']:.3f} "
                f"drawdown={levels['drawdown_stop']:.3f} "
                f"hard={levels['hard_stop']:.3f}\n"
                f"       net_profit={candidate.net_profit_per_unit:.4f}/unit "
                f"annual={candidate.annualized_return:.1%}\n"
                f"       expiry={candidate.days_to_expiry:.0f}d"
            )
            # Still track position in dry-run for monitoring logic
            pos = VolumePosition(
                token_id=candidate.token_id,
                complement_token_id=candidate.complement_token_id,
                condition_id=candidate.condition_id,
                question=candidate.question,
                category=candidate.category,
                side=candidate.side,
                entry_price=candidate.entry_price,
                entry_time=time.time(),
                size_usdc=position_size,
                size_shares=shares,
                current_price=candidate.probability,
                stop_loss_price=levels["effective_stop"],
                kelly_fraction=self.kelly_fraction,
                days_to_expiry=candidate.days_to_expiry,
            )
            self.tracker.add_position(pos)
            return

        # Place buy order
        try:
            result = self.client.place_limit_order(
                token_id=candidate.token_id,
                price=candidate.entry_price,
                size=round(shares, 2),
                side="BUY",
            )
            order_id = result.get("orderID", result.get("id", "unknown"))
            logger.info(
                f"ENTER {candidate.side} {candidate.question[:40]}... "
                f"@ {candidate.entry_price:.4f} ${position_size:.1f} "
                f"order={order_id[:12]}"
            )

            pos = VolumePosition(
                token_id=candidate.token_id,
                complement_token_id=candidate.complement_token_id,
                condition_id=candidate.condition_id,
                question=candidate.question,
                category=candidate.category,
                side=candidate.side,
                entry_price=candidate.entry_price,
                entry_time=time.time(),
                size_usdc=position_size,
                size_shares=shares,
                current_price=candidate.probability,
                stop_loss_price=levels["effective_stop"],
                kelly_fraction=self.kelly_fraction,
                days_to_expiry=candidate.days_to_expiry,
            )
            self.tracker.add_position(pos)
            self.ws_client.subscribe(candidate.token_id)

        except Exception as e:
            logger.error(f"Failed to enter position: {e}")

    def _exit_position(self, pos: VolumePosition, reason: str):
        """Exit a position with aggressive limit order."""
        if self.dry_run:
            logger.info(
                f"[DRY] EXIT {pos.question[:40]}... "
                f"entry={pos.entry_price:.4f} current={pos.current_price:.4f} "
                f"reason={reason}"
            )
            pos.close(pos.current_price, reason)
            return

        try:
            # Get best bid for aggressive limit sell
            ob = self.client.get_orderbook(pos.token_id)
            if ob.bids:
                # Place 1 tick below best bid for fast fill
                sell_price = round(ob.bids[0].price - 0.01, 2)
            else:
                sell_price = round(pos.current_price - 0.02, 2)

            sell_price = max(0.01, sell_price)

            result = self.client.place_limit_order(
                token_id=pos.token_id,
                price=sell_price,
                size=round(pos.size_shares, 2),
                side="SELL",
            )
            order_id = result.get("orderID", result.get("id", "unknown"))

            pos.status = "exiting"
            pos.exit_order_id = order_id
            pos.exit_order_time = time.time()
            pos.exit_reason = reason

            logger.info(
                f"Exit order placed: {pos.question[:30]}... "
                f"SELL {pos.size_shares:.0f} @ {sell_price:.4f} "
                f"order={order_id[:12]} reason={reason}"
            )
        except Exception as e:
            logger.error(f"Failed to exit position: {e}")
            # Try force exit
            self._force_exit(pos)

    def _reduce_position(self, pos: VolumePosition, reason: str):
        """Reduce position by half."""
        if self.dry_run:
            logger.info(f"[DRY] REDUCE 50%: {pos.question[:40]}... reason={reason}")
            pos.size_shares /= 2
            pos.size_usdc /= 2
            return

        half_shares = round(pos.size_shares / 2, 2)
        if half_shares < 1:
            # Too small to reduce, just exit
            self._exit_position(pos, reason)
            return

        try:
            ob = self.client.get_orderbook(pos.token_id)
            sell_price = round(ob.bids[0].price - 0.01, 2) if ob.bids else round(pos.current_price - 0.02, 2)
            sell_price = max(0.01, sell_price)

            self.client.place_limit_order(
                token_id=pos.token_id,
                price=sell_price,
                size=half_shares,
                side="SELL",
            )
            pos.size_shares -= half_shares
            pos.size_usdc /= 2
            logger.info(f"Reduced 50%: {pos.question[:30]}... sold {half_shares:.0f} shares")
        except Exception as e:
            logger.error(f"Failed to reduce position: {e}")

    def _force_exit(self, pos: VolumePosition):
        """Force exit by crossing the spread."""
        if self.dry_run:
            pos.close(pos.current_price, pos.exit_reason or "force_exit")
            return

        try:
            # Sell at very low price to guarantee fill
            crash_price = max(0.01, round(pos.current_price * 0.90, 2))
            result = self.client.place_limit_order(
                token_id=pos.token_id,
                price=crash_price,
                size=round(pos.size_shares, 2),
                side="SELL",
            )
            order_id = result.get("orderID", result.get("id", "unknown"))
            pos.close(crash_price, pos.exit_reason or "force_exit")
            logger.warning(f"Force exit: {pos.question[:30]}... @ {crash_price:.4f}")
        except Exception as e:
            logger.error(f"Force exit failed: {e}")
            pos.status = "open"  # Retry next tick

    def _get_midpoint(self, token_id: str) -> float | None:
        """Get current price from WebSocket or HTTP."""
        ws_mid = self.ws_client.get_midpoint(token_id)
        if ws_mid is not None:
            return ws_mid
        try:
            return self.client.get_midpoint(token_id)
        except Exception:
            return None

    def _log_status(self):
        """Log volume farming status."""
        open_positions = self.tracker.get_open_positions()
        total_exposure = self.tracker.total_exposure()
        unrealized = self.tracker.total_unrealized_pnl()
        realized = self.tracker.total_realized_pnl()

        logger.info(
            f"[Volume] positions={len(open_positions)}, "
            f"exposure=${total_exposure:.0f}/${self.capital * self.max_total_exposure_pct:.0f}, "
            f"unrealized=${unrealized:.2f}, realized=${realized:.2f}"
        )
        for pos in open_positions:
            pnl = pos.unrealized_pnl()
            logger.info(
                f"  {pos.side} {pos.question[:35]}... "
                f"entry={pos.entry_price:.3f} now={pos.current_price:.3f} "
                f"PnL=${pnl:.2f} ${pos.size_usdc:.0f}"
            )

    def _shutdown(self):
        """Clean shutdown."""
        logger.info("Volume bot shutting down...")
        self.ws_client.stop()

        # Cancel any pending exit orders
        if not self.dry_run:
            for pos in self.tracker.positions.values():
                if pos.status == "exiting" and pos.exit_order_id:
                    try:
                        self.client.cancel_order(pos.exit_order_id)
                    except Exception:
                        pass

        self.tracker.save()
        self.pnl_tracker.save()

        # Summary
        open_pos = self.tracker.get_open_positions()
        realized = self.tracker.total_realized_pnl()
        unrealized = self.tracker.total_unrealized_pnl()
        logger.info(
            f"Volume bot stopped. "
            f"Open positions: {len(open_pos)}, "
            f"Realized PnL: ${realized:.2f}, "
            f"Unrealized PnL: ${unrealized:.2f}"
        )
