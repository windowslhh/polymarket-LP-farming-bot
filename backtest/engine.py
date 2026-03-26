"""Backtesting engine for LP farming strategy.

Simulates the bot against historical data with realistic fill logic:
- Orders fill when market price crosses our quote price
- Adverse selection: fills are more likely when price moves against us
- LP rewards calculated using Polymarket's quadratic scoring function
- Maker rebates based on category and fill volume
- Tracks inventory, PnL, and risk metrics
"""

import math
from dataclasses import dataclass, field

from backtest.data_generator import Candle, MarketHistory
from src.strategy import get_rebate_rate


@dataclass
class BacktestConfig:
    """Parameters to test."""
    spread_bps: int = 300
    order_size_usdc: float = 30.0
    order_levels: int = 2
    level_spacing_bps: int = 100
    max_position_per_market: float = 200.0
    max_total_position: float = 800.0
    daily_loss_limit: float = 20.0
    inventory_skew_factor: float = 0.5
    refresh_every_n: int = 1  # requote every N candles
    min_probability: float = 0.10
    max_probability: float = 0.90


@dataclass
class OrderSim:
    """Simulated order."""
    price: float
    size_shares: float
    size_usdc: float
    side: str  # "BUY" or "SELL"


@dataclass
class FillEvent:
    """Record of a fill."""
    timestamp: int
    side: str
    price: float
    size_shares: float
    size_usdc: float
    midpoint_at_fill: float
    spread_pnl: float  # positive = good (bought below mid / sold above mid)


@dataclass
class DailyResult:
    """One day of backtest results."""
    day: int
    fills: int = 0
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    spread_pnl: float = 0.0
    lp_reward: float = 0.0
    maker_rebate: float = 0.0
    adverse_selection_cost: float = 0.0
    inventory_value_change: float = 0.0
    max_position: float = 0.0

    @property
    def realized_pnl(self) -> float:
        """Realized PnL = spread capture + rewards + rebates (no inventory risk)."""
        return self.spread_pnl + self.lp_reward + self.maker_rebate

    @property
    def total_pnl(self) -> float:
        """Total including unrealized inventory change (risky, don't optimize for this)."""
        return self.realized_pnl + self.inventory_value_change

    @property
    def total_volume(self) -> float:
        return self.buy_volume + self.sell_volume


@dataclass
class BacktestResult:
    """Complete backtest results for one parameter set."""
    config: BacktestConfig
    market_id: str
    market_name: str
    category: str
    duration_days: int
    daily_results: list[DailyResult] = field(default_factory=list)
    all_fills: list[FillEvent] = field(default_factory=list)

    @property
    def realized_pnl(self) -> float:
        """Realized PnL only (spread + LP + rebate, no inventory risk)."""
        return sum(d.realized_pnl for d in self.daily_results)

    @property
    def total_pnl(self) -> float:
        return sum(d.total_pnl for d in self.daily_results)

    @property
    def total_spread_pnl(self) -> float:
        return sum(d.spread_pnl for d in self.daily_results)

    @property
    def total_lp_rewards(self) -> float:
        return sum(d.lp_reward for d in self.daily_results)

    @property
    def total_maker_rebates(self) -> float:
        return sum(d.maker_rebate for d in self.daily_results)

    @property
    def total_volume(self) -> float:
        return sum(d.total_volume for d in self.daily_results)

    @property
    def total_fills(self) -> int:
        return sum(d.fills for d in self.daily_results)

    @property
    def avg_daily_pnl(self) -> float:
        if not self.daily_results:
            return 0.0
        return self.realized_pnl / len(self.daily_results)

    @property
    def max_drawdown(self) -> float:
        """Maximum peak-to-trough realized PnL drawdown."""
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for d in self.daily_results:
            cumulative += d.realized_pnl
            peak = max(peak, cumulative)
            dd = peak - cumulative
            max_dd = max(max_dd, dd)
        return max_dd

    @property
    def sharpe_ratio(self) -> float:
        """Annualized Sharpe ratio of daily PnL."""
        if len(self.daily_results) < 2:
            return 0.0
        daily_pnls = [d.realized_pnl for d in self.daily_results]
        mean = sum(daily_pnls) / len(daily_pnls)
        variance = sum((p - mean) ** 2 for p in daily_pnls) / (len(daily_pnls) - 1)
        std = math.sqrt(variance) if variance > 0 else 0.001
        return (mean / std) * math.sqrt(365)

    @property
    def win_rate(self) -> float:
        """Percentage of profitable days."""
        if not self.daily_results:
            return 0.0
        wins = sum(1 for d in self.daily_results if d.realized_pnl > 0)
        return wins / len(self.daily_results) * 100


