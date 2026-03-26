"""Synthetic historical market data generator.

Generates realistic Polymarket-style price paths with:
- Mean-reverting random walks (prediction markets tend toward 0 or 1)
- Volume clustering (high vol during events, low vol otherwise)
- Orderbook depth simulation
- Multiple market types (stable, trending, volatile, event-driven)

Based on observed Polymarket market characteristics:
- Prices in [0.01, 0.99] range
- Tick size: 0.01
- Typical daily volume: $10K-$500K
- Spread: 1-5% depending on liquidity
"""

import math
import random
from dataclasses import dataclass, field

import numpy as np


@dataclass
class Candle:
    """Single time period data point."""
    timestamp: int        # seconds since start
    midpoint: float
    best_bid: float
    best_ask: float
    market_spread_bps: float
    volume: float         # USDC traded in this period
    num_trades: int
    bid_depth: float      # total USDC on bid side
    ask_depth: float


@dataclass
class MarketHistory:
    """Complete history for one market."""
    market_id: str
    question: str
    category: str
    initial_price: float
    final_price: float
    duration_days: int
    candles: list[Candle] = field(default_factory=list)
    total_volume: float = 0.0
    # LP reward config
    daily_reward_pool: float = 50.0  # USDC per day
    max_reward_spread_bps: int = 500


# Market archetypes observed on Polymarket
MARKET_TEMPLATES = [
    {
        "name": "S&P 500 above 6000 end of month",
        "category": "Finance",
        "archetype": "stable",
        "initial_price": 0.62,
        "volatility": 0.08,
        "duration_days": 30,
        "daily_volume": 80000,
        "base_spread_bps": 150,
        "reward_pool": 80,
    },
    {
        "name": "BTC above $90k on April 1",
        "category": "Crypto",
        "archetype": "volatile",
        "initial_price": 0.55,
        "volatility": 0.20,
        "duration_days": 14,
        "daily_volume": 150000,
        "base_spread_bps": 200,
        "reward_pool": 60,
    },
    {
        "name": "Fed rate cut in April",
        "category": "Finance",
        "archetype": "trending",
        "initial_price": 0.35,
        "volatility": 0.10,
        "duration_days": 45,
        "daily_volume": 40000,
        "base_spread_bps": 180,
        "reward_pool": 50,
    },
    {
        "name": "Democrats win midterms",
        "category": "Politics",
        "archetype": "stable",
        "initial_price": 0.48,
        "volatility": 0.05,
        "duration_days": 60,
        "daily_volume": 200000,
        "base_spread_bps": 100,
        "reward_pool": 120,
    },
    {
        "name": "ETH above $5k by July",
        "category": "Crypto",
        "archetype": "volatile",
        "initial_price": 0.25,
        "volatility": 0.25,
        "duration_days": 90,
        "daily_volume": 30000,
        "base_spread_bps": 250,
        "reward_pool": 40,
    },
    {
        "name": "AAPL earnings beat estimates",
        "category": "Finance",
        "archetype": "event_driven",
        "initial_price": 0.58,
        "volatility": 0.12,
        "duration_days": 21,
        "daily_volume": 60000,
        "base_spread_bps": 160,
        "reward_pool": 55,
    },
]


def generate_price_path(
    initial_price: float,
    num_steps: int,
    volatility: float,
    archetype: str = "stable",
    dt: float = 1 / 288,  # 5-minute intervals, 288 per day
) -> np.ndarray:
    """Generate realistic prediction market price path.

    Uses logit-normal model: prices are transformed to logit space,
    where they follow a random walk, then mapped back to (0,1).
    This naturally keeps prices in valid range and creates the
    characteristic "S-curve" behavior near 0 and 1.
    """
    # Work in logit space: logit(p) = log(p/(1-p))
    p = np.clip(initial_price, 0.02, 0.98)
    logit_p = math.log(p / (1 - p))

    prices = np.zeros(num_steps)
    prices[0] = p

    # Adjust volatility for archetype
    vol_scale = {
        "stable": 0.7,
        "volatile": 1.5,
        "trending": 1.0,
        "event_driven": 1.2,
    }.get(archetype, 1.0)
    sigma = volatility * vol_scale * math.sqrt(dt)

    # Add trend for trending archetype
    drift = 0
    if archetype == "trending":
        # Slight drift toward 0.5 (mean reversion) or away
        drift = random.choice([-0.3, 0.3]) * dt

    for i in range(1, num_steps):
        # Mean reversion in logit space (toward 0 = probability 0.5)
        mean_rev = -0.01 * logit_p * dt

        # Random component
        noise = np.random.normal(0, sigma)

        # Event shock: occasional large moves
        if archetype == "event_driven" and random.random() < 0.001:
            noise += np.random.normal(0, sigma * 5)
        elif archetype == "volatile" and random.random() < 0.002:
            noise += np.random.normal(0, sigma * 3)

        logit_p += drift + mean_rev + noise

        # Clamp logit to prevent extreme values
        logit_p = np.clip(logit_p, -4.5, 4.5)  # ~0.01 to 0.99

        # Convert back to probability
        prices[i] = 1 / (1 + math.exp(-logit_p))

    return prices


