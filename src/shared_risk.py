"""Shared risk coordinator for LP + Volume farming bots.

When both bots run in parallel on the same exchange account, this
coordinator ensures:
1. Combined daily loss limit is respected
2. Total portfolio exposure doesn't exceed account balance
3. Volume bot pauses when LP bot hits risk limits (and vice versa)

Usage:
    coordinator = SharedRiskCoordinator(config, total_capital=500)
    # Pass to both bots:
    lp_bot = LPFarmingBot(..., risk_coordinator=coordinator)
    vf_bot = VolumeFarmingBot(..., risk_coordinator=coordinator)

Thread-safe: all methods use a lock for concurrent access.
"""

import threading
import time

from loguru import logger


class SharedRiskCoordinator:
    """Cross-bot risk management for parallel LP + Volume farming."""

    def __init__(self, config: dict, total_capital: float):
        self._lock = threading.Lock()
        self.total_capital = total_capital

        risk_cfg = config.get("shared_risk", {})
        self.global_daily_loss_limit = risk_cfg.get("global_daily_loss_limit", 15.0)
        self.max_total_exposure_pct = risk_cfg.get("max_total_exposure_pct", 0.90)

        cap_cfg = config.get("capital_allocation", {})
        self.lp_pct = cap_cfg.get("lp_farming_pct", 0.80)
        self.vf_pct = cap_cfg.get("volume_farming_pct", 0.10)

        # Tracked state
        self._lp_daily_pnl: float = 0.0
        self._vf_daily_pnl: float = 0.0
        self._lp_exposure: float = 0.0
        self._vf_exposure: float = 0.0
        self._global_pause = False
        self._pause_reason = ""
        self._daily_reset_time = time.time()

    # ── Reporting (called by each bot periodically) ──────────────

    def report_lp_pnl(self, daily_pnl: float, exposure: float):
        """LP bot reports its current daily PnL and exposure."""
        with self._lock:
            self._lp_daily_pnl = daily_pnl
            self._lp_exposure = exposure
            self._check_global_limits()

    def report_vf_pnl(self, daily_pnl: float, exposure: float):
        """Volume bot reports its current daily PnL and exposure."""
        with self._lock:
            self._vf_daily_pnl = daily_pnl
            self._vf_exposure = exposure
            self._check_global_limits()

    # ── Queries (called before taking action) ────────────────────

    def can_trade(self, bot_type: str = "lp") -> tuple[bool, str]:
        """Check if a bot is allowed to trade.

        Returns (allowed, reason).
        """
        with self._lock:
            self._maybe_reset_daily()

            if self._global_pause:
                return False, f"Global pause: {self._pause_reason}"

            combined_pnl = self._lp_daily_pnl + self._vf_daily_pnl
            if combined_pnl < -self.global_daily_loss_limit:
                self._global_pause = True
                self._pause_reason = (
                    f"Combined daily loss ${combined_pnl:.2f} "
                    f"exceeds -${self.global_daily_loss_limit}"
                )
                logger.error(f"GLOBAL PAUSE: {self._pause_reason}")
                return False, self._pause_reason

            # Check total exposure
            total_exposure = self._lp_exposure + self._vf_exposure
            max_exposure = self.total_capital * self.max_total_exposure_pct
            if total_exposure >= max_exposure:
                return False, (
                    f"Total exposure ${total_exposure:.0f} >= "
                    f"${max_exposure:.0f} ({self.max_total_exposure_pct:.0%})"
                )

            # Check per-bot budget
            if bot_type == "vf":
                vf_budget = self.total_capital * self.vf_pct
                if self._vf_exposure >= vf_budget:
                    return False, f"VF exposure ${self._vf_exposure:.0f} >= budget ${vf_budget:.0f}"

            return True, "OK"

    def get_status(self) -> dict:
        """Return current combined risk status."""
        with self._lock:
            return {
                "lp_daily_pnl": self._lp_daily_pnl,
                "vf_daily_pnl": self._vf_daily_pnl,
                "combined_daily_pnl": self._lp_daily_pnl + self._vf_daily_pnl,
                "lp_exposure": self._lp_exposure,
                "vf_exposure": self._vf_exposure,
                "total_exposure": self._lp_exposure + self._vf_exposure,
                "global_pause": self._global_pause,
                "pause_reason": self._pause_reason,
            }

    # ── Internal ─────────────────────────────────────────────────

    def _check_global_limits(self):
        """Check if combined metrics exceed global limits."""
        combined_pnl = self._lp_daily_pnl + self._vf_daily_pnl
        if combined_pnl < -self.global_daily_loss_limit:
            self._global_pause = True
            self._pause_reason = (
                f"Combined daily loss ${combined_pnl:.2f} "
                f"exceeds -${self.global_daily_loss_limit}"
            )
            logger.error(f"GLOBAL PAUSE: {self._pause_reason}")

    def _maybe_reset_daily(self):
        """Reset daily counters every 24 hours."""
        now = time.time()
        if now - self._daily_reset_time > 86400:
            self._lp_daily_pnl = 0.0
            self._vf_daily_pnl = 0.0
            self._global_pause = False
            self._pause_reason = ""
            self._daily_reset_time = now
            logger.info("SharedRiskCoordinator: daily counters reset")