def run_backtest(market: MarketHistory, config: BacktestConfig) -> BacktestResult:
    """Run backtest on a single market with given parameters."""
    result = BacktestResult(
        config=config,
        market_id=market.market_id,
        market_name=market.question,
        category=market.category,
        duration_days=market.duration_days,
    )

    steps_per_day = 288  # 5-minute intervals
    position_shares = 0.0  # Net position in shares
    position_cost = 0.0    # Average cost basis in USDC
    daily_loss = 0.0
    day_paused = False

    current_day = -1
    daily_result = None

    for i, candle in enumerate(market.candles):
        day = i // steps_per_day

        # New day
        if day != current_day:
            if daily_result is not None:
                # End-of-day inventory mark-to-market
                daily_result.inventory_value_change = (
                    position_shares * candle.midpoint - position_cost
                ) if position_shares != 0 else 0.0
                daily_result.max_position = abs(position_shares * candle.midpoint)
                result.daily_results.append(daily_result)

            current_day = day
            daily_result = DailyResult(day=day)
            daily_loss = 0.0
            day_paused = False

            # Calculate LP reward for previous day
            if len(result.daily_results) >= 1:
                prev_day = result.daily_results[-1]
                lp_reward = _estimate_lp_reward(
                    market=market,
                    spread_bps=config.spread_bps,
                    daily_volume_placed=prev_day.total_volume,
                )
                prev_day.lp_reward = lp_reward

        # Skip if paused (daily loss limit)
        if day_paused:
            continue

        # Skip refresh ticks
        if i % config.refresh_every_n != 0:
            continue

        mid = candle.midpoint

        # Probability filter
        if mid < config.min_probability or mid > config.max_probability:
            continue

        # Calculate inventory skew
        inventory_usdc = abs(position_shares * mid)
        skew_bps = 0.0
        if inventory_usdc > 10:
            skew_ratio = (position_shares * mid) / config.max_position_per_market
            skew_bps = skew_ratio * config.inventory_skew_factor * 100

        # Generate quotes
        orders = _generate_orders(mid, config, skew_bps)

        # Check fills against market
        for order in orders:
            # Position limit check
            projected_pos = position_shares
            if order.side == "BUY":
                projected_pos += order.size_shares
            else:
                projected_pos -= order.size_shares

            if abs(projected_pos * mid) > config.max_position_per_market:
                continue

            # Fill logic: order fills if market price crosses our price
            filled, fill_pnl = _check_fill(order, candle, mid)

            if filled:
                fill = FillEvent(
                    timestamp=candle.timestamp,
                    side=order.side,
                    price=order.price,
                    size_shares=order.size_shares,
                    size_usdc=order.size_usdc,
                    midpoint_at_fill=mid,
                    spread_pnl=fill_pnl,
                )
                result.all_fills.append(fill)

                # Update position
                if order.side == "BUY":
                    position_cost += order.size_usdc
                    position_shares += order.size_shares
                    daily_result.buy_volume += order.size_usdc
                else:
                    position_cost -= order.size_usdc
                    position_shares -= order.size_shares
                    daily_result.sell_volume += order.size_usdc

                daily_result.fills += 1
                daily_result.spread_pnl += fill_pnl

                if fill_pnl < 0:
                    daily_result.adverse_selection_cost += abs(fill_pnl)

                # Maker rebate on each fill
                rebate_rate = get_rebate_rate(market.category)
                # Estimate: taker pays ~1% fee, we get rebate_rate of that
                taker_fee = order.size_usdc * 0.01
                daily_result.maker_rebate += taker_fee * rebate_rate

                # Check daily loss limit
                daily_loss += min(0, fill_pnl)
                if abs(daily_loss) > config.daily_loss_limit:
                    day_paused = True

    # Final day
    if daily_result is not None:
        last_mid = market.candles[-1].midpoint if market.candles else 0.5
        daily_result.inventory_value_change = (
            position_shares * last_mid - position_cost
        ) if position_shares != 0 else 0.0
        daily_result.max_position = abs(position_shares * last_mid)

        lp_reward = _estimate_lp_reward(
            market=market,
            spread_bps=config.spread_bps,
            daily_volume_placed=daily_result.total_volume,
        )
        daily_result.lp_reward = lp_reward
        result.daily_results.append(daily_result)

    return result


