"""Unit tests for volume farming strategy core functions.

Tests Kelly sizing, stop-loss calculations, EV thresholds,
and position PnL tracking.
"""

import time
import pytest
from src.stop_loss import (
    kelly_position_size,
    compute_stop_loss_levels,
    check_ev_stop_loss,
    compute_drift_velocity,
    compute_pl_ratio,
    assess_position_risk,
    StopLossAction,
)
from src.volume_position import VolumePosition


# ───────────────── Kelly Position Sizing ─────────────────

class TestKelly:
    def test_no_edge_returns_zero(self):
        """When win_prob == entry_price, no edge → $0."""
        size = kelly_position_size(
            win_prob=0.95, entry_price=0.95,
            capital=1000, fraction=0.25,
        )
        assert size == 0.0

    def test_positive_edge(self):
        """With +2% edge, Kelly returns positive size."""
        size = kelly_position_size(
            win_prob=0.97, entry_price=0.95,
            capital=1000, fraction=0.25,
        )
        assert size > 0

    def test_max_position_cap(self):
        """Position size capped at max_position_pct of capital."""
        size = kelly_position_size(
            win_prob=0.99, entry_price=0.90,
            capital=1000, fraction=1.0,  # Full Kelly
            max_position_pct=0.02,
        )
        assert size <= 1000 * 0.02

    def test_negative_edge_returns_zero(self):
        """When win_prob < entry_price, negative edge → $0."""
        size = kelly_position_size(
            win_prob=0.90, entry_price=0.95,
            capital=1000, fraction=0.25,
        )
        assert size == 0.0

    def test_invalid_price_returns_zero(self):
        assert kelly_position_size(0.95, 0.0, 1000) == 0.0
        assert kelly_position_size(0.95, 1.0, 1000) == 0.0
        assert kelly_position_size(0.95, -0.1, 1000) == 0.0


# ───────────────── Stop-Loss Levels ─────────────────

class TestStopLossLevels:
    def test_ev_threshold_includes_round_trip_fees(self):
        """EV threshold should account for both entry and exit fees."""
        levels = compute_stop_loss_levels(
            entry_price=0.95,
            fee_rate=0.02,
            drawdown_by_prob={"0.95": 0.08},
            hard_stop=0.70,
        )
        # Round-trip: 0.95 * 1.02 / 0.98 = 0.9888
        expected = 0.95 * 1.02 / 0.98
        assert abs(levels["ev_threshold"] - round(expected, 4)) < 0.001

    def test_hard_stop_floor(self):
        levels = compute_stop_loss_levels(
            entry_price=0.95, fee_rate=0.02,
            drawdown_by_prob={"0.95": 0.08},
            hard_stop=0.70,
        )
        assert levels["hard_stop"] == 0.70

    def test_drawdown_by_entry_prob(self):
        levels = compute_stop_loss_levels(
            entry_price=0.97, fee_rate=0.02,
            drawdown_by_prob={"0.97": 0.05, "0.95": 0.08},
            hard_stop=0.70,
        )
        # 0.97 entry with 5% drawdown → stop at 0.97 * 0.95 = 0.9215
        assert abs(levels["drawdown_stop"] - 0.9215) < 0.001


# ───────────────── EV Stop-Loss ─────────────────

class TestEVStopLoss:
    def test_above_threshold_resets_timer(self):
        """Price above EV threshold should reset the timer."""
        should_exit, reason, since = check_ev_stop_loss(
            current_prob=0.98,
            entry_price=0.95,
            fee_rate=0.02,
            below_ev_since=time.time() - 3600,  # Was below for 1 hour
            ev_buffer=0.05,
        )
        assert not should_exit
        assert since is None  # Timer reset

    def test_below_threshold_starts_timer(self):
        """First time below → start timer, don't exit yet."""
        should_exit, reason, since = check_ev_stop_loss(
            current_prob=0.90,
            entry_price=0.95,
            fee_rate=0.02,
            below_ev_since=None,
            ev_buffer=0.05,
        )
        assert not should_exit
        assert since is not None  # Timer started

    def test_persistence_triggers_exit(self):
        """Below threshold for longer than persistence → exit."""
        should_exit, reason, since = check_ev_stop_loss(
            current_prob=0.90,
            entry_price=0.95,
            fee_rate=0.02,
            below_ev_since=time.time() - 3700,  # 61+ minutes ago
            ev_buffer=0.05,
            persistence_minutes=60.0,
        )
        assert should_exit


