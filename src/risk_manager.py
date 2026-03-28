"""Risk management for conservative airdrop farming.

Core principle: minimize losses. We'd rather miss LP rewards
than lose money from adverse selection.

Dual-BID awareness:
  - Each market has a YES token and a NO token
  - We BID on both, so we track position_size (YES) and no_position_size (NO)
  - Net exposure = YES_position - NO_position
  - Inventory skew is based on net exposure, not individual positions
  - Fill rate per market tracks how often we get hit (high rate = spread too tight)
"""

import time
from dataclasses import dataclass, field

from loguru import logger


@dataclass
class MarketState:
    """Tracks risk state for a single market (keyed by YES token_id)."""
    token_id: str
    position_size: float = 0.0       # YES position in USDC (positive = long YES)
    no_position_size: float = 0.0    # NO position in USDC (positive = long NO)
    realized_pnl: float = 0.0
    fill_count: int = 0              # fills since last reset (for fill-rate monitoring)
    fill_count_reset_time: float = field(default_factory=time.time)
    midpoint_history: list[tuple[float, float]] = field(default_factory=list)  # (ts, price)
    paused: bool = False
    pause_reason: str = ""


class RiskManager:
    """Enforces risk limits to control losses."""

    def __init__(self, config: dict):
        self.max_position_per_market = config.get("max_position_per_market", 150)
        self.max_total_position = config.get("max_total_position", 400)
        self.daily_loss_limit = config.get("daily_loss_limit", 10)
        self.inventory_skew_limit = config.get("inventory_skew_limit", 60)
        self.min_days_to_expiry = config.get("min_days_to_expiry", 7)
        self.min_probability = config.get("min_probability", 0.15)
        self.max_probability = config.get("max_probability", 0.85)
        self.midpoint_change_pause_pct = config.get("midpoint_change_pause_pct", 10)
        self.fill_rate_warn = config.get("fill_rate_warn", 5)    # fills/hour → widen
        self.fill_rate_danger = config.get("fill_rate_danger", 10)  # fills/hour → widen more

        self.market_states: dict[str, MarketState] = {}
        # Maps NO token_id → YES token_id (market key)
        self._token_to_market: dict[str, str] = {}
        self.daily_pnl: float = 0.0
        self.daily_pnl_reset_time: float = 0.0
        self._global_pause = False

    def register_market(self, yes_token_id: str, no_token_id: str | None):
        """Register a market's YES/NO token pair.

        Must be called when a market is selected so that fills on the
        NO token can be mapped back to the correct MarketState.
        """
        self._token_to_market[yes_token_id] = yes_token_id
        if no_token_id:
            self._token_to_market[no_token_id] = yes_token_id

    def can_trade(self, token_id: str, midpoint: float,
                  days_to_expiry: float | None = None) -> tuple[bool, str]:
        """Check if trading is allowed for this market.

        Returns (allowed, reason) tuple.
        """
        self._maybe_reset_daily_pnl()
        if self._global_pause:
            return False, "Global pause: daily loss limit reached"

        if self.daily_pnl < -self.daily_loss_limit:
            self._global_pause = True
            logger.warning(f"Daily loss limit hit: ${self.daily_pnl:.2f}")
            return False, f"Daily loss ${self.daily_pnl:.2f} exceeds limit"

        if midpoint < self.min_probability:
            return False, f"Probability {midpoint:.2f} below minimum {self.min_probability}"
        if midpoint > self.max_probability:
            return False, f"Probability {midpoint:.2f} above maximum {self.max_probability}"

        if days_to_expiry is not None and days_to_expiry < self.min_days_to_expiry:
            return False, f"Too close to expiry: {days_to_expiry:.1f} days"

        market_id = self._token_to_market.get(token_id, token_id)
        state = self._get_state(market_id)

        if self._detect_rapid_move(state, midpoint):
            state.paused = True
            state.pause_reason = "Rapid midpoint movement detected"
            return False, state.pause_reason

        if state.paused and state.pause_reason == "Rapid midpoint movement detected":
            state.paused = False

        # Total locked capital = YES + NO per market
        total_locked = state.position_size + state.no_position_size
        if total_locked >= self.max_position_per_market:
            return False, f"Market position ${total_locked:.0f} at limit"

        all_locked = sum(
            s.position_size + s.no_position_size
            for s in self.market_states.values()
        )
        if all_locked >= self.max_total_position:
            return False, f"Total position ${all_locked:.0f} at limit"

        state.midpoint_history.append((time.time(), midpoint))
        if len(state.midpoint_history) > 30:
            state.midpoint_history = state.midpoint_history[-30:]

        return True, "OK"

    def get_inventory_skew(self, token_id: str) -> float:
        """Get inventory skew in basis points based on net YES/NO exposure.

        Net exposure = YES_position - NO_position (both in USDC)
        Positive skew (long YES) → shift quotes up → tighten ask, widen bid
        Negative skew (long NO = short YES) → shift quotes down → tighten bid, widen ask
        """
        market_id = self._token_to_market.get(token_id, token_id)
        state = self._get_state(market_id)
        net_exposure = state.position_size - state.no_position_size
        if abs(net_exposure) < 10:
            return 0.0
        skew_ratio = net_exposure / self.inventory_skew_limit
        skew_bps = skew_ratio * 100
        return max(-200, min(200, skew_bps))

    def get_fill_rate_adjustment(self, token_id: str) -> float:
        """Return spread_target_pct adjustment based on fill rate.

        High fill rate means our spread is too tight → widen.
        Returns additive adjustment to spread_target_pct (0.0 to +0.18).
        """
        market_id = self._token_to_market.get(token_id, token_id)
        state = self._get_state(market_id)
        self._maybe_reset_fill_count(state)

        if state.fill_count >= self.fill_rate_danger:
            return 0.15   # very high fill rate → push toward max_spread
        elif state.fill_count >= self.fill_rate_warn:
            return 0.08   # moderate fill rate → nudge wider
        return 0.0

    def record_fill(self, token_id: str, side: str, usdc_amount: float,
                    fill_price: float, midpoint_at_fill: float):
        """Record a fill and update position/PnL tracking."""
        market_id = self._token_to_market.get(token_id, token_id)
        state = self._get_state(market_id)
        is_no_token = (token_id != market_id)

        if side == "BUY" and not is_no_token:
            state.position_size += usdc_amount       # bought YES
        elif side == "BUY" and is_no_token:
            state.no_position_size += usdc_amount    # bought NO

        slippage = abs(fill_price - midpoint_at_fill) * usdc_amount
        self.daily_pnl -= slippage
        state.realized_pnl -= slippage
        state.fill_count += 1

        token_label = "NO" if is_no_token else "YES"
        logger.info(
            f"Fill: BUY {token_label} ${usdc_amount:.0f} @ {fill_price:.4f} "
            f"(mid: {midpoint_at_fill:.4f}), "
            f"YES pos: ${state.position_size:.0f}, NO pos: ${state.no_position_size:.0f}, "
            f"daily PnL: ${self.daily_pnl:.2f}"
        )

    def get_status(self) -> dict:
        """Get current risk status summary."""
        total_pos = sum(
            s.position_size + s.no_position_size
            for s in self.market_states.values()
        )
        return {
            "daily_pnl": round(self.daily_pnl, 2),
            "total_position": round(total_pos, 2),
            "markets_active": len([s for s in self.market_states.values() if not s.paused]),
            "markets_paused": len([s for s in self.market_states.values() if s.paused]),
            "global_pause": self._global_pause,
        }

    def _get_state(self, token_id: str) -> MarketState:
        if token_id not in self.market_states:
            self.market_states[token_id] = MarketState(token_id=token_id)
        return self.market_states[token_id]

    def _detect_rapid_move(self, state: MarketState, current_mid: float) -> bool:
        if not state.midpoint_history:
            return False
        now = time.time()
        five_min_ago = now - 300
        old_prices = [p for t, p in state.midpoint_history if t >= five_min_ago]
        if not old_prices:
            return False
        oldest = old_prices[0]
        if oldest == 0:
            return False
        pct_change = abs(current_mid - oldest) / oldest * 100
        return pct_change > self.midpoint_change_pause_pct

    def _maybe_reset_daily_pnl(self):
        now = time.time()
        if now - self.daily_pnl_reset_time > 86400:
            if self.daily_pnl != 0:
                logger.info(f"Daily PnL reset (was ${self.daily_pnl:.2f})")
            self.daily_pnl = 0.0
            self.daily_pnl_reset_time = now
            self._global_pause = False

    def _maybe_reset_fill_count(self, state: MarketState):
        """Reset fill counter every hour."""
        if time.time() - state.fill_count_reset_time > 3600:
            state.fill_count = 0
            state.fill_count_reset_time = time.time()
