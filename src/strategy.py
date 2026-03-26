"""Conservative market making strategy for airdrop farming.

Goal: Stay active as LP with minimal losses, not maximize trading profit.
- Wide spreads to reduce adverse selection
- Small order sizes to limit exposure
- Always two-sided (bid + ask) for reward bonus
- Fee-aware spread calculation
"""

from dataclasses import dataclass

from loguru import logger


# Polymarket fee rates at 50% probability (peak) by category
# Fees scale down as probability approaches 0% or 100%
CATEGORY_MAX_FEE_RATE = {
    "Crypto": 0.018,       # 1.8%
    "Finance": 0.010,      # 1.0%
    "Politics": 0.010,     # 1.0%
    "Tech": 0.010,         # 1.0%
    "Sports": 0.0044,      # 0.44%
    "Economics": 0.0125,   # 1.25%
    "Culture": 0.015,      # 1.5%
    "Weather": 0.0125,     # 1.25%
    "Geopolitics": 0.0,    # No fees
}

# Maker rebate rates (% of taker fees redistributed to makers)
CATEGORY_REBATE_RATE = {
    "Finance": 0.50,       # 50% rebate - best for LPs
    "default": 0.25,       # 25% for all others
    "Geopolitics": 0.0,    # No fees = no rebates
}


@dataclass
class Quote:
    """A single order quote."""
    price: float
    size: float
    side: str  # "BUY" or "SELL"


@dataclass
class QuotePair:
    """Bid and ask quotes for a market."""
    bids: list[Quote]
    asks: list[Quote]


def estimate_fee_rate(probability: float, category: str = "default") -> float:
    """Estimate taker fee rate based on current probability and category.

    Fees are highest at 50% and taper to 0 at extremes.
    Approximated as: fee = max_fee * 4 * p * (1 - p)
    where p is the probability (peaks at p=0.5).
    """
    max_fee = CATEGORY_MAX_FEE_RATE.get(category, 0.010)
    # Quadratic scaling: peaks at 0.5, zero at 0 and 1
    return max_fee * 4 * probability * (1 - probability)


def get_rebate_rate(category: str) -> float:
    """Get maker rebate rate for a category."""
    return CATEGORY_REBATE_RATE.get(category, CATEGORY_REBATE_RATE["default"])


def calculate_quotes(
    midpoint: float,
    spread_bps: int = 300,
    order_size: float = 30.0,
    order_levels: int = 2,
    level_spacing_bps: int = 100,
    inventory_skew: float = 0.0,
) -> QuotePair:
    """Calculate conservative two-sided quotes.

    Args:
        midpoint: Current mid-price (0-1 range).
        spread_bps: Base spread in basis points.
        order_size: USDC size per order layer.
        order_levels: Number of layers on each side.
        level_spacing_bps: Additional spread per level.
        inventory_skew: Shift quotes to reduce inventory.
            Positive = long (shift asks tighter, bids wider).
            Negative = short (shift bids tighter, asks wider).

    Returns:
        QuotePair with bid and ask orders.
    """
    half_spread = spread_bps / 10000 / 2
    skew_offset = inventory_skew / 10000

    bids = []
    asks = []

    for level in range(order_levels):
        extra_spread = level * level_spacing_bps / 10000

        bid_price = midpoint - half_spread - extra_spread + skew_offset
        ask_price = midpoint + half_spread + extra_spread + skew_offset

        # Clamp to valid price range [0.01, 0.99]
        bid_price = _clamp_price(bid_price)
        ask_price = _clamp_price(ask_price)

        # Skip if bid >= ask (shouldn't happen with reasonable params)
        if bid_price >= ask_price:
            logger.warning(f"Skipping level {level}: bid {bid_price} >= ask {ask_price}")
            continue

        # Convert USDC size to share size at this price
        bid_shares = order_size / bid_price if bid_price > 0 else 0
        ask_shares = order_size / ask_price if ask_price > 0 else 0

        bids.append(Quote(price=round(bid_price, 4), size=round(bid_shares, 2), side="BUY"))
        asks.append(Quote(price=round(ask_price, 4), size=round(ask_shares, 2), side="SELL"))

    return QuotePair(bids=bids, asks=asks)


def should_requote(current_midpoint: float, last_midpoint: float,
                   threshold_bps: int = 50) -> bool:
    """Check if midpoint moved enough to warrant requoting."""
    if last_midpoint == 0:
        return True
    change = abs(current_midpoint - last_midpoint) / last_midpoint
    return change > threshold_bps / 10000


def check_reward_compliance(
    quotes: QuotePair,
    max_spread: float = 0.04,
    min_shares: float = 50.0,
) -> tuple[bool, str]:
    """Check if quotes comply with reward program requirements.

    Returns (is_compliant, reason).
    """
    if not quotes.bids or not quotes.asks:
        return False, "Missing bid or ask side"

    # Check spread: best bid to best ask must be within max_spread
    best_bid = max(q.price for q in quotes.bids)
    best_ask = min(q.price for q in quotes.asks)
    spread = best_ask - best_bid

    if spread > max_spread:
        return False, f"Spread {spread:.4f} exceeds max {max_spread:.4f}"

    # Check min shares
    for q in quotes.bids + quotes.asks:
        if q.size < min_shares:
            return False, f"Order size {q.size:.1f} below min {min_shares:.0f} shares"

    return True, "OK"


def _clamp_price(price: float) -> float:
    """Clamp price to valid Polymarket range."""
    return max(0.01, min(0.99, price))
