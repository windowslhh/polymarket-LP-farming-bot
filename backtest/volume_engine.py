"""Backtesting engine for volume farming strategy.

Simulates directional buying of high-probability outcomes with
dynamic stop-loss. Tracks:
- Entry/exit timing and prices
- Stop-loss trigger reasons
- P&L including fees
- False alarms (whipsawed exits)
- Position sizing via Kelly
"""

import math
from dataclasses import dataclass, field

from backtest.volume_data import HighProbMarket
from src.stop_loss import (
    StopLossAction,
    check_ev_stop_loss,
    compute_drift_sigma,
    compute_drift_velocity,
    compute_pl_ratio,
    compute_stop_loss_levels,
    kelly_position_size,
    should_emergency_exit,
)


@dataclass
class VolumeBacktestConfig:
    """Parameters for volume farming backtest."""
    capital: float = 500.0
    # Kelly
    kelly_fraction: float = 0.25
    max_position_pct: float = 0.02
    edge_assumption: float = 0.02   # Assumed edge over market price
    # Stop-loss
    ev_stop_enabled: bool = True    # EV stop saves ~$4 vs hard-stop-only (backtest tuned)
    ev_buffer: float = 0.05
    ev_persistence_minutes: float = 60.0
    hard_stop_probability: float = 0.70
    drift_sigma_threshold: float = 2.0
    pl_ratio_min: float = 0.20
    near_expiry_exit: bool = False  # Near-expiry exit (usually too aggressive)
    drawdown_by_entry_prob: dict = field(default_factory=lambda: {
        "0.97": 0.05,
        "0.95": 0.08,
        "0.92": 0.12,
        "0.90": 0.15,
    })
    # Execution
    slippage_bps: int = 10          # Entry/exit slippage in bps
    exit_spread_cost: float = 0.005  # Additional cost on exit (crossing spread)


@dataclass
class TradeRecord:
    """Record of a single trade (entry or exit)."""
    step: int
    timestamp: int
    action: str         # "ENTER" / "EXIT" / "REDUCE" / "RESOLVED"
    price: float
    shares: float
    usdc: float
    fee: float
    reason: str = ""


@dataclass
class PositionResult:
    """Result of a single position lifecycle."""
    market_id: str
    market_name: str
    category: str
    side: str
    entry_price: float
    exit_price: float
    resolution: str     # "YES" / "NO"
    resolved_correctly: bool
    entry_step: int
    exit_step: int
    hold_days: float
    size_usdc: float
    shares: float
    gross_pnl: float
    fees_paid: float
    net_pnl: float
    exit_reason: str
    max_drawdown_pct: float     # Max adverse price movement during hold
    min_price_seen: float
    was_whipsawed: bool = False  # Exited but would have been profitable if held


