"""Market selection and scoring for LP farming.

Core logic: find 「高奖励 + 低波动 + 低竞争」markets, NOT chase high-volume mainstream markets.

Selection pipeline:
1. Hard filters: rewards eligibility, probability range, expiry, activity
2. Reward compliance: max spread, min shares requirements
3. Volatility filter: historical std dev < threshold
4. Competition assessment: order depth analysis
5. Scoring: reward_potential / (volatility × competition)
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from loguru import logger

from src.strategy import get_rebate_rate


@dataclass
class RewardInfo:
    """Reward program parameters for a market."""
    total_rewards: float = 0.0       # Daily reward pool size
    max_spread: float = 0.04         # Max spread to qualify (default 4¢)
    min_shares: float = 50.0         # Min shares per order to qualify
    has_rewards: bool = False


@dataclass
class CompetitionInfo:
    """Competition assessment for a market."""
    bid_depth: float = 0.0           # Total bid liquidity (USDC)
    ask_depth: float = 0.0           # Total ask liquidity (USDC)
    num_bid_levels: int = 0
    num_ask_levels: int = 0
    competition_score: float = 1.0   # 0-1, lower = less competition = better


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
    # Reward program info
    reward_info: RewardInfo = field(default_factory=RewardInfo)
    competition_info: CompetitionInfo = field(default_factory=CompetitionInfo)
    volatility: float = 0.0          # Historical std dev (¢ per day)
    # For the complement side
    complement_token_id: str | None = None


# --- Category configuration ---

# Categories to AVOID (high competition / high volatility red ocean)
BLACKLIST_CATEGORIES = {
    "Crypto Up/Down",    # Bot arms race, worst red ocean
}

# Keywords in market question that signal dangerous markets
BLACKLIST_KEYWORDS = [
    "up or down",        # Crypto Up/Down series
    "above or below",    # Price prediction series
    "5-minute",          # Ultra short-term
    "15-minute",         # Short-term
]

# Category priority for niche/safe markets (higher index = lower priority)
CATEGORY_PRIORITY = {
    # Tier 1: Niche, low competition, predictable
    "Sports": 1.0,          # Niche sports (NCAAB, Serie A, etc.)
    "Science": 1.0,         # Long cycle, almost no sudden news
    "Weather": 0.95,        # Very predictable, low competition
    "Culture": 0.90,        # Pop culture, slow-moving
    # Tier 2: Medium competition, good rewards
    "Finance": 0.80,        # High rebate (50%) but more competitive
    "Economics": 0.75,      # Decent rewards, medium competition
    "Tech": 0.70,           # SpaceX etc., long cycle when calm
    # Tier 3: Higher risk
    "Politics": 0.50,       # News catalysts, volatile around events
    # Tier 4: Avoid or very careful
    "Crypto": 0.30,         # High competition, high volatility
    "Geopolitics": 0.20,    # No rebates, unpredictable
}


def assess_competition(orderbook: dict | None) -> CompetitionInfo:
    """Assess competition level from orderbook data.

    Low competition = shallow order book = better for small capital.
    """
    if not orderbook:
        return CompetitionInfo(competition_score=0.5)  # Unknown, assume medium

    bids = orderbook.get("bids", [])
    asks = orderbook.get("asks", [])

    bid_depth = sum(float(b.get("size", 0)) * float(b.get("price", 0)) for b in bids)
    ask_depth = sum(float(a.get("size", 0)) * float(a.get("price", 0)) for a in asks)
    total_depth = bid_depth + ask_depth

    num_bid_levels = len(bids)
    num_ask_levels = len(asks)

    # Competition score: 0 = no competition, 1 = very competitive
    # Thresholds based on typical Polymarket markets:
    # < $500 total depth = very low competition
    # $500-$2000 = low
    # $2000-$10000 = medium
    # > $10000 = high competition
    if total_depth < 500:
        comp_score = 0.1
    elif total_depth < 2000:
        comp_score = 0.3
    elif total_depth < 5000:
        comp_score = 0.5
    elif total_depth < 10000:
        comp_score = 0.7
    else:
        comp_score = 0.9

    # More price levels = more bots competing
    total_levels = num_bid_levels + num_ask_levels
    if total_levels > 20:
        comp_score = min(1.0, comp_score + 0.15)

    return CompetitionInfo(
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        num_bid_levels=num_bid_levels,
        num_ask_levels=num_ask_levels,
        competition_score=comp_score,
    )


def estimate_volatility(price_history: list[float] | None) -> float:
    """Estimate daily price volatility from historical prices.

    Returns std dev in cents (¢) per day.
    Low volatility (< 5¢/day) is ideal for script-based market making.
    """
    if not price_history or len(price_history) < 10:
        return 5.0  # Unknown, assume medium

    # Calculate returns (differences)
    returns = [price_history[i] - price_history[i - 1] for i in range(1, len(price_history))]

    # Standard deviation of returns
    mean_ret = sum(returns) / len(returns)
    variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
    std_dev = variance ** 0.5

    # Convert to cents (prices are 0-1, so multiply by 100)
    return std_dev * 100


def parse_reward_info(market: dict) -> RewardInfo:
    """Extract reward program info from market data.

    Real Polymarket API format:
    {
      "rewards": {
        "rates": [{"asset_address": "...", "rewards_daily_rate": 1}],
        "min_size": 20,
        "max_spread": 3.5   # In CENTS (3.5 = 3.5¢ = 0.035 in decimal)
      }
    }
    """
    rewards = market.get("rewards", {})
    if not rewards or not isinstance(rewards, dict):
        return RewardInfo(has_rewards=False)

    # Extract daily reward rate from rates array
    total_rewards = 0.0
    rates = rewards.get("rates", [])
    if isinstance(rates, list) and rates:
        for rate_entry in rates:
            if isinstance(rate_entry, dict):
                total_rewards += float(rate_entry.get("rewards_daily_rate", 0))

    # Fallback: try direct total_rewards field (mock data compat)
    if total_rewards == 0:
        total_rewards = float(rewards.get("total_rewards", 0))

    if total_rewards <= 0:
        return RewardInfo(has_rewards=False)

    # max_spread: API returns in CENTS (e.g., 3.5 = 3.5¢)
    # Convert to decimal for internal use (3.5 -> 0.035)
    raw_max_spread = float(rewards.get("max_spread", 4.0))
    if raw_max_spread > 1:
        # API format: cents (3.5 = 3.5¢)
        max_spread = raw_max_spread / 100.0
    else:
        # Already in decimal (mock data compat: 0.04)
        max_spread = raw_max_spread

    min_shares = float(rewards.get("min_size", 20))

    return RewardInfo(
        total_rewards=total_rewards,
        max_spread=max_spread,
        min_shares=min_shares,
        has_rewards=True,
    )


def _get_category(market: dict) -> str:
    """Extract category from market data.

    Real API uses 'tags' array (first tag is primary category).
    Mock data uses 'category' field directly.
    """
    # Real API: tags array
    tags = market.get("tags", [])
    if tags and isinstance(tags, list):
        # First tag is usually the primary category
        # Skip generic tags like "All"
        for tag in tags:
            if tag and tag != "All":
                return tag

    # Fallback: direct category field (mock data compat)
    return market.get("category", "Other")


def _is_blacklisted(market: dict) -> bool:
    """Check if market should be avoided entirely."""
    category = _get_category(market)
    question = market.get("question", "").lower()

    # Category blacklist
    if category in BLACKLIST_CATEGORIES:
        return True

    # Keyword blacklist
    for keyword in BLACKLIST_KEYWORDS:
        if keyword in question:
            return True

    return False


def score_market(
    market: dict,
    min_probability: float = 0.15,
    max_probability: float = 0.85,
    min_days_to_expiry: float = 7,
    min_daily_volume: float = 200,
    max_daily_volume: float = 500000,
    require_rewards: bool = True,
    min_reward_pool: float = 1.0,
    max_reward_pool: float = 100.0,
    max_volatility: float = 8.0,
    orderbook: dict | None = None,
    price_history: list[float] | None = None,
) -> tuple[float, RewardInfo, CompetitionInfo, float]:
    """Score a market for LP farming potential.

    Core philosophy: 高奖励 + 低波动 + 低竞争
    NOT: chase high-volume mainstream markets.

    Returns:
        (score, reward_info, competition_info, volatility)
    """
    empty = (0.0, RewardInfo(), CompetitionInfo(), 0.0)

    # --- Hard filters ---

    # 0. Blacklist check
    if _is_blacklisted(market):
        return empty

    # 1. Must have rewards (critical - no rewards = wasted effort)
    reward_info = parse_reward_info(market)
    if require_rewards and not reward_info.has_rewards:
        return empty

    # 2. Reward pool size: sweet spot is 1-100
    #    Too low (<1) = not worth it; too high (>100) often = mainstream + competitive
    if reward_info.has_rewards and reward_info.total_rewards > 0:
        if reward_info.total_rewards < min_reward_pool:
            return empty
        # Don't hard-filter high rewards, but penalize in scoring

    midpoint = _get_midpoint(market)
    days_to_expiry = _get_days_to_expiry(market)
    daily_volume = _get_daily_volume(market)
    category = _get_category(market)

    # 3. Probability range (tighter than before: 15-85% for safety)
    if midpoint < min_probability or midpoint > max_probability:
        return empty

    # 4. Time to expiry
    if days_to_expiry < min_days_to_expiry:
        return empty

    # 5. Minimum activity (skip filter if volume data not available)
    #    Real API sampling markets don't always include volume
    if daily_volume > 0 and daily_volume < min_daily_volume:
        return empty

    # 6. Market must be active
    if not market.get("active", True):
        return empty

    # --- Volatility assessment ---
    volatility = estimate_volatility(price_history)
    if volatility > max_volatility:
        return empty  # Too volatile for script

    # --- Competition assessment ---
    competition = assess_competition(orderbook)

    # --- Scoring: reward_potential / (volatility × competition) ---

    # A. Reward score (higher rewards = better, but diminishing returns)
    if reward_info.has_rewards and reward_info.total_rewards > 0:
        # Log scale: reward 5 -> 1.6, reward 10 -> 2.3, reward 30 -> 3.4
        reward_score = math.log2(max(reward_info.total_rewards, 1)) + 1
        # Penalize very high reward pools (likely competitive)
        if reward_info.total_rewards > max_reward_pool:
            reward_score *= 0.6  # 40% penalty for mainstream markets
        elif reward_info.total_rewards > 30:
            reward_score *= 0.8  # 20% penalty
    else:
        reward_score = 0.5  # Fallback if no reward data

    # B. Volatility score (lower = better)
    # < 2¢/day = perfect, 2-5¢ = good, 5-8¢ = acceptable
    if volatility < 2.0:
        vol_score = 1.0
    elif volatility < 3.0:
        vol_score = 0.85
    elif volatility < 5.0:
        vol_score = 0.65
    else:
        vol_score = 0.4

    # C. Competition score (lower competition = better)
    comp_score = 1.0 - competition.competition_score  # Invert: low comp = high score

    # D. Category score (niche > mainstream)
    cat_score = CATEGORY_PRIORITY.get(category, 0.5)

    # E. Rebate multiplier (still valuable)
    rebate = get_rebate_rate(category)
    rebate_mult = 1.0 + rebate  # Finance: 1.5, others: 1.25

    # F. Probability safety (30-70% best for LP rewards due to quadratic scoring)
    prob_dist = abs(midpoint - 0.5)
    if prob_dist < 0.15:       # 0.35-0.65
        safety_score = 1.0
    elif prob_dist < 0.25:     # 0.25-0.75
        safety_score = 0.75
    elif prob_dist < 0.35:     # 0.15-0.85
        safety_score = 0.5
    else:
        safety_score = 0.2

    # G. Expiry score (longer = safer, but not too long)
    if 14 < days_to_expiry <= 90:
        expiry_score = 1.0     # Sweet spot
    elif days_to_expiry > 90:
        expiry_score = 0.85    # Very long, still fine
    elif days_to_expiry > 7:
        expiry_score = 0.5
    else:
        expiry_score = 0.2

    # Final score: reward-driven, penalized by volatility and competition
    score = (
        reward_score
        * vol_score
        * comp_score
        * cat_score
        * rebate_mult
        * safety_score
        * expiry_score
    )

    return (round(score, 4), reward_info, competition, volatility)


def select_markets(
    markets: list[dict],
    max_markets: int = 5,
    config: dict | None = None,
    orderbooks: dict[str, dict] | None = None,
    price_histories: dict[str, list[float]] | None = None,
) -> list[ScoredMarket]:
    """Select top markets for LP farming.

    Pipeline:
    1. Hard filter (rewards, probability, expiry, blacklist)
    2. Score by reward/volatility/competition ratio
    3. Return top N

    Args:
        markets: Raw market data from Polymarket API.
        max_markets: Maximum number of markets to select.
        config: Market selection config dict.
        orderbooks: Optional pre-fetched orderbook data per token_id.
        price_histories: Optional historical price data per token_id.
    """
    if config is None:
        config = {}
    if orderbooks is None:
        orderbooks = {}
    if price_histories is None:
        price_histories = {}

    min_prob = config.get("min_probability", 0.15)
    max_prob = config.get("max_probability", 0.85)
    min_expiry = config.get("min_days_to_expiry", 7)
    min_vol = config.get("min_daily_volume", 200)
    require_rewards = config.get("require_rewards", True)
    min_reward_pool = config.get("min_reward_pool", 1.0)
    max_reward_pool = config.get("max_reward_pool", 100.0)
    max_volatility = config.get("max_volatility_cents", 8.0)

    scored = []
    filtered_counts = {"blacklist": 0, "no_rewards": 0, "low_score": 0, "passed": 0}

    for market in markets:
        tokens = market.get("tokens", [])
        if not tokens:
            continue

        token_id = tokens[0].get("token_id", "")
        ob = orderbooks.get(token_id)
        ph = price_histories.get(token_id)

        s, reward_info, comp_info, volatility = score_market(
            market,
            min_probability=min_prob,
            max_probability=max_prob,
            min_days_to_expiry=min_expiry,
            min_daily_volume=min_vol,
            require_rewards=require_rewards,
            min_reward_pool=min_reward_pool,
            max_reward_pool=max_reward_pool,
            max_volatility=max_volatility,
            orderbook=ob,
            price_history=ph,
        )

        if s <= 0:
            if _is_blacklisted(market):
                filtered_counts["blacklist"] += 1
            elif require_rewards and not reward_info.has_rewards:
                filtered_counts["no_rewards"] += 1
            else:
                filtered_counts["low_score"] += 1
            continue

        filtered_counts["passed"] += 1
        complement = tokens[1] if len(tokens) > 1 else None

        scored.append(ScoredMarket(
            token_id=token_id,
            condition_id=market.get("condition_id", ""),
            question=market.get("question", "Unknown"),
            outcome=tokens[0].get("outcome", "Yes"),
            category=_get_category(market),
            midpoint=_get_midpoint(market),
            daily_volume=_get_daily_volume(market),
            days_to_expiry=_get_days_to_expiry(market),
            score=s,
            reward_info=reward_info,
            competition_info=comp_info,
            volatility=volatility,
            complement_token_id=complement.get("token_id") if complement else None,
        ))

    # Sort by score descending (reward/vol/comp optimized)
    scored.sort(key=lambda m: m.score, reverse=True)
    selected = scored[:max_markets]

    # Log selection summary
    logger.info(
        f"Market filter: {filtered_counts['passed']} passed, "
        f"{filtered_counts['no_rewards']} no rewards, "
        f"{filtered_counts['blacklist']} blacklisted, "
        f"{filtered_counts['low_score']} low score"
    )
    if selected:
        logger.info(f"Selected {len(selected)} markets for LP farming:")
        for m in selected:
            reward_str = f"R={m.reward_info.total_rewards:.1f}" if m.reward_info.has_rewards else "no-R"
            comp_str = f"C={m.competition_info.competition_score:.1f}"
            logger.info(
                f"  [{m.category}] {m.question[:50]}... "
                f"score={m.score:.2f} mid={m.midpoint:.2f} "
                f"vol={m.volatility:.1f}¢ {reward_str} {comp_str}"
            )

    return selected


# -- Helpers to extract data from market dicts --

def _get_midpoint(market: dict) -> float:
    """Extract midpoint/probability from market data."""
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
