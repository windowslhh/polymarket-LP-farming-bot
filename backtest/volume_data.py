"""Synthetic data for high-probability markets (90%+ or <10%).

Generates price paths for volume farming backtesting:
- Stable high-prob: stays near 95%, resolves YES (happy path)
- Gradual drift down: starts 95%, slowly drifts to 85% (stop-loss test)
- Sudden reversal: starts 95%, sudden drop to 60% (emergency exit test)
- Near-expiry convergence: starts 93%, converges to 99% as expiry nears
- False alarm: dips to 88% then recovers to 96% (whipsaw test)
- Slow bleed: gradually loses 1%/day (drift velocity test)
"""

import math
import random
from dataclasses import dataclass

import numpy as np

from backtest.data_generator import Candle


@dataclass
class HighProbMarket:
    """A high-probability market for volume farming backtest."""
    market_id: str
    question: str
    category: str
    initial_price: float         # Starting probability (0.90-0.99)
    resolution: str              # "YES" or "NO" (how it actually resolves)
    duration_days: int
    candles: list[Candle]
    daily_volume: float
    taker_fee_rate: float        # Effective taker fee at this probability


# Market scenarios for backtesting different stop-loss behaviors
VOLUME_MARKET_SCENARIOS = [
    # === Happy path: high prob stays high, resolves YES ===
    {
        "name": "Norway wins Eurovision 2026",
        "category": "Culture",
        "initial_price": 0.95,
        "resolution": "YES",
        "archetype": "stable_high",
        "duration_days": 14,
        "daily_volume": 5000,
        "volatility": 0.03,
    },
    {
        "name": "GDP growth positive Q1 2026",
        "category": "Economics",
        "initial_price": 0.92,
        "resolution": "YES",
        "archetype": "stable_high",
        "duration_days": 21,
        "daily_volume": 8000,
        "volatility": 0.04,
    },
    {
        "name": "Lakers make NBA playoffs",
        "category": "Sports",
        "initial_price": 0.97,
        "resolution": "YES",
        "archetype": "stable_high",
        "duration_days": 10,
        "daily_volume": 15000,
        "volatility": 0.02,
    },
    # === Gradual drift down: stop-loss should trigger ===
    {
        "name": "SpaceX Starship orbital by April",
        "category": "Tech",
        "initial_price": 0.93,
        "resolution": "NO",
        "archetype": "gradual_down",
        "duration_days": 30,
        "daily_volume": 3000,
        "volatility": 0.06,
    },
    {
        "name": "Fed rate cut before May",
        "category": "Finance",
        "initial_price": 0.91,
        "resolution": "NO",
        "archetype": "gradual_down",
        "duration_days": 25,
        "daily_volume": 20000,
        "volatility": 0.05,
    },
    # === Sudden reversal: emergency exit test ===
    {
        "name": "Team X wins championship",
        "category": "Sports",
        "initial_price": 0.94,
        "resolution": "NO",
        "archetype": "sudden_reversal",
        "duration_days": 14,
        "daily_volume": 10000,
        "volatility": 0.08,
    },
    # === Near-expiry convergence to YES ===
    {
        "name": "S&P 500 ends above 5500",
        "category": "Finance",
        "initial_price": 0.93,
        "resolution": "YES",
        "archetype": "convergence",
        "duration_days": 7,
        "daily_volume": 50000,
        "volatility": 0.03,
    },
    # === False alarm / whipsaw: dips then recovers ===
    {
        "name": "Bitcoin above $80k end of month",
        "category": "Crypto",
        "initial_price": 0.95,
        "resolution": "YES",
        "archetype": "whipsaw",
        "duration_days": 14,
        "daily_volume": 30000,
        "volatility": 0.07,
    },
    # === Slow bleed: 1%/day drift down ===
    {
        "name": "UK election result prediction",
        "category": "Politics",
        "initial_price": 0.96,
        "resolution": "NO",
        "archetype": "slow_bleed",
        "duration_days": 20,
        "daily_volume": 6000,
        "volatility": 0.04,
    },
    # === Very high prob, resolves YES (best case) ===
    {
        "name": "Sun rises tomorrow",
        "category": "Science",
        "initial_price": 0.98,
        "resolution": "YES",
        "archetype": "stable_high",
        "duration_days": 3,
        "daily_volume": 2000,
        "volatility": 0.01,
    },
]


