"""Dynamic stop-loss calculator for volume farming.

Core risk management based on:
- Kelly criterion position sizing
- Bayesian EV-based stop-loss with persistence confirmation
- Odds drift velocity monitoring (2σ emergency exit)
- Multi-layer stop-loss (EV → drawdown → hard stop)
- Portfolio VaR control

Key design: EV stop-loss uses buffer + persistence to avoid
being shaken out by normal volatility.
"""

import math
import time
from enum import Enum

from loguru import logger


class StopLossAction(Enum):
    HOLD = "hold"
    REDUCE = "reduce"
    EXIT = "exit"


# ---------------------------------------------------------------------------
# Kelly Criterion Position Sizing
# ---------------------------------------------------------------------------

def kelly_position_size(
    win_prob: float,
    entry_price: float,
    capital: float,
    fraction: float = 0.25,
    max_position_pct: float = 0.02,
) -> float:
    """Calculate position size using fractional Kelly criterion.

    For a binary market:
        payout ratio b = (1 - entry_price) / entry_price
        f* = (p(b+1) - 1) / b

    Uses 1/4 Kelly by default (fraction=0.25) to reduce volatility.
    Capped at max_position_pct of capital.

    Returns USDC amount to invest.
    """
    if entry_price <= 0 or entry_price >= 1.0:
        return 0.0

    b = (1.0 - entry_price) / entry_price
    if b <= 0:
        return 0.0

    f_star = (win_prob * (b + 1) - 1) / b
    if f_star <= 0:
        return 0.0  # Negative Kelly → don't bet

    kelly_amount = capital * f_star * fraction
    max_amount = capital * max_position_pct
    return min(kelly_amount, max_amount)


# ---------------------------------------------------------------------------
# Stop-Loss Price Computation (Three Layers)
# ---------------------------------------------------------------------------

def compute_stop_loss_levels(
    entry_price: float,
    fee_rate: float = 0.0,
    drawdown_by_prob: dict[str, float] | None = None,
    hard_stop: float = 0.75,
) -> dict:
    """Compute three-layer stop-loss levels.

    Layer 1 - EV threshold: probability where E[return] = 0
    Layer 2 - Drawdown stop: dynamic based on entry probability
    Layer 3 - Hard stop: absolute floor (default 0.75)

    Returns dict with all levels and the effective (most conservative) stop.
    """
    if drawdown_by_prob is None:
        drawdown_by_prob = {
            "0.97": 0.03,
            "0.95": 0.05,
            "0.92": 0.08,
            "0.90": 0.10,
        }

    # Layer 1: EV breakeven probability
    # E[return] = p*(1-entry) - (1-p)*entry - fee*entry = 0
    # p*(1-entry) + p*entry = entry + fee*entry
    # p = entry*(1+fee)  ... but p = (entry + fee*entry)/(1) = entry*(1+fee)
    # More precisely: p_breakeven = (entry_price + fee_rate * entry_price)
    #   / (1.0 + fee_rate * entry_price)  -- not quite
    # Correct derivation:
    # p*(1-entry) - (1-p)*entry - fee*entry = 0
    # p - p*entry - entry + p*entry - fee*entry = 0
    # p = entry*(1 + fee)
    ev_threshold = entry_price * (1.0 + fee_rate)
    ev_threshold = min(ev_threshold, 0.999)  # Clamp

    # Layer 2: Dynamic drawdown based on entry probability
    drawdown_coeff = _get_drawdown_coefficient(entry_price, drawdown_by_prob)
    drawdown_stop = entry_price * (1.0 - drawdown_coeff)

    # Effective stop = highest (most conservative) of all three
    effective = max(ev_threshold, drawdown_stop, hard_stop)

    return {
        "ev_threshold": round(ev_threshold, 4),
        "drawdown_stop": round(drawdown_stop, 4),
        "hard_stop": hard_stop,
        "drawdown_coeff": drawdown_coeff,
        "effective_stop": round(effective, 4),
    }


def _get_drawdown_coefficient(
    entry_price: float,
    drawdown_by_prob: dict[str, float],
) -> float:
    """Get drawdown tolerance based on entry probability.

    Higher entry price → less drawdown tolerance (thinner profit margin).
    Interpolates between configured thresholds.
    """
    # Sort thresholds descending
    thresholds = sorted(
        [(float(k), v) for k, v in drawdown_by_prob.items()],
        key=lambda x: x[0],
        reverse=True,
    )

    # Find matching tier
    for prob_threshold, coeff in thresholds:
        if entry_price >= prob_threshold:
            return coeff

    # Below all thresholds, use the most generous
    return thresholds[-1][1] if thresholds else 0.10


# ---------------------------------------------------------------------------
# EV Stop-Loss with Persistence (anti-whipsaw)
# ---------------------------------------------------------------------------

