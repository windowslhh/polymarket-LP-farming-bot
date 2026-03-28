"""Position tracking for volume farming strategy.

Each VolumePosition represents a directional bet on a high-probability
outcome, held until resolution or stop-loss exit.
"""

import json
import os
import time
from dataclasses import dataclass, field

from loguru import logger


@dataclass
class VolumePosition:
    """A single directional position in a high-probability market."""
    token_id: str
    complement_token_id: str | None
    condition_id: str
    question: str
    category: str
    side: str                   # "YES" or "NO"
    entry_price: float          # Entry probability (e.g., 0.95)
    entry_time: float           # Timestamp
    size_usdc: float            # USDC invested
    size_shares: float          # Shares held
    current_price: float        # Latest midpoint
    price_history: list[tuple[float, float]] = field(default_factory=list)
    stop_loss_price: float = 0.75
    kelly_fraction: float = 0.0
    # State machine: pending -> open -> exiting -> closed/resolved
    status: str = "open"
    entry_order_id: str | None = None   # Track entry order for fill confirmation
    exit_order_id: str | None = None
    exit_order_time: float = 0.0
    exit_price: float = 0.0
    exit_reason: str = ""
    realized_pnl: float = 0.0
    days_to_expiry: float = 0.0
    # EV stop-loss persistence timer
    below_ev_since: float | None = None
    # Price staleness tracking
    last_price_update: float = 0.0

    def update_price(self, price: float):
        """Update current price and add to history."""
        now = time.time()
        self.current_price = price
        self.last_price_update = now
        self.price_history.append((now, price))
        # Keep last 480 entries (~4 hours at 30s intervals)
        # Longer history = better drift velocity estimation for multi-day holds
        if len(self.price_history) > 480:
            self.price_history = self.price_history[-480:]

    def is_price_stale(self, max_stale_sec: float = 300) -> bool:
        """Check if price data is stale (no update for max_stale_sec)."""
        if not self.last_price_update:
            return False
        return (time.time() - self.last_price_update) > max_stale_sec

    def unrealized_pnl(self, fee_rate: float = 0.02) -> float:
        """Estimate unrealized P&L if we sold at current price (fee-adjusted)."""
        if self.status != "open":
            return 0.0
        # Mark-to-market: what we'd get selling now minus what we paid
        entry_cost = self.entry_price * (1 + fee_rate) * self.size_shares
        exit_proceeds = self.current_price * (1 - fee_rate) * self.size_shares
        return exit_proceeds - entry_cost

    def close(self, exit_price: float, reason: str, fee_rate: float = 0.02):
        """Mark position as closed with fee-adjusted PnL."""
        self.status = "closed"
        self.exit_price = exit_price
        self.exit_reason = reason
        entry_cost = self.entry_price * (1 + fee_rate) * self.size_shares
        if reason.startswith("resolved"):
            # Resolution: no exit fee, payout is 1.0 (YES) or 0.0 (NO)
            payout = exit_price * self.size_shares  # exit_price=1.0 or 0.0
            self.realized_pnl = payout - entry_cost
        else:
            # Stop-loss exit: pay exit fee
            exit_proceeds = exit_price * (1 - fee_rate) * self.size_shares
            self.realized_pnl = exit_proceeds - entry_cost
        logger.info(
            f"Position closed: {self.question[:40]}... "
            f"entry={self.entry_price:.4f} exit={exit_price:.4f} "
            f"PnL=${self.realized_pnl:.2f} reason={reason}"
        )


class VolumePositionTracker:
    """Manages all volume farming positions."""

    def __init__(self, data_dir: str = "data"):
        self.positions: dict[str, VolumePosition] = {}  # token_id -> position
        self._data_dir = data_dir
        self._data_file = os.path.join(data_dir, "volume_positions.json")
        self._load()

    def add_position(self, position: VolumePosition):
        """Track a new position."""
        self.positions[position.token_id] = position
        logger.info(
            f"New position: {position.question[:40]}... "
            f"{position.side} @ {position.entry_price:.4f} "
            f"${position.size_usdc:.0f} ({position.size_shares:.0f} shares)"
        )

    def get_open_positions(self) -> list[VolumePosition]:
        """Return all confirmed open positions (excludes pending/closed)."""
        return [p for p in self.positions.values() if p.status == "open"]

    def get_pending_positions(self) -> list[VolumePosition]:
        """Return positions awaiting entry fill confirmation."""
        return [p for p in self.positions.values() if p.status == "pending"]

    def get_position(self, token_id: str) -> VolumePosition | None:
        return self.positions.get(token_id)

    def total_exposure(self) -> float:
        """Total USDC invested in open positions."""
        return sum(p.size_usdc for p in self.positions.values() if p.status == "open")

    def total_unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl() for p in self.positions.values() if p.status == "open")

    def total_realized_pnl(self) -> float:
        return sum(p.realized_pnl for p in self.positions.values() if p.status == "closed")

    def save(self):
        """Persist positions to disk."""
        os.makedirs(self._data_dir, exist_ok=True)
        data = {}
        for tid, pos in self.positions.items():
            data[tid] = {
                "token_id": pos.token_id,
                "complement_token_id": pos.complement_token_id,
                "condition_id": pos.condition_id,
                "question": pos.question,
                "category": pos.category,
                "side": pos.side,
                "entry_price": pos.entry_price,
                "entry_time": pos.entry_time,
                "size_usdc": pos.size_usdc,
                "size_shares": pos.size_shares,
                "current_price": pos.current_price,
                "stop_loss_price": pos.stop_loss_price,
                "kelly_fraction": pos.kelly_fraction,
                "status": pos.status,
                "exit_price": pos.exit_price,
                "exit_reason": pos.exit_reason,
                "realized_pnl": pos.realized_pnl,
                "days_to_expiry": pos.days_to_expiry,
            }
        try:
            with open(self._data_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save volume positions: {e}")

    def _load(self):
        """Load positions from disk (recover after restart)."""
        if not os.path.exists(self._data_file):
            return
        try:
            with open(self._data_file) as f:
                data = json.load(f)
            for tid, d in data.items():
                if d.get("status") in ("closed", "resolved"):
                    continue  # Don't reload finished positions
                pos = VolumePosition(
                    token_id=d["token_id"],
                    complement_token_id=d.get("complement_token_id"),
                    condition_id=d["condition_id"],
                    question=d["question"],
                    category=d.get("category", ""),
                    side=d["side"],
                    entry_price=d["entry_price"],
                    entry_time=d["entry_time"],
                    size_usdc=d["size_usdc"],
                    size_shares=d["size_shares"],
                    current_price=d.get("current_price", d["entry_price"]),
                    stop_loss_price=d.get("stop_loss_price", 0.75),
                    kelly_fraction=d.get("kelly_fraction", 0.0),
                    status=d.get("status", "open"),
                    days_to_expiry=d.get("days_to_expiry", 0.0),
                )
                self.positions[tid] = pos
            if self.positions:
                logger.info(f"Loaded {len(self.positions)} volume positions from disk")
        except Exception as e:
            logger.error(f"Failed to load volume positions: {e}")