def generate_high_prob_path(
    initial_price: float,
    num_steps: int,
    volatility: float,
    archetype: str,
    resolution: str,
    duration_days: int,
) -> np.ndarray:
    """Generate price path for a high-probability market.

    Different archetypes produce different characteristic patterns.
    """
    prices = np.zeros(num_steps)
    prices[0] = initial_price
    steps_per_day = num_steps // max(duration_days, 1)
    dt = 1.0 / steps_per_day

    if archetype == "stable_high":
        # Stays near initial price with small random walk, converges to 1.0
        for i in range(1, num_steps):
            progress = i / num_steps  # 0 to 1
            # Mean revert toward initial_price + drift toward resolution
            target = initial_price + (0.99 - initial_price) * progress * 0.5
            mean_rev = 0.1 * (target - prices[i - 1]) * dt
            noise = np.random.normal(0, volatility * math.sqrt(dt))
            prices[i] = np.clip(prices[i - 1] + mean_rev + noise, 0.05, 0.99)

    elif archetype == "gradual_down":
        # Drifts down ~0.3-0.5%/day
        daily_drift = -0.004
        for i in range(1, num_steps):
            drift = daily_drift * dt
            noise = np.random.normal(0, volatility * math.sqrt(dt))
            prices[i] = np.clip(prices[i - 1] + drift + noise, 0.05, 0.99)

    elif archetype == "sudden_reversal":
        # Stable for 60% of duration, then sudden drop
        crash_step = int(num_steps * 0.6)
        crash_magnitude = 0.25  # Drop by 25 probability points
        for i in range(1, num_steps):
            if i == crash_step:
                # Sudden crash over ~2 hours (24 steps at 5-min)
                prices[i] = prices[i - 1] - crash_magnitude * 0.3
            elif crash_step < i < crash_step + 24:
                # Continue dropping
                prices[i] = prices[i - 1] - crash_magnitude * 0.03
            else:
                noise = np.random.normal(0, volatility * 0.5 * math.sqrt(dt))
                if i < crash_step:
                    # Stable before crash
                    mean_rev = 0.05 * (initial_price - prices[i - 1]) * dt
                else:
                    # Bounces around low level after crash
                    low_target = initial_price - crash_magnitude * 0.8
                    mean_rev = 0.05 * (low_target - prices[i - 1]) * dt
                prices[i] = prices[i - 1] + mean_rev + noise
            prices[i] = np.clip(prices[i], 0.05, 0.99)

    elif archetype == "convergence":
        # Converges toward 0.99 as expiry approaches
        for i in range(1, num_steps):
            progress = i / num_steps
            target = initial_price + (0.99 - initial_price) * progress ** 0.5
            mean_rev = 0.3 * (target - prices[i - 1]) * dt
            noise = np.random.normal(0, volatility * (1 - progress * 0.7) * math.sqrt(dt))
            prices[i] = np.clip(prices[i - 1] + mean_rev + noise, 0.05, 0.99)

    elif archetype == "whipsaw":
        # Dips 7-10% around 30-40% progress, then recovers
        dip_center = 0.35
        dip_width = 0.15
        dip_depth = 0.08
        for i in range(1, num_steps):
            progress = i / num_steps
            # Dip function: Gaussian-shaped temporary drop
            dip = dip_depth * math.exp(-((progress - dip_center) ** 2) / (2 * dip_width ** 2))
            target = initial_price - dip + (0.99 - initial_price) * progress * 0.3
            mean_rev = 0.15 * (target - prices[i - 1]) * dt
            noise = np.random.normal(0, volatility * math.sqrt(dt))
            prices[i] = np.clip(prices[i - 1] + mean_rev + noise, 0.05, 0.99)

    elif archetype == "slow_bleed":
        # Loses ~0.8-1%/day consistently
        daily_drift = -0.008
        for i in range(1, num_steps):
            drift = daily_drift * dt
            noise = np.random.normal(0, volatility * 0.6 * math.sqrt(dt))
            prices[i] = np.clip(prices[i - 1] + drift + noise, 0.05, 0.99)

    else:
        # Default: random walk with slight upward drift
        for i in range(1, num_steps):
            noise = np.random.normal(0, volatility * math.sqrt(dt))
            prices[i] = np.clip(prices[i - 1] + noise, 0.05, 0.99)

    return prices


def generate_volume_market(
    scenario: dict,
    seed: int | None = None,
) -> HighProbMarket:
    """Generate a complete high-probability market from a scenario."""
    if seed is not None:
        np.random.seed(seed)
        random.seed(seed)

    steps_per_day = 288  # 5-min intervals
    num_steps = scenario["duration_days"] * steps_per_day

    prices = generate_high_prob_path(
        initial_price=scenario["initial_price"],
        num_steps=num_steps,
        volatility=scenario["volatility"],
        archetype=scenario["archetype"],
        resolution=scenario["resolution"],
        duration_days=scenario["duration_days"],
    )

    # Estimate fee at this probability level
    # fee = max_fee * 4 * p * (1-p), using Finance max_fee=1%
    p = scenario["initial_price"]
    fee_rate = 0.01 * 4 * p * (1 - p)

    # Build candles
    candles = []
    base_vol_per_step = scenario["daily_volume"] / steps_per_day
    for i in range(num_steps):
        mid = prices[i]
        # Spread: tighter at high probability
        spread_pct = 0.01 + 0.02 * (1 - mid)  # 1-3%
        half_spread = mid * spread_pct / 2
        bid = max(0.01, round(mid - half_spread, 4))
        ask = min(0.99, round(mid + half_spread, 4))

        # Volume variation
        vol = base_vol_per_step * max(0.2, np.random.lognormal(0, 0.5))

        # Depth: correlated with volume
        depth_base = scenario["daily_volume"] / 10
        bid_depth = depth_base * max(0.3, np.random.lognormal(0, 0.3))
        ask_depth = depth_base * max(0.3, np.random.lognormal(0, 0.3))

        candles.append(Candle(
            timestamp=i * 300,
            midpoint=round(mid, 4),
            best_bid=bid,
            best_ask=ask,
            market_spread_bps=round(spread_pct * 10000, 1),
            volume=round(vol, 2),
            num_trades=max(0, int(vol / 100)),
            bid_depth=round(bid_depth, 2),
            ask_depth=round(ask_depth, 2),
        ))

    market_id = scenario["name"].lower().replace(" ", "_")[:20]
    return HighProbMarket(
        market_id=market_id,
        question=scenario["name"],
        category=scenario["category"],
        initial_price=scenario["initial_price"],
        resolution=scenario["resolution"],
        duration_days=scenario["duration_days"],
        candles=candles,
        daily_volume=scenario["daily_volume"],
        taker_fee_rate=round(fee_rate, 5),
    )


def generate_all_volume_markets(seed: int = 42) -> list[HighProbMarket]:
    """Generate all volume farming test markets."""
    markets = []
    for i, scenario in enumerate(VOLUME_MARKET_SCENARIOS):
        market = generate_volume_market(scenario, seed=seed + i * 7)
        markets.append(market)
    return markets