def check_ev_stop_loss(
    current_prob: float,
    entry_price: float,
    fee_rate: float,
    below_ev_since: float | None,
    ev_buffer: float = 0.02,
    persistence_minutes: float = 5.0,
) -> tuple[bool, str, float | None]:
    """Check EV-based stop-loss with buffer and persistence.

    To avoid being shaken out by normal volatility:
    1. Buffer: threshold = breakeven - buffer (2% default)
    2. Persistence: must stay below threshold for N minutes

    Returns (should_exit, reason, updated_below_ev_since).
    """
    p_breakeven = entry_price * (1.0 + fee_rate)
    threshold = p_breakeven - ev_buffer

    if current_prob >= threshold:
        return False, "EV OK", None  # Reset timer

    # Below threshold
    now = time.time()
    if below_ev_since is None:
        return (
            False,
            f"EV warning: prob {current_prob:.3f} < threshold {threshold:.3f}, observing",
            now,
        )

    elapsed_min = (now - below_ev_since) / 60.0
    if elapsed_min >= persistence_minutes:
        return (
            True,
            f"EV stop: prob {current_prob:.3f} below threshold for {elapsed_min:.1f}min",
            below_ev_since,
        )

    return (
        False,
        f"EV warning: {elapsed_min:.1f}min / {persistence_minutes:.0f}min",
        below_ev_since,
    )


# ---------------------------------------------------------------------------
# Odds Drift Velocity (Early Warning)
# ---------------------------------------------------------------------------

def compute_drift_velocity(
    price_history: list[tuple[float, float]],
) -> float:
    """Calculate rate of probability change (dp/dt) in prob units per day.

    Uses linear regression slope on recent price history.
    Negative value = probability declining (bad for long positions).
    """
    if len(price_history) < 3:
        return 0.0

    # Convert timestamps to days
    t0 = price_history[0][0]
    xs = [(t - t0) / 86400.0 for t, _ in price_history]
    ys = [p for _, p in price_history]

    n = len(xs)
    sum_x = sum(xs)
    sum_y = sum(ys)
    sum_xy = sum(x * y for x, y in zip(xs, ys))
    sum_x2 = sum(x * x for x in xs)

    denom = n * sum_x2 - sum_x * sum_x
    if abs(denom) < 1e-12:
        return 0.0

    slope = (n * sum_xy - sum_x * sum_y) / denom
    return slope  # prob units per day


def compute_drift_sigma(
    price_history: list[tuple[float, float]],
) -> float:
    """Compute standard deviation of price changes.

    Used to define what counts as "normal" volatility.
    Returns sigma in probability units per day.
    """
    if len(price_history) < 3:
        return 0.01  # Default small sigma

    # Calculate per-interval changes, normalized to daily
    changes = []
    for i in range(1, len(price_history)):
        dt = price_history[i][0] - price_history[i - 1][0]
        if dt <= 0:
            continue
        dp = price_history[i][1] - price_history[i - 1][1]
        # Normalize to daily rate
        daily_dp = dp * (86400.0 / dt)
        changes.append(daily_dp)

    if not changes:
        return 0.01

    mean = sum(changes) / len(changes)
    variance = sum((c - mean) ** 2 for c in changes) / len(changes)
    sigma = math.sqrt(variance)
    return max(sigma, 0.001)  # Floor to avoid div-by-zero


def should_emergency_exit(
    drift_velocity: float,
    drift_sigma: float,
    sigma_threshold: float = 2.0,
) -> bool:
    """Check if drift exceeds threshold (2σ by default).

    Adverse drift = probability falling (negative velocity for long positions).
    If |adverse drift| > sigma_threshold × σ → emergency exit.
    """
    # For long YES positions, adverse = negative drift
    if drift_velocity >= 0:
        return False  # Price going up, no emergency

    return abs(drift_velocity) > sigma_threshold * drift_sigma


# ---------------------------------------------------------------------------
# Profit/Loss Ratio Check
# ---------------------------------------------------------------------------

def compute_pl_ratio(
    current_price: float,
    entry_price: float,
) -> float:
    """Compute current profit/loss ratio.

    P/L ratio = potential_profit / potential_loss
    potential_profit = (1.0 - entry_price)  (if resolves YES)
    potential_loss = (entry_price - current_price)  (if we exit now)

    Returns ratio; < 0.20 means loss potential is 5x profit potential.
    """
    potential_profit = 1.0 - entry_price
    potential_loss = entry_price - current_price

    if potential_loss <= 0:
        return float("inf")  # No loss, in profit
    if potential_profit <= 0:
        return 0.0

    return potential_profit / potential_loss


# ---------------------------------------------------------------------------
# Comprehensive Risk Assessment
# ---------------------------------------------------------------------------