@dataclass
class VolumeBacktestResult:
    """Complete backtest results for one config across multiple markets."""
    config: VolumeBacktestConfig
    positions: list[PositionResult] = field(default_factory=list)

    @property
    def total_trades(self) -> int:
        return len(self.positions)

    @property
    def winning_trades(self) -> int:
        return sum(1 for p in self.positions if p.net_pnl > 0)

    @property
    def losing_trades(self) -> int:
        return sum(1 for p in self.positions if p.net_pnl <= 0)

    @property
    def win_rate(self) -> float:
        return self.winning_trades / self.total_trades * 100 if self.total_trades else 0

    @property
    def total_net_pnl(self) -> float:
        return sum(p.net_pnl for p in self.positions)

    @property
    def total_gross_pnl(self) -> float:
        return sum(p.gross_pnl for p in self.positions)

    @property
    def total_fees(self) -> float:
        return sum(p.fees_paid for p in self.positions)

    @property
    def total_volume(self) -> float:
        """Total USDC volume traded (entry + exit)."""
        return sum(p.size_usdc * 2 for p in self.positions)

    @property
    def avg_hold_days(self) -> float:
        if not self.positions:
            return 0
        return sum(p.hold_days for p in self.positions) / len(self.positions)

    @property
    def max_single_loss(self) -> float:
        if not self.positions:
            return 0
        return min(p.net_pnl for p in self.positions)

    @property
    def whipsaw_count(self) -> int:
        return sum(1 for p in self.positions if p.was_whipsawed)

    @property
    def stop_loss_exits(self) -> int:
        return sum(1 for p in self.positions if p.exit_reason not in ("resolved", ""))

    @property
    def avg_drawdown(self) -> float:
        if not self.positions:
            return 0
        return sum(p.max_drawdown_pct for p in self.positions) / len(self.positions)

    @property
    def sharpe_ratio(self) -> float:
        if len(self.positions) < 2:
            return 0
        pnls = [p.net_pnl for p in self.positions]
        mean = sum(pnls) / len(pnls)
        var = sum((x - mean) ** 2 for x in pnls) / (len(pnls) - 1)
        std = math.sqrt(var) if var > 0 else 0.001
        # Annualize: assume ~20 trades/year
        return (mean / std) * math.sqrt(min(len(pnls), 50))

    def exit_reason_summary(self) -> dict[str, int]:
        reasons: dict[str, int] = {}
        for p in self.positions:
            r = p.exit_reason or "resolved"
            reasons[r] = reasons.get(r, 0) + 1
        return reasons


def run_volume_backtest(
    markets: list[HighProbMarket],
    config: VolumeBacktestConfig,
) -> VolumeBacktestResult:
    """Run volume farming backtest across multiple markets.

    For each market:
    1. Enter position at step 0 (buy at ask + slippage)
    2. Monitor every 6 steps (~30 min) with stop-loss checks
    3. Exit on stop-loss trigger or hold until resolution
    4. Record P&L
    """
    result = VolumeBacktestResult(config=config)

    for market in markets:
        pos_result = _simulate_position(market, config)
        result.positions.append(pos_result)

    return result


