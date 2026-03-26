"""Market selection and scoring for LP farming.

Selects markets that maximize LP reward potential while minimizing risk.
Prioritizes: high reward pools, low competition, favorable rebate tiers,
safe probability ranges, and sufficient time to expiry.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from loguru import logger

from src.strategy import get_rebate_rate


@dataclass
class ScoredMarket:
    """A market with its farming score."""
    token_id: str
    condition_id: str
    question: str
    outcome: str
    category: str
    midpoint: float
    daily_volume: float
    days_to_expiry: float
    score: float
    # For the complement side
    complement_token_id: str | None = None


def score_market(
    market: dict,
    min_probability: float = 0.10,
    max_probability: float = 0.90,
    min_days_to_expiry: float = 5,
    min_daily_volume: float = 1000,
    prefer_categories: list[str] | None = None,
) -> float:
    """Score a market for LP farming potential.

    Higher score = better market to provide liquidity in.

    Scoring factors:
    1. Volume score: higher volume = more fills = more activity recorded
    2. Rebate tier: Finance (50%) >> others (25%)
    3. Safety: probability near 50% is ideal for LP rewards but
       we penalize extremes that are risky
    4. Time to expiry: more time = safer
    5. Disqualifiers: extreme probability, too close to expiry, low volume
    """
    if prefer_categories is None:
        prefer_categories = ["Finance", "Politics", "Crypto", "Sports"]

    # Extract market data
    midpoint = _get_midpoint(market)
    days_to_expiry = _get_days_to_expiry(market)
    daily_volume = _get_daily_volume(market)
    category = market.get("category", "Other")

    # Hard filters - return 0 if market is unsuitable
    if midpoint < min_probability or midpoint > max_probability:
        return 0.0
    if days_to_expiry < min_days_to_expiry:
        return 0.0
    if daily_volume < min_daily_volume:
        return 0.0
    if not market.get("active", True):
        return 0.0

    # --- Scoring ---

    # 1. Volume score (log scale, capped)
    # $1K -> 1.0, $10K -> 2.0, $100K -> 3.0
    import math
    volume_score = math.log10(max(daily_volume, 1)) - 2  # normalize so $1K = 1
    volume_score = max(0.1, min(3.0, volume_score))

    # 2. Rebate tier multiplier
    rebate = get_rebate_rate(category)
    rebate_mult = 1.0 + rebate  # Finance: 1.5, others: 1.25, Geopolitics: 1.0

    # 3. Category preference
    category_mult = 1.0
    if category in prefer_categories:
        # Higher rank = higher multiplier
        rank = prefer_categories.index(category)
        category_mult = 1.0 + (len(prefer_categories) - rank) * 0.1

    # 4. Probability safety score
    # Best: 0.30-0.70 (high LP rewards, manageable risk)
    # OK: 0.15-0.85
    # Bad: <0.10 or >0.90 (filtered out above)
    prob_dist = abs(midpoint - 0.5)
    if prob_dist < 0.20:  # 0.30-0.70
        safety_score = 1.0
    elif prob_dist < 0.35:  # 0.15-0.85
        safety_score = 0.6
    else:
        safety_score = 0.3

    # 5. Expiry score (more days = better)
    if days_to_expiry > 30:
        expiry_score = 1.0
    elif days_to_expiry > 14:
        expiry_score = 0.8
    elif days_to_expiry > 7:
        expiry_score = 0.5
    else:
        expiry_score = 0.2

    score = volume_score * rebate_mult * category_mult * safety_score * expiry_score
    return round(score, 4)


def select_markets(
    markets: list[dict],
    max_markets: int = 5,
    config: dict | None = None,
) -> list[ScoredMarket]:
    """Select top markets for LP farming.

    Args:
        markets: Raw market data from Polymarket API.
        max_markets: Maximum number of markets to select.
        config: Market selection config dict.

    Returns:
        List of ScoredMarket, sorted by score descending.
    """
    if config is None:
        config = {}

    min_prob = config.get("min_probability", 0.10)
    max_prob = config.get("max_probability", 0.90)
    min_expiry = config.get("min_days_to_expiry", 5)
    min_vol = config.get("min_daily_volume", 1000)
    prefer_cats = config.get("prefer_categories", ["Finance", "Politics", "Crypto"])

    scored = []
    for market in markets:
        s = score_market(
            market,
            min_probability=min_prob,
            max_probability=max_prob,
            min_days_to_expiry=min_expiry,
            min_daily_volume=min_vol,
            prefer_categories=prefer_cats,
        )
        if s <= 0:
            continue

        tokens = market.get("tokens", [])
        if not tokens:
            continue

        # Pick the first token (YES side typically)
        token = tokens[0]
        complement = tokens[1] if len(tokens) > 1 else None

        scored.append(ScoredMarket(
            token_id=token.get("token_id", ""),
            condition_id=market.get("condition_id", ""),
            question=market.get("question", "Unknown"),
            outcome=token.get("outcome", "Yes"),
            category=market.get("category", "Other"),
            midpoint=_get_midpoint(market),
            daily_volume=_get_daily_volume(market),
            days_to_expiry=_get_days_to_expiry(market),
            score=s,
            complement_token_id=complement.get("token_id") if complement else None,
        ))

    # Sort by score descending
    scored.sort(key=lambda m: m.score, reverse=True)
    selected = scored[:max_markets]

    if selected:
        logger.info(f"Selected {len(selected)} markets for LP farming:")
        for m in selected:
            logger.info(
                f"  [{m.category}] {m.question[:50]}... "
                f"score={m.score:.2f} mid={m.midpoint:.2f} vol=${m.daily_volume:.0f}"
            )

    return selected


# -- Helpers to extract data from market dicts --

def _get_midpoint(market: dict) -> float:
    """Extract midpoint/probability from market data."""
    # Try various field names used by Polymarket API
    for key in ("midpoint", "last_trade_price", "best_bid"):
        if key in market:
            return float(market[key])

    tokens = market.get("tokens", [])
    if tokens and "price" in tokens[0]:
        return float(tokens[0]["price"])

    return 0.5  # default


def _get_days_to_expiry(market: dict) -> float:
    """Calculate days until market resolution."""
    end_date = market.get("end_date_iso") or market.get("end_date")
    if not end_date:
        return 365.0  # Unknown = assume far away

    try:
        if isinstance(end_date, str):
            # Handle ISO format
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