def assess_position_risk(
    current_prob: float,
    entry_price: float,
    fee_rate: float,
    price_history: list[tuple[float, float]],
    below_ev_since: float | None,
    days_to_expiry: float,
    config: dict,
) -> tuple[StopLossAction, str, float | None]:
    """Assess all risk signals and return recommended action.

    Priority order:
    1. Hard stop (immediate, no persistence)
    2. Drift 2σ (immediate)
    3. EV stop (with buffer + persistence)
    4. Drawdown stop (with persistence)
    5. P/L ratio degradation → REDUCE
    6. Near-expiry risk → EXIT

    Returns (action, reason, updated_below_ev_since).
    """
    hard_stop = config.get("hard_stop_probability", 0.70)
    sigma_threshold = config.get("drift_sigma_threshold", 2.0)
    ev_buffer = config.get("ev_buffer", 0.05)
    persistence_min = config.get("ev_persistence_minutes", 60.0)
    ev_stop_enabled = config.get("ev_stop_enabled", True)
    near_expiry_exit = config.get("near_expiry_exit", False)
    pl_ratio_min = config.get("pl_ratio_min", 0.20)
    expiry_alert_days = config.get("expiry_alert_days", 7)
    drawdown_by_prob = config.get("drawdown_by_entry_prob", None)

    # Priority 1: Hard stop (immediate)
    if current_prob < hard_stop:
        return StopLossAction.EXIT, f"Hard stop: prob {current_prob:.3f} < {hard_stop}", below_ev_since

    # Priority 2: Drift emergency (immediate)
    drift_vel = compute_drift_velocity(price_history)
    drift_sig = compute_drift_sigma(price_history)
    if should_emergency_exit(drift_vel, drift_sig, sigma_threshold):
        return (
            StopLossAction.EXIT,
            f"Drift emergency: velocity {drift_vel:.4f}/day > {sigma_threshold}σ ({drift_sig:.4f})",
            below_ev_since,
        )

    # Priority 3: EV stop (with persistence) — can be disabled
    updated_since = below_ev_since
    if ev_stop_enabled:
        ev_exit, ev_reason, updated_since = check_ev_stop_loss(
            current_prob, entry_price, fee_rate, below_ev_since,
            ev_buffer, persistence_min,
        )
        if ev_exit:
            return StopLossAction.EXIT, ev_reason, updated_since

    # Priority 4: Drawdown stop
    levels = compute_stop_loss_levels(entry_price, fee_rate, drawdown_by_prob, hard_stop)
    if current_prob < levels["drawdown_stop"]:
        # Drawdown also uses persistence (same timer as EV)
        if updated_since is not None:
            elapsed = (time.time() - updated_since) / 60.0
            if elapsed >= persistence_min:
                return (
                    StopLossAction.EXIT,
                    f"Drawdown stop: prob {current_prob:.3f} < {levels['drawdown_stop']:.3f}",
                    updated_since,
                )

    # Priority 5: P/L ratio degradation → reduce
    pl_ratio = compute_pl_ratio(current_prob, entry_price)
    if 0 < pl_ratio < pl_ratio_min:
        return (
            StopLossAction.REDUCE,
            f"P/L ratio {pl_ratio:.2f} < {pl_ratio_min} → reduce position",
            updated_since,
        )

    # Priority 6: Near-expiry risk — can be disabled
    if near_expiry_exit and days_to_expiry < expiry_alert_days:
        # In alert zone: any adverse movement is a signal
        if current_prob < entry_price - 0.01:
            return (
                StopLossAction.EXIT,
                f"Near expiry ({days_to_expiry:.1f}d) + price below entry → exit",
                updated_since,
            )

    return StopLossAction.HOLD, "All clear", updated_since


# ---------------------------------------------------------------------------
# Portfolio VaR Control
# ---------------------------------------------------------------------------

def check_portfolio_var(
    positions: list,
    capital: float,
    max_drawdown_pct: float = 0.05,
) -> tuple[bool, float]:
    """Check if portfolio VaR allows new positions.

    VaR = sum of worst-case losses across all positions.
    Worst-case for each position: size_usdc × (1 - hard_stop / entry_price).

    Returns (can_add_position, current_var_pct).
    """
    if not positions or capital <= 0:
        return True, 0.0

    total_var = 0.0
    for pos in positions:
        if pos.status != "open":
            continue
        # Worst case: price drops to hard stop
        worst_loss_pct = 1.0 - (pos.stop_loss_price / pos.entry_price)
        worst_loss_pct = max(0, worst_loss_pct)
        total_var += pos.size_usdc * worst_loss_pct

    var_pct = total_var / capital
    can_add = var_pct < max_drawdown_pct
    return can_add, round(var_pct, 4)