def _simulate_position(
    market: HighProbMarket,
    config: VolumeBacktestConfig,
) -> PositionResult:
    """Simulate a single position lifecycle."""
    candles = market.candles
    if not candles:
        return _empty_result(market)

    # Entry: buy at first candle's ask + slippage
    entry_candle = candles[0]
    entry_price = entry_candle.best_ask * (1 + config.slippage_bps / 10000)
    entry_price = min(0.99, entry_price)

    # Position sizing
    estimated_prob = min(market.initial_price + config.edge_assumption, 0.999)
    position_usdc = kelly_position_size(
        win_prob=estimated_prob,
        entry_price=entry_price,
        capital=config.capital,
        fraction=config.kelly_fraction,
        max_position_pct=config.max_position_pct,
    )

    if position_usdc <= 0:
        # Kelly says don't bet; use minimum viable position for testing
        position_usdc = config.capital * config.max_position_pct

    shares = position_usdc / entry_price
    entry_fee = market.taker_fee_rate * position_usdc

    # Stop-loss levels
    sl_levels = compute_stop_loss_levels(
        entry_price=entry_price,
        fee_rate=market.taker_fee_rate,
        drawdown_by_prob=config.drawdown_by_entry_prob,
        hard_stop=config.hard_stop_probability,
    )

    # Monitoring loop
    check_every = 6  # Every 6 steps = 30 minutes
    price_history: list[tuple[float, float]] = []
    below_ev_since: float | None = None
    min_price_seen = entry_price
    max_drawdown_pct = 0.0
    exit_step = len(candles) - 1
    exit_price = entry_price
    exit_reason = "resolved"
    exited = False
    current_shares = shares

    for i, candle in enumerate(candles):
        mid = candle.midpoint
        timestamp = candle.timestamp

        # Track min price and drawdown
        if mid < min_price_seen:
            min_price_seen = mid
        dd = (entry_price - mid) / entry_price
        if dd > max_drawdown_pct:
            max_drawdown_pct = dd

        # Add to price history
        price_history.append((float(timestamp), mid))
        if len(price_history) > 120:
            price_history = price_history[-120:]

        # Only check stop-loss every N steps
        if i == 0 or i % check_every != 0:
            continue

        # Days to expiry
        remaining_steps = len(candles) - i
        days_remaining = remaining_steps / 288.0

        # Build stop-loss config dict
        sl_config = {
            "hard_stop_probability": config.hard_stop_probability,
            "drift_sigma_threshold": config.drift_sigma_threshold,
            "ev_buffer": config.ev_buffer,
            "ev_persistence_minutes": config.ev_persistence_minutes,
            "pl_ratio_min": config.pl_ratio_min,
            "expiry_alert_days": 7,
            "drawdown_by_entry_prob": config.drawdown_by_entry_prob,
            "ev_stop_enabled": config.ev_stop_enabled,
            "near_expiry_exit": config.near_expiry_exit,
        }

        # Run risk assessment
        # Use assess_position_risk from stop_loss module
        action, reason, updated_since = _assess_risk(
            current_prob=mid,
            entry_price=entry_price,
            fee_rate=market.taker_fee_rate,
            price_history=price_history,
            below_ev_since=below_ev_since,
            days_to_expiry=days_remaining,
            config=sl_config,
        )
        below_ev_since = updated_since

        if action == StopLossAction.EXIT:
            exit_step = i
            exit_price = candle.best_bid * (1 - config.slippage_bps / 10000)
            exit_price = max(0.01, exit_price)
            exit_reason = reason
            exited = True
            break
        elif action == StopLossAction.REDUCE:
            # Reduce half, continue monitoring
            current_shares /= 2
            position_usdc /= 2
            # Don't break, keep monitoring

    # If not exited, resolve at final price
    if not exited:
        if market.resolution == "YES":
            exit_price = 1.0  # Resolves YES → each share worth $1
        else:
            exit_price = 0.0  # Resolves NO → shares worthless

    # Calculate P&L
    exit_fee = market.taker_fee_rate * (exit_price * current_shares) if exited else 0.0
    gross_pnl = (exit_price - entry_price) * current_shares
    total_fees = entry_fee + exit_fee
    net_pnl = gross_pnl - total_fees

    # Whipsaw detection: exited at a loss, but resolution would have been profitable
    was_whipsawed = False
    if exited and net_pnl < 0 and market.resolution == "YES":
        # Would have made money if held
        hypothetical_pnl = (1.0 - entry_price) * shares - entry_fee
        if hypothetical_pnl > 0:
            was_whipsawed = True

    hold_days = (exit_step * 300) / 86400.0

    return PositionResult(
        market_id=market.market_id,
        market_name=market.question,
        category=market.category,
        side="YES",
        entry_price=round(entry_price, 4),
        exit_price=round(exit_price, 4),
        resolution=market.resolution,
        resolved_correctly=market.resolution == "YES",
        entry_step=0,
        exit_step=exit_step,
        hold_days=round(hold_days, 1),
        size_usdc=round(position_usdc, 2),
        shares=round(current_shares, 2),
        gross_pnl=round(gross_pnl, 4),
        fees_paid=round(total_fees, 4),
        net_pnl=round(net_pnl, 4),
        exit_reason=exit_reason,
        max_drawdown_pct=round(max_drawdown_pct, 4),
        min_price_seen=round(min_price_seen, 4),
        was_whipsawed=was_whipsawed,
    )


