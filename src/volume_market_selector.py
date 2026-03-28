"""Market selection for volume farming strategy.

Selects high-probability markets (>90%) for directional buying.
Key differences from LP market selection:
- Probability range: 0.90-0.99 (vs 0.15-0.85 for LP)
- No reward requirements (volume itself is the goal)
- Prefers shorter expiry (faster capital turnover)
- Filters by bid depth and spread (must be able to exit)
- Filters by annualized return after fees
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from loguru import logger

from src.strategy import estimate_fee_rate


@dataclass
class VolumeCandidate:
    """A market candidate for volume farming."""
    token_id: str
    complement_token_id: str | None
    condition_id: str
    question: str
    category: str
    side: str                   # "YES" or "NO" (which side to buy)
    probability: float          # Current probability of chosen side
    entry_price: float          # Expected entry price (ask for YES, or 1-bid for NO)
    days_to_expiry: float
    daily_volume: float
    bid_depth_usdc: float       # Available bid-side liquidity for exit
    spread_pct: float           # Bid-ask spread as fraction
    fee_rate: float             # Estimated taker fee rate
    net_profit_per_unit: float  # Expected profit per $1 after fees
    annualized_return: float    # Annualized ROI
    score: float = 0.0


def select_volume_markets(
    markets: list[dict],
    config: dict,
    orderbooks: dict[str, dict] | None = None,
) -> list[VolumeCandidate]:
    """Select top markets for volume farming.

    Pipeline:
    1. Hard filter by probability, expiry, volume, activity
    2. Check spread and bid depth from orderbook
    3. Filter by profitability (fees, annualized return)
    4. Score and rank
    """
    if orderbooks is None:
        orderbooks = {}

    min_prob = config.get("min_probability", 0.90)
    max_prob = config.get("max_probability", 0.99)
    min_expiry = config.get("min_days_to_expiry", 3)
    max_expiry = config.get("max_days_to_expiry", 30)
    min_volume = config.get("min_daily_volume", 1000)
    max_spread = config.get("max_spread_pct", 0.03)
    min_bid_depth = config.get("min_bid_depth_usdc", 200)
    min_annual_return = config.get("min_annualized_return", 0.15)
    max_markets = config.get("max_markets", 5)

    candidates = []

    for market in markets:
        if not market.get("active", True):
            continue

        tokens = market.get("tokens", [])
        if not tokens:
            continue

        # Determine which side to buy
        side, token_idx, prob = _pick_side(market, min_prob, max_prob)
        if side is None:
            continue

        token_id = tokens[token_idx].get("token_id", "")
        complement_idx = 1 - token_idx
        complement_id = tokens[complement_idx].get("token_id", "") if len(tokens) > 1 else None

        # Expiry filter
        days_to_expiry = _get_days_to_expiry(market)
        if days_to_expiry < min_expiry or days_to_expiry > max_expiry:
            continue

        # Volume filter
        daily_volume = _get_daily_volume(market)
        if daily_volume > 0 and daily_volume < min_volume:
            continue

        # Category (for fee calculation)
        category = _get_category(market)

        # Blacklist dangerous markets
        if _is_blacklisted(market):
            continue

        # Orderbook checks (spread + depth)
        ob = orderbooks.get(token_id)
        bid_depth, spread_pct, entry_price = _analyze_orderbook(ob, prob)

        if spread_pct > max_spread:
            continue
        if bid_depth < min_bid_depth:
            continue

        # Fee calculation
        fee_rate = estimate_fee_rate(prob, category)

        # Profitability check
        # Profit if resolves correctly: (1.0 - entry_price) per share
        # Cost: entry fee + potential exit fee (2x for round trip)
        gross_profit = (1.0 - entry_price) * prob
        total_fees = 2 * fee_rate * entry_price
        net_profit = gross_profit - total_fees

        if net_profit <= 0:
            continue

        # Annualized return
        if days_to_expiry > 0:
            annualized = (net_profit / entry_price) * (365.0 / days_to_expiry)
        else:
            annualized = 0.0

        if annualized < min_annual_return:
            continue

        candidates.append(VolumeCandidate(
            token_id=token_id,
            complement_token_id=complement_id,
            condition_id=market.get("condition_id", ""),
            question=market.get("question", "Unknown"),
            category=category,
            side=side,
            probability=prob,
            entry_price=entry_price,
            days_to_expiry=days_to_expiry,
            daily_volume=daily_volume,
            bid_depth_usdc=bid_depth,
            spread_pct=spread_pct,
            fee_rate=fee_rate,
            net_profit_per_unit=net_profit,
            annualized_return=annualized,
        ))

    # Score and sort
    for c in candidates:
        c.score = _score_candidate(c)

    candidates.sort(key=lambda c: c.score, reverse=True)
    selected = candidates[:max_markets]

    # Log results
    logger.info(f"Volume farming: {len(candidates)} candidates passed filters, selected {len(selected)}")
    for c in selected:
        logger.info(
            f"  [{c.category}] {c.question[:45]}... "
            f"side={c.side} prob={c.probability:.3f} entry={c.entry_price:.3f} "
            f"net={c.net_profit_per_unit:.4f} annual={c.annualized_return:.1%} "
            f"expiry={c.days_to_expiry:.0f}d score={c.score:.3f}"
        )

    return selected


def _pick_side(
    market: dict,
    min_prob: float,
    max_prob: float,
) -> tuple[str | None, int, float]:
    """Determine which side (YES/NO) to buy.

    Returns (side, token_index, probability) or (None, 0, 0) if neither qualifies.
    Prefers the side with higher probability (more certain outcome).
    """
    tokens = market.get("tokens", [])
    if not tokens:
        return None, 0, 0.0

    # Get YES price
    yes_price = _get_token_price(tokens[0])
    no_price = _get_token_price(tokens[1]) if len(tokens) > 1 else (1.0 - yes_price)

    # Check YES side
    if min_prob <= yes_price <= max_prob:
        return "YES", 0, yes_price

    # Check NO side
    if min_prob <= no_price <= max_prob:
        return "NO", 1, no_price

    return None, 0, 0.0


def _analyze_orderbook(
    orderbook: dict | None,
    probability: float,
) -> tuple[float, float, float]:
    """Analyze orderbook for exit feasibility.

    Returns (bid_depth_usdc, spread_pct, estimated_entry_price).
    """
    if not orderbook:
        # No orderbook data: use probability as entry, assume medium depth
        return 100.0, 0.02, probability

    bids = orderbook.get("bids", []) or []
    asks = orderbook.get("asks", []) or []

    # Bid depth (how much we can sell for exit)
    bid_depth = sum(
        float(b.get("price", 0)) * float(b.get("size", 0))
        for b in bids
    )

    # Spread
    best_bid = float(bids[0].get("price", 0)) if bids else probability - 0.01
    best_ask = float(asks[0].get("price", 0)) if asks else probability + 0.01

    if best_bid > 0:
        spread_pct = (best_ask - best_bid) / best_bid
    else:
        spread_pct = 0.05  # Default high spread

    # Entry price: we'll buy at the ask
    entry_price = best_ask if asks else probability

    return bid_depth, max(0, spread_pct), entry_price


def _score_candidate(c: VolumeCandidate) -> float:
    """Score a volume farming candidate.

    score = profit × liquidity × time_efficiency × safety

    Sweet spot: 0.92-0.97 probability, 5-15 days to expiry.
    """
    # Profit score (net profit after fees, log-scaled)
    profit_score = math.log(max(c.net_profit_per_unit * 1000, 1)) + 1

    # Liquidity score (volume + depth)
    if c.daily_volume >= 10000:
        liquidity_score = 1.0
    elif c.daily_volume >= 5000:
        liquidity_score = 0.8
    elif c.daily_volume >= 1000:
        liquidity_score = 0.6
    else:
        liquidity_score = 0.3

    # Depth bonus
    if c.bid_depth_usdc >= 1000:
        liquidity_score *= 1.2
    elif c.bid_depth_usdc >= 500:
        liquidity_score *= 1.1

    # Time efficiency (faster resolution = better capital turnover)
    if c.days_to_expiry <= 7:
        time_score = 1.0
    elif c.days_to_expiry <= 14:
        time_score = 0.85
    elif c.days_to_expiry <= 21:
        time_score = 0.70
    else:
        time_score = 0.55

    # Safety score (probability sweet spot 0.92-0.97)
    if 0.92 <= c.probability <= 0.97:
        safety_score = 1.0
    elif 0.90 <= c.probability < 0.92:
        safety_score = 0.85
    elif 0.97 < c.probability <= 0.99:
        safety_score = 0.75  # Very high prob but thin margin
    else:
        safety_score = 0.5

    # Spread penalty (lower spread = better)
    spread_penalty = max(0.5, 1.0 - c.spread_pct * 10)

    return profit_score * liquidity_score * time_score * safety_score * spread_penalty


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_token_price(token: dict) -> float:
    """Extract price from token data."""
    return float(token.get("price", 0.5))


def _get_days_to_expiry(market: dict) -> float:
    """Calculate days until market resolution."""
    end_date = market.get("end_date_iso") or market.get("end_date")
    if not end_date:
        return 365.0

    try:
        if isinstance(end_date, str):
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        else:
            end_dt = datetime.fromtimestamp(end_date, tz=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = (end_dt - now).total_seconds() / 86400
        return max(0, delta)
    except (ValueError, TypeError):
        return 365.0


def _get_daily_volume(market: dict) -> float:
    """Extract daily trading volume."""
    for key in ("volume_24h", "volume24hr", "daily_volume", "volume"):
        if key in market:
            try:
                return float(market[key])
            except (ValueError, TypeError):
                pass
    return 0.0


def _get_category(market: dict) -> str:
    """Extract category from market data."""
    tags = market.get("tags", [])
    if tags and isinstance(tags, list):
        for tag in tags:
            if tag and tag != "All":
                return tag
    return market.get("category", "Other")


# Reuse blacklist from LP selector
_BLACKLIST_KEYWORDS = [
    "up or down", "above or below", "5-minute", "15-minute",
]

_BLACKLIST_CATEGORIES = {"Crypto Up/Down"}


def _is_blacklisted(market: dict) -> bool:
    """Check if market should be avoided."""
    category = _get_category(market)
    if category in _BLACKLIST_CATEGORIES:
        return True
    question = market.get("question", "").lower()
    return any(kw in question for kw in _BLACKLIST_KEYWORDS)