# ───────────────── Drift Velocity ─────────────────

class TestDriftVelocity:
    def test_flat_price_zero_drift(self):
        now = time.time()
        history = [(now + i * 60, 0.95) for i in range(10)]
        drift = compute_drift_velocity(history)
        assert abs(drift) < 0.001

    def test_declining_price_negative_drift(self):
        now = time.time()
        # Price drops 0.001 per minute over 10 minutes
        history = [(now + i * 60, 0.95 - i * 0.001) for i in range(10)]
        drift = compute_drift_velocity(history)
        assert drift < 0  # Negative = declining

    def test_insufficient_data(self):
        assert compute_drift_velocity([]) == 0.0
        assert compute_drift_velocity([(0, 0.95)]) == 0.0


# ───────────────── P/L Ratio ─────────────────

class TestPLRatio:
    def test_in_profit(self):
        """Current price above entry → infinite P/L ratio."""
        ratio = compute_pl_ratio(0.96, 0.95)
        assert ratio == float("inf")

    def test_at_entry(self):
        ratio = compute_pl_ratio(0.95, 0.95)
        assert ratio == float("inf")

    def test_below_entry(self):
        """Current 0.90, entry 0.95 → P/L = 0.05/0.05 = 1.0"""
        ratio = compute_pl_ratio(0.90, 0.95)
        assert abs(ratio - 1.0) < 0.01

    def test_deep_loss(self):
        """Current 0.80, entry 0.95 → P/L = 0.05/0.15 = 0.33"""
        ratio = compute_pl_ratio(0.80, 0.95)
        assert abs(ratio - 0.333) < 0.01


# ───────────────── Assess Position Risk ─────────────────

class TestAssessRisk:
    def _make_config(self, **overrides):
        cfg = {
            "hard_stop_probability": 0.70,
            "drift_sigma_threshold": 2.0,
            "ev_buffer": 0.05,
            "ev_persistence_minutes": 60.0,
            "ev_stop_enabled": True,
            "near_expiry_exit": False,
            "pl_ratio_min": 0.20,
            "expiry_alert_days": 7,
            "drawdown_by_entry_prob": {
                "0.97": 0.05, "0.95": 0.08, "0.92": 0.12, "0.90": 0.15,
            },
        }
        cfg.update(overrides)
        return cfg

    def test_hard_stop_immediate(self):
        """Below hard stop → immediate EXIT."""
        now = time.time()
        action, reason, _ = assess_position_risk(
            current_prob=0.65,
            entry_price=0.95,
            fee_rate=0.02,
            price_history=[(now, 0.65)],
            below_ev_since=None,
            days_to_expiry=10,
            config=self._make_config(),
        )
        assert action == StopLossAction.EXIT
        assert "Hard stop" in reason

    def test_healthy_position_holds(self):
        """Price near entry → HOLD."""
        now = time.time()
        action, reason, _ = assess_position_risk(
            current_prob=0.96,
            entry_price=0.95,
            fee_rate=0.02,
            price_history=[(now - i * 30, 0.96) for i in range(5)],
            below_ev_since=None,
            days_to_expiry=10,
            config=self._make_config(),
        )
        assert action == StopLossAction.HOLD

    def test_ev_stop_disabled_skips(self):
        """When ev_stop_enabled=False, EV check is skipped."""
        now = time.time()
        action, reason, _ = assess_position_risk(
            current_prob=0.88,
            entry_price=0.95,
            fee_rate=0.02,
            price_history=[(now - i * 30, 0.88) for i in range(5)],
            below_ev_since=now - 7200,  # 2 hours below
            days_to_expiry=10,
            config=self._make_config(ev_stop_enabled=False),
        )
        # Should NOT exit via EV (disabled), may exit via drawdown or hold
        assert "EV stop" not in reason

    def test_near_expiry_disabled(self):
        """near_expiry_exit=False skips expiry check."""
        now = time.time()
        action, reason, _ = assess_position_risk(
            current_prob=0.93,
            entry_price=0.95,
            fee_rate=0.02,
            price_history=[(now, 0.93)],
            below_ev_since=None,
            days_to_expiry=2,
            config=self._make_config(near_expiry_exit=False),
        )
        assert "Near expiry" not in reason


# ───────────────── Position PnL ─────────────────