def _assess_risk(
    current_prob: float,
    entry_price: float,
    fee_rate: float,
    price_history: list[tuple[float, float]],
    below_ev_since: float | None,
    days_to_expiry: float,
    config: dict,
) -> tuple[StopLossAction, str, float | None]:
    """Wrapper around stop_loss.assess_position_risk for backtesting.

    In backtest, we use candle timestamps instead of real time.
    Need to convert persistence check to use candle-based time.
    """
    hard_stop = config.get("hard_stop_probability", 0.75)
    sigma_threshold = config.get("drift_sigma_threshold", 2.0)
    ev_buffer = config.get("ev_buffer", 0.02)
    persistence_min = config.get("ev_persistence_minutes", 5.0)
    pl_ratio_min = config.get("pl_ratio_min", 0.20)
    expiry_alert_days = config.get("expiry_alert_days", 7)
    drawdown_by_prob = config.get("drawdown_by_entry_prob")
    ev_stop_enabled = config.get("ev_stop_enabled", True)
    near_expiry_exit = config.get("near_expiry_exit", True)

    # Priority 1: Hard stop
    if current_prob < hard_stop:
        return StopLossAction.EXIT, f"Hard stop: {current_prob:.3f} < {hard_stop}", below_ev_since

    # Priority 2: Drift emergency
    if len(price_history) >= 6:
        drift_vel = compute_drift_velocity(price_history)
        drift_sig = compute_drift_sigma(price_history)
        if should_emergency_exit(drift_vel, drift_sig, sigma_threshold):
            return (
                StopLossAction.EXIT,
                f"Drift: vel={drift_vel:.4f}/d > {sigma_threshold}σ({drift_sig:.4f})",
                below_ev_since,
            )

    # Priority 3: EV stop with persistence (use candle timestamps)
    if ev_stop_enabled:
        # Round-trip breakeven: entry + exit fees
        p_breakeven = entry_price * (1.0 + fee_rate) / (1.0 - fee_rate)
        p_breakeven = min(p_breakeven, 0.999)
        threshold = p_breakeven - ev_buffer

        if current_prob < threshold:
            if below_ev_since is None:
                below_ev_since = price_history[-1][0] if price_history else 0
            else:
                # Check persistence using candle timestamps
                elapsed_min = (price_history[-1][0] - below_ev_since) / 60.0
                if elapsed_min >= persistence_min:
                    return (
                        StopLossAction.EXIT,
                        f"EV stop: {current_prob:.3f} < {threshold:.3f} for {elapsed_min:.0f}min",
                        below_ev_since,
                    )
        else:
            below_ev_since = None

    # Priority 4: Drawdown stop
    sl_levels = compute_stop_loss_levels(entry_price, fee_rate, drawdown_by_prob, hard_stop)
    if current_prob < sl_levels["drawdown_stop"]:
        if below_ev_since is not None:
            elapsed_min = (price_history[-1][0] - below_ev_since) / 60.0
            if elapsed_min >= persistence_min:
                return (
                    StopLossAction.EXIT,
                    f"Drawdown: {current_prob:.3f} < {sl_levels['drawdown_stop']:.3f}",
                    below_ev_since,
                )

    # Priority 5: P/L ratio
    pl_ratio = compute_pl_ratio(current_prob, entry_price)
    if 0 < pl_ratio < pl_ratio_min:
        return (
            StopLossAction.REDUCE,
            f"P/L ratio {pl_ratio:.2f} < {pl_ratio_min}",
            below_ev_since,
        )

    # Priority 6: Near-expiry
    if near_expiry_exit and days_to_expiry < expiry_alert_days and current_prob < entry_price - 0.01:
        return (
            StopLossAction.EXIT,
            f"Near expiry ({days_to_expiry:.1f}d) + price below entry",
            below_ev_since,
        )

    return StopLossAction.HOLD, "OK", below_ev_since


def _empty_result(market: HighProbMarket) -> PositionResult:
    return PositionResult(
        market_id=market.market_id,
        market_name=market.question,
        category=market.category,
        side="YES",
        entry_price=market.initial_price,
        exit_price=market.initial_price,
        resolution=market.resolution,
        resolved_correctly=False,
        entry_step=0,
        exit_step=0,
        hold_days=0,
        size_usdc=0,
        shares=0,
        gross_pnl=0,
        fees_paid=0,
        net_pnl=0,
        exit_reason="no_data",
        max_drawdown_pct=0,
        min_price_seen=market.initial_price,
    )
