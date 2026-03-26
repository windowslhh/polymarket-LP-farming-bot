"""Risk management for conservative airdrop farming.

Core principle: minimize losses. We'd rather miss LP rewards
than lose money from adverse selection.
"""

import time
from dataclasses import dataclass, field

from loguru import logger


@dataclass
class MarketState:
    """Tracks risk state for a single market."""
    token_id: str
    position_size: float = 0.0  # Net position in USDC (positive = long)
    realized_pnl: float = 0.0
    midpoint_history: list[tuple[float, float]] = field(default_factory=list)  # (timestamp, price)
    paused: bool = False
    pause_reason: str = ""


class RiskManager:
    """Enforces risk limits to control losses."""

    def __init__(self, config: dict):
        self.max_position_per_market = config.get("max_position_per_market", 200)
        self.max_total_position = config.get("max_total_position", 800)
        self.daily_loss_limit = config.get("daily_loss_limit", 20)
        self.inventory_skew_limit = config.get("inventory_skew_limit", 100)
        self.min_days_to_expiry = config.get("min_days_to_expiry", 5)
        self.min_probability = config.get("min_probability", 0.10)
        self.max_probability = config.get("max_probability", 0.90)
        self.midpoint_change_pause_pct = config.get("midpoint_change_pause_pct", 10)

        self.market_states: dict[str, MarketState] = {}
        self.daily_pnl: float = 0.0
        self.daily_pnl_reset_time: float = 0.0
        self._global_pause = False

    def can_trade(self, token_id: str, midpoint: float,
                  days_to_expiry: float | None = None) -> tuple[bool, str]:
        """Check if trading is allowed for this market.

        Returns (allowed, reason) tuple.
        """
        # Check global pause (daily loss limit hit)
        self._maybe_reset_daily_pnl()
        if self._global_pause:
            return False, "Global pause: daily loss limit reached"

        # Check daily loss
        if self.daily_pnl < -self.daily_loss_limit:
            self._global_pause = True
            logger.warning(f"Daily loss limit hit: ${self.daily_pnl:.2f}")
            return False, f"Daily loss ${self.daily_pnl:.2f} exceeds limit"

        # Check probability bounds
        if midpoint < self.min_probability:
            return False, f"Probability {midpoint:.2f} below minimum {self.min_probability}"
        if midpoint > self.max_probability:
            return False, f"Probability {midpoint:.2f} above maximum {self.max_probability}"

        # Check expiry
        if days_to_expiry is not None and days_to_expiry < self.min_days_to_expiry:
            return False, f"Too close to expiry: {days_to_expiry:.1f} days"

        # Check market-specific state
        state = self._get_state(token_id)

        # Check for rapid midpoint movement
        if self._detect_rapid_move(state, midpoint):
            state.paused = True
            state.pause_reason = "Rapid midpoint movement detected"
            return False, state.pause_reason

        # Unpause if previously paused for rapid movement
        if state.paused and state.pause_reason == "Rapid midpoint movement detected":
            state.paused = False

        # Check total position
        total_pos = sum(abs(s.position_size) for s in self.market_states.values())
        if total_pos >= self.max_total_position:
            return False, f"Total position ${total_pos:.0f} at limit"

        # Record midpoint
        state.midpoint_history.append((time.time(), midpoint))
        # Keep last 30 entries only
        if len(state.midpoint_history) > 30:
            state.midpoint_history = state.midpoint_history[-30:]

        return True, "OK"

    def get_inventory_skew(self, token_id: str) -> float:
        """Get inventory skew adjustment in basis points.

        Returns positive value if long (should shift quotes to sell more),
        negative if short (should shift quotes to buy more).
        """
        state = self._get_state(token_id)
        if abs(state.position_size) < 10:  # Ignore tiny positions
            return 0.0

        # Scale skew: at limit, shift spread by 100 bps
        skew_ratio = state.position_size / self.inventory_skew_limit
        skew_bps = skew_ratio * 100  # Max 100 bps shift
        return max(-200, min(200, skew_bps))  # Cap at 200 bps

    def can_increase_position(self, token_id: str, side: str,
                              additional_usdc: float) -> bool:
        """Check if adding to position is within limits."""
        state = self._get_state(token_id)
        projected = state.position_size
        if side == "BUY":
            projected += additional_usdc
        else:
            projected -= additional_usdc

        return abs(projected) <= self.max_position_per_market

    def record_fill(self, token_id: str, side: str, usdc_amount: float,
                    fill_price: float, midpoint_at_fill: float):
        """Record a fill and update position/PnL tracking."""
        state = self._get_state(token_id)

        if side == "BUY":
            state.position_size += usdc_amount
            # Estimate immediate loss from buying above mid
            slippage = (fill_price - midpoint_at_fill) * usdc_amount
        else:
            state.position_size -= usdc_amount
            slippage = (midpoint_at_fill - fill_price) * usdc_amount

        self.daily_pnl -= abs(slippage)
        state.realized_pnl -= abs(slippage)

        logger.info(
            f"Fill: {side} ${usdc_amount:.0f} @ {fill_price:.4f} "
            f"(mid: {midpoint_at_fill:.4f}), "
            f"position: ${state.position_size:.0f}, "
            f"daily PnL: ${self.daily_pnl:.2f}"
        )

    def get_status(self) -> dict:
        """Get current risk status summary."""
        total_pos = sum(abs(s.position_size) for s in self.market_states.values())
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
        """Detect if midpoint moved too fast (>threshold in 5 minutes)."""
        if not state.midpoint_history:
            return False

        now = time.time()
        five_min_ago = now - 300

        # Find oldest price within 5 min window
        old_prices = [p for t, p in state.midpoint_history if t >= five_min_ago]
        if not old_prices:
            return False

        oldest = old_prices[0]
        if oldest == 0:
            return False

        pct_change = abs(current_mid - oldest) / oldest * 100
        return pct_change > self.midpoint_change_pause_pct

    def _maybe_reset_daily_pnl(self):
        """Reset daily PnL counter at midnight UTC (approximate)."""
        now = time.time()
        # Reset every 24 hours
        if now - self.daily_pnl_reset_time > 86400:
            if self.daily_pnl != 0:
                logger.info(f"Daily PnL reset (was ${self.daily_pnl:.2f})")
            self.daily_pnl = 0.0
            self.daily_pnl_reset_time = now
            self._global_pause = False