def generate_volume_profile(
    num_steps: int,
    daily_volume: float,
    archetype: str,
    steps_per_day: int = 288,
) -> np.ndarray:
    """Generate realistic volume profile.

    Volume follows:
    - Intraday pattern (higher at "open" and "close")
    - Random clustering (hot periods)
    - Lower volume on weekends
    """
    volumes = np.zeros(num_steps)
    base_vol_per_step = daily_volume / steps_per_day

    for i in range(num_steps):
        day = i // steps_per_day
        intraday_pos = (i % steps_per_day) / steps_per_day

        # Intraday U-shape: higher at start and end
        intraday_mult = 0.5 + 1.0 * (4 * (intraday_pos - 0.5) ** 2)

        # Weekend reduction (simplified: every 7th day)
        weekend_mult = 0.3 if (day % 7) >= 5 else 1.0

        # Random clustering
        cluster_mult = 1.0 + max(0, np.random.normal(0, 0.5))

        # Event-driven spikes
        if archetype == "event_driven" and random.random() < 0.005:
            cluster_mult *= 5.0

        vol = base_vol_per_step * intraday_mult * weekend_mult * cluster_mult
        volumes[i] = max(0, vol)

    return volumes


def generate_spread_profile(
    prices: np.ndarray,
    base_spread_bps: int,
    volumes: np.ndarray,
) -> np.ndarray:
    """Generate realistic market spread.

    Spread widens when:
    - Price near 0 or 1 (less LP interest)
    - Volume is low
    - Volatility increases
    """
    spreads = np.zeros(len(prices))
    median_vol = np.median(volumes[volumes > 0]) if np.any(volumes > 0) else 1

    for i in range(len(prices)):
        p = prices[i]

        # Probability distance from 0.5 increases spread
        prob_mult = 1.0 + 2.0 * abs(p - 0.5)

        # Low volume increases spread
        vol_mult = 1.0
        if volumes[i] > 0:
            vol_mult = max(0.5, min(3.0, median_vol / volumes[i]))

        spread = base_spread_bps * prob_mult * vol_mult

        # Add noise
        spread *= max(0.5, np.random.normal(1.0, 0.15))

        spreads[i] = max(50, min(1000, spread))  # 0.5% to 10%

    return spreads


def generate_market_history(template: dict, seed: int | None = None) -> MarketHistory:
    """Generate complete market history from a template."""
    if seed is not None:
        np.random.seed(seed)
        random.seed(seed)

    steps_per_day = 288  # 5-minute intervals
    num_steps = template["duration_days"] * steps_per_day

    # Generate price path
    prices = generate_price_path(
        initial_price=template["initial_price"],
        num_steps=num_steps,
        volatility=template["volatility"],
        archetype=template["archetype"],
    )

    # Generate volume
    volumes = generate_volume_profile(
        num_steps=num_steps,
        daily_volume=template["daily_volume"],
        archetype=template["archetype"],
    )

    # Generate spreads
    spreads = generate_spread_profile(
        prices=prices,
        base_spread_bps=template["base_spread_bps"],
        volumes=volumes,
    )

    # Build candles
    candles = []
    for i in range(num_steps):
        half_spread = spreads[i] / 10000 / 2
        mid = prices[i]
        bid = max(0.01, mid - half_spread)
        ask = min(0.99, mid + half_spread)

        # Estimate depth (correlated with volume)
        depth_mult = max(0.2, volumes[i] / (template["daily_volume"] / steps_per_day))
        bid_depth = 500 * depth_mult * random.uniform(0.5, 1.5)
        ask_depth = 500 * depth_mult * random.uniform(0.5, 1.5)

        candles.append(Candle(
            timestamp=i * 300,  # 5 minutes per step
            midpoint=round(mid, 4),
            best_bid=round(bid, 4),
            best_ask=round(ask, 4),
            market_spread_bps=round(spreads[i], 1),
            volume=round(volumes[i], 2),
            num_trades=max(0, int(volumes[i] / 50 * random.uniform(0.5, 1.5))),
            bid_depth=round(bid_depth, 2),
            ask_depth=round(ask_depth, 2),
        ))

    market_id = template["name"].lower().replace(" ", "_")[:20]
    return MarketHistory(
        market_id=market_id,
        question=template["name"],
        category=template["category"],
        initial_price=template["initial_price"],
        final_price=round(prices[-1], 4),
        duration_days=template["duration_days"],
        candles=candles,
        total_volume=round(float(np.sum(volumes)), 2),
        daily_reward_pool=template["reward_pool"],
        max_reward_spread_bps=500,
    )


def generate_all_markets(seed: int = 42) -> list[MarketHistory]:
    """Generate histories for all market templates."""
    markets = []
    for i, template in enumerate(MARKET_TEMPLATES):
        market = generate_market_history(template, seed=seed + i)
        markets.append(market)
    return markets