class TestPositionPnL:
    def _make_pos(self, entry=0.95, current=0.96):
        return VolumePosition(
            token_id="test", complement_token_id=None,
            condition_id="cond", question="Test?",
            category="Test", side="YES",
            entry_price=entry, entry_time=time.time(),
            size_usdc=10.0, size_shares=10.0,
            current_price=current, status="open",
        )

    def test_unrealized_pnl_fee_adjusted(self):
        pos = self._make_pos(entry=0.95, current=0.96)
        pnl = pos.unrealized_pnl(fee_rate=0.02)
        # entry_cost = 0.95 * 1.02 * 10 = 9.69
        # exit_proceeds = 0.96 * 0.98 * 10 = 9.408
        expected = 9.408 - 9.69
        assert abs(pnl - expected) < 0.01

    def test_close_resolved_yes(self):
        pos = self._make_pos(entry=0.95, current=1.0)
        pos.close(1.0, "resolved", fee_rate=0.02)
        # payout = 1.0 * 10 = 10.0
        # entry_cost = 0.95 * 1.02 * 10 = 9.69
        assert pos.realized_pnl > 0
        assert abs(pos.realized_pnl - 0.31) < 0.01

    def test_close_stop_loss(self):
        pos = self._make_pos(entry=0.95, current=0.88)
        pos.close(0.88, "EV stop", fee_rate=0.02)
        # exit_proceeds = 0.88 * 0.98 * 10 = 8.624
        # entry_cost = 0.95 * 1.02 * 10 = 9.69
        assert pos.realized_pnl < 0

    def test_price_staleness(self):
        pos = self._make_pos()
        pos.last_price_update = time.time() - 400
        assert pos.is_price_stale(max_stale_sec=300)

        pos.last_price_update = time.time() - 100
        assert not pos.is_price_stale(max_stale_sec=300)


# ───────────────── Shared Risk Coordinator ─────────────────

from src.shared_risk import SharedRiskCoordinator


class TestSharedRisk:
    def _make_coordinator(self, **overrides):
        config = {
            "shared_risk": {
                "global_daily_loss_limit": 15.0,
                "max_total_exposure_pct": 0.90,
            },
            "capital_allocation": {
                "lp_farming_pct": 0.80,
                "volume_farming_pct": 0.10,
            },
        }
        config.update(overrides)
        return SharedRiskCoordinator(config, total_capital=500.0)

    def test_normal_trading_allowed(self):
        coord = self._make_coordinator()
        allowed, reason = coord.can_trade("vf")
        assert allowed

    def test_combined_loss_triggers_pause(self):
        coord = self._make_coordinator()
        coord.report_lp_pnl(daily_pnl=-8.0, exposure=200)
        coord.report_vf_pnl(daily_pnl=-8.0, exposure=30)
        # Combined: -16 exceeds -15 limit
        allowed, reason = coord.can_trade("vf")
        assert not allowed
        assert "Global pause" in reason

    def test_exposure_limit(self):
        coord = self._make_coordinator()
        # Report exposure near 90% of 500 = 450
        coord.report_lp_pnl(daily_pnl=0, exposure=400)
        coord.report_vf_pnl(daily_pnl=0, exposure=60)
        # Total: 460 >= 450
        allowed, reason = coord.can_trade("vf")
        assert not allowed
        assert "exposure" in reason.lower()

    def test_vf_budget_limit(self):
        coord = self._make_coordinator()
        coord.report_vf_pnl(daily_pnl=0, exposure=55)
        # VF budget = 500 * 0.10 = 50, exposure 55 > 50
        allowed, reason = coord.can_trade("vf")
        assert not allowed
        assert "VF exposure" in reason

    def test_lp_not_blocked_by_vf_budget(self):
        coord = self._make_coordinator()
        coord.report_vf_pnl(daily_pnl=0, exposure=55)
        # LP should still be allowed (VF budget check only applies to VF)
        allowed, reason = coord.can_trade("lp")
        assert allowed

    def test_status_report(self):
        coord = self._make_coordinator()
        coord.report_lp_pnl(daily_pnl=-3.0, exposure=200)
        coord.report_vf_pnl(daily_pnl=-2.0, exposure=40)
        status = coord.get_status()
        assert status["combined_daily_pnl"] == -5.0
        assert status["total_exposure"] == 240
        assert not status["global_pause"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