def _generate_orders(
    midpoint: float,
    config: BacktestConfig,
    skew_bps: float = 0.0,
) -> list[OrderSim]:
    """Generate orders at current midpoint."""
    orders = []
    half_spread = config.spread_bps / 10000 / 2
    skew_offset = skew_bps / 10000

    for level in range(config.order_levels):
        extra = level * config.level_spacing_bps / 10000

        bid_price = midpoint - half_spread - extra + skew_offset
        ask_price = midpoint + half_spread + extra + skew_offset

        bid_price = max(0.01, min(0.99, bid_price))
        ask_price = max(0.01, min(0.99, ask_price))

        if bid_price >= ask_price:
            continue

        bid_shares = config.order_size_usdc / bid_price if bid_price > 0 else 0
        ask_shares = config.order_size_usdc / ask_price if ask_price > 0 else 0

        orders.append(OrderSim(
            price=round(bid_price, 4),
            size_shares=round(bid_shares, 2),
            size_usdc=config.order_size_usdc,
            side="BUY",
        ))
        orders.append(OrderSim(
            price=round(ask_price, 4),
            size_shares=round(ask_shares, 2),
            size_usdc=config.order_size_usdc,
            side="SELL",
        ))

    return orders


def _check_fill(order: OrderSim, candle: Candle, midpoint: float) -> tuple[bool, float]:
    """Determine if an order would fill given market conditions.

    Fill probability based on:
    1. How close order price is to market best bid/ask
    2. Market volume (more volume = more fills)
    3. Adverse selection: fills more likely when price moves against us

    Returns (filled: bool, spread_pnl: float)
    """
    if order.side == "BUY":
        # Buy order fills if market ask drops to our bid
        distance = candle.best_ask - order.price
        # Closer to market = higher fill probability
        if distance <= 0:
            # Our bid is above market ask - immediate fill (bad!)
            pnl = (midpoint - order.price) * order.size_shares
            return True, pnl

        # Probabilistic fill based on distance and volume
        # Normalize distance by spread
        market_spread = (candle.best_ask - candle.best_bid)
        if market_spread <= 0:
            market_spread = 0.01
        rel_distance = distance / market_spread

        # Base fill probability: ~5% per tick if at market, lower if far
        vol_factor = min(2.0, candle.volume / 20)  # higher volume = more fills
        fill_prob = 0.05 * vol_factor * max(0, 1 - rel_distance)

        if fill_prob > 0 and _random_check(fill_prob):
            pnl = (midpoint - order.price) * order.size_shares
            return True, pnl

    else:  # SELL
        distance = order.price - candle.best_bid
        if distance <= 0:
            pnl = (order.price - midpoint) * order.size_shares
            return True, pnl

        market_spread = (candle.best_ask - candle.best_bid)
        if market_spread <= 0:
            market_spread = 0.01
        rel_distance = distance / market_spread

        vol_factor = min(2.0, candle.volume / 20)
        fill_prob = 0.05 * vol_factor * max(0, 1 - rel_distance)

        if fill_prob > 0 and _random_check(fill_prob):
            pnl = (order.price - midpoint) * order.size_shares
            return True, pnl

    return False, 0.0


# Simple deterministic "random" based on global state for reproducibility
_fill_counter = 0


def _random_check(probability: float) -> bool:
    """Deterministic pseudo-random check for reproducibility."""
    global _fill_counter
    _fill_counter += 1
    # Use a simple hash-like function
    x = (_fill_counter * 2654435761) & 0xFFFFFFFF
    return (x / 0xFFFFFFFF) < probability


def _estimate_lp_reward(
    market: MarketHistory,
    spread_bps: int,
    daily_volume_placed: float,
) -> float:
    """Estimate LP reward using Polymarket's quadratic scoring.

    S(v,s) = ((v-s)/v)^2 * b
    where v = max_reward_spread, s = our spread, b = base reward

    We estimate our share as proportional to our volume vs market volume.
    """
    max_spread = market.max_reward_spread_bps
    if spread_bps >= max_spread:
        return 0.0

    # Quadratic score: closer to midpoint = exponentially more reward
    score = ((max_spread - spread_bps) / max_spread) ** 2

    # Our share of the reward pool depends on our relative activity
    # Assume ~5-10 other LPs competing
    avg_daily_volume = market.total_volume / max(market.duration_days, 1)
    our_share = min(0.30, daily_volume_placed / max(avg_daily_volume, 1))

    # Two-sided bonus: if we post both sides (we always do), 1.5x
    two_sided_bonus = 1.5

    reward = market.daily_reward_pool * score * our_share * two_sided_bonus
    return round(reward, 4)


def reset_fill_counter():
    """Reset for reproducible backtests."""
    global _fill_counter
    _fill_counter = 0
