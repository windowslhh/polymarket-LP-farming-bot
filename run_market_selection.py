#!/usr/bin/env python3
"""Market selection demo - simulates filtering against realistic Polymarket data.

Since we can't access the real API from this environment, this uses mock data
modeled after real markets visible on polymarket.com/rewards (March 2026).

Run: python run_market_selection.py
"""

import sys
import os
import random

sys.path.insert(0, os.path.dirname(__file__))

from loguru import logger

logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    level="INFO",
)

from src.market_selector import (
    score_market, select_markets, assess_competition,
    estimate_volatility, CATEGORY_PRIORITY,
)


def generate_realistic_markets():
    """Generate mock markets that mirror real Polymarket rewards page (March 2026).

    Categories and reward levels based on actual observed data.
    """
    markets = [
        # === Tier 1: Niche Sports (low competition, predictable) ===
        {
            "question": "Baylor Bears vs Minnesota Golden Gophers - NCAA Tournament",
            "category": "Sports",
            "active": True,
            "volume_24h": 2500,
            "end_date_iso": "2026-04-07T00:00:00Z",
            "rewards": {"total_rewards": 8, "max_spread": 0.04, "min_size": 50},
            "tokens": [
                {"token_id": "ncaa_baylor_yes", "outcome": "Yes", "price": 0.55},
                {"token_id": "ncaa_baylor_no", "outcome": "No", "price": 0.45},
            ],
        },
        {
            "question": "Oklahoma Sooners vs Colorado Buffaloes - NCAA",
            "category": "Sports",
            "active": True,
            "volume_24h": 1800,
            "end_date_iso": "2026-04-07T00:00:00Z",
            "rewards": {"total_rewards": 6, "max_spread": 0.04, "min_size": 50},
            "tokens": [
                {"token_id": "ncaa_oklahoma_yes", "outcome": "Yes", "price": 0.62},
                {"token_id": "ncaa_oklahoma_no", "outcome": "No", "price": 0.38},
            ],
        },
        {
            "question": "Inter Milan vs Juventus - Serie A Match Result",
            "category": "Sports",
            "active": True,
            "volume_24h": 3200,
            "end_date_iso": "2026-04-15T00:00:00Z",
            "rewards": {"total_rewards": 10, "max_spread": 0.04, "min_size": 50},
            "tokens": [
                {"token_id": "seriea_inter_yes", "outcome": "Yes", "price": 0.48},
                {"token_id": "seriea_inter_no", "outcome": "No", "price": 0.52},
            ],
        },
        {
            "question": "Will Manchester City win the Premier League 2025-26?",
            "category": "Sports",
            "active": True,
            "volume_24h": 8000,
            "end_date_iso": "2026-05-25T00:00:00Z",
            "rewards": {"total_rewards": 15, "max_spread": 0.04, "min_size": 50},
            "tokens": [
                {"token_id": "epl_mancity_yes", "outcome": "Yes", "price": 0.35},
                {"token_id": "epl_mancity_no", "outcome": "No", "price": 0.65},
            ],
        },

        # === Tier 1: Science / Environment (ultra-low competition) ===
        {
            "question": "Will Arctic sea ice extent reach new record low in 2026?",
            "category": "Science",
            "active": True,
            "volume_24h": 600,
            "end_date_iso": "2026-09-30T00:00:00Z",
            "rewards": {"total_rewards": 5, "max_spread": 0.04, "min_size": 50},
            "tokens": [
                {"token_id": "arctic_ice_yes", "outcome": "Yes", "price": 0.30},
                {"token_id": "arctic_ice_no", "outcome": "No", "price": 0.70},
            ],
        },
        {
            "question": "Will global average temperature anomaly exceed 1.6°C in 2026?",
            "category": "Science",
            "active": True,
            "volume_24h": 400,
            "end_date_iso": "2026-12-31T00:00:00Z",
            "rewards": {"total_rewards": 4, "max_spread": 0.04, "min_size": 50},
            "tokens": [
                {"token_id": "temp_anomaly_yes", "outcome": "Yes", "price": 0.45},
                {"token_id": "temp_anomaly_no", "outcome": "No", "price": 0.55},
            ],
        },

        # === Tier 1: Weather (very predictable) ===
        {
            "question": "Will a Category 5 hurricane make US landfall in 2026?",
            "category": "Weather",
            "active": True,
            "volume_24h": 500,
            "end_date_iso": "2026-11-30T00:00:00Z",
            "rewards": {"total_rewards": 6, "max_spread": 0.04, "min_size": 50},
            "tokens": [
                {"token_id": "hurricane_cat5_yes", "outcome": "Yes", "price": 0.20},
                {"token_id": "hurricane_cat5_no", "outcome": "No", "price": 0.80},
            ],
        },

        # === Tier 1: Culture (slow-moving) ===
        {
            "question": "Will Taylor Swift announce a new album before July 2026?",
            "category": "Culture",
            "active": True,
            "volume_24h": 2000,
            "end_date_iso": "2026-07-01T00:00:00Z",
            "rewards": {"total_rewards": 7, "max_spread": 0.04, "min_size": 50},
            "tokens": [
                {"token_id": "swift_album_yes", "outcome": "Yes", "price": 0.60},
                {"token_id": "swift_album_no", "outcome": "No", "price": 0.40},
            ],
        },
        {
            "question": "Will the next James Bond actor be announced by end of 2026?",
            "category": "Culture",
            "active": True,
            "volume_24h": 1200,
            "end_date_iso": "2026-12-31T00:00:00Z",
            "rewards": {"total_rewards": 5, "max_spread": 0.04, "min_size": 50},
            "tokens": [
                {"token_id": "bond_actor_yes", "outcome": "Yes", "price": 0.40},
                {"token_id": "bond_actor_no", "outcome": "No", "price": 0.60},
            ],
        },

        # === Tier 2: Finance (high rebate but competitive) ===
        {
            "question": "Will S&P 500 close above 6000 on April 30?",
            "category": "Finance",
            "active": True,
            "volume_24h": 85000,
            "end_date_iso": "2026-04-30T00:00:00Z",
            "rewards": {"total_rewards": 60, "max_spread": 0.04, "min_size": 100},
            "tokens": [
                {"token_id": "sp500_6000_yes", "outcome": "Yes", "price": 0.58},
                {"token_id": "sp500_6000_no", "outcome": "No", "price": 0.42},
            ],
        },
        {
            "question": "Will Fed cut rates at May 2026 FOMC meeting?",
            "category": "Finance",
            "active": True,
            "volume_24h": 120000,
            "end_date_iso": "2026-05-07T00:00:00Z",
            "rewards": {"total_rewards": 80, "max_spread": 0.04, "min_size": 100},
            "tokens": [
                {"token_id": "fed_cut_may_yes", "outcome": "Yes", "price": 0.35},
                {"token_id": "fed_cut_may_no", "outcome": "No", "price": 0.65},
            ],
        },
        {
            "question": "Will US unemployment rate exceed 4.5% in Q2 2026?",
            "category": "Economics",
            "active": True,
            "volume_24h": 15000,
            "end_date_iso": "2026-07-15T00:00:00Z",
            "rewards": {"total_rewards": 12, "max_spread": 0.04, "min_size": 50},
            "tokens": [
                {"token_id": "unemployment_yes", "outcome": "Yes", "price": 0.25},
                {"token_id": "unemployment_no", "outcome": "No", "price": 0.75},
            ],
        },

        # === Tier 2: Tech (SpaceX etc.) ===
        {
            "question": "Will SpaceX Starship complete orbital flight before July 2026?",
            "category": "Tech",
            "active": True,
            "volume_24h": 25000,
            "end_date_iso": "2026-07-01T00:00:00Z",
            "rewards": {"total_rewards": 20, "max_spread": 0.04, "min_size": 50},
            "tokens": [
                {"token_id": "spacex_orbital_yes", "outcome": "Yes", "price": 0.72},
                {"token_id": "spacex_orbital_no", "outcome": "No", "price": 0.28},
            ],
        },
        {
            "question": "Will SpaceX IPO in 2026?",
            "category": "Tech",
            "active": True,
            "volume_24h": 18000,
            "end_date_iso": "2026-12-31T00:00:00Z",
            "rewards": {"total_rewards": 15, "max_spread": 0.04, "min_size": 50},
            "tokens": [
                {"token_id": "spacex_ipo_yes", "outcome": "Yes", "price": 0.15},
                {"token_id": "spacex_ipo_no", "outcome": "No", "price": 0.85},
            ],
        },

        # === Tier 3: Politics (news catalysts, volatile) ===
        {
            "question": "Will Trump approval rating exceed 50% in April 2026?",
            "category": "Politics",
            "active": True,
            "volume_24h": 45000,
            "end_date_iso": "2026-04-30T00:00:00Z",
            "rewards": {"total_rewards": 35, "max_spread": 0.04, "min_size": 50},
            "tokens": [
                {"token_id": "trump_approval_yes", "outcome": "Yes", "price": 0.42},
                {"token_id": "trump_approval_no", "outcome": "No", "price": 0.58},
            ],
        },
        {
            "question": "Will there be a US government shutdown before June 2026?",
            "category": "Politics",
            "active": True,
            "volume_24h": 30000,
            "end_date_iso": "2026-06-01T00:00:00Z",
            "rewards": {"total_rewards": 25, "max_spread": 0.04, "min_size": 50},
            "tokens": [
                {"token_id": "shutdown_yes", "outcome": "Yes", "price": 0.30},
                {"token_id": "shutdown_no", "outcome": "No", "price": 0.70},
            ],
        },

        # === Tier 4: Crypto (RED OCEAN - should be deprioritized) ===
        {
            "question": "Will BTC be above $100k on April 1?",
            "category": "Crypto",
            "active": True,
            "volume_24h": 200000,
            "end_date_iso": "2026-04-01T00:00:00Z",
            "rewards": {"total_rewards": 100, "max_spread": 0.02, "min_size": 100},
            "tokens": [
                {"token_id": "btc_100k_apr_yes", "outcome": "Yes", "price": 0.65},
                {"token_id": "btc_100k_apr_no", "outcome": "No", "price": 0.35},
            ],
        },
        {
            "question": "Will ETH be above or below $4000 on April 1?",
            "category": "Crypto",
            "active": True,
            "volume_24h": 150000,
            "end_date_iso": "2026-04-01T00:00:00Z",
            "rewards": {"total_rewards": 80, "max_spread": 0.02, "min_size": 100},
            "tokens": [
                {"token_id": "eth_4k_yes", "outcome": "Yes", "price": 0.45},
                {"token_id": "eth_4k_no", "outcome": "No", "price": 0.55},
            ],
        },
        {
            "question": "Will BTC go up or down in the next 5 minutes?",
            "category": "Crypto",
            "active": True,
            "volume_24h": 500000,
            "end_date_iso": "2026-03-27T00:00:00Z",
            "rewards": {"total_rewards": 50, "max_spread": 0.01, "min_size": 200},
            "tokens": [
                {"token_id": "btc_5min_yes", "outcome": "Up", "price": 0.50},
                {"token_id": "btc_5min_no", "outcome": "Down", "price": 0.50},
            ],
        },

        # === Should be FILTERED: No rewards ===
        {
            "question": "Will aliens be confirmed by 2030?",
            "category": "Science",
            "active": True,
            "volume_24h": 300,
            "end_date_iso": "2030-01-01T00:00:00Z",
            "tokens": [
                {"token_id": "aliens_yes", "outcome": "Yes", "price": 0.03},
            ],
        },

        # === Should be FILTERED: Extreme probability ===
        {
            "question": "Will the sun rise tomorrow?",
            "category": "Science",
            "active": True,
            "volume_24h": 1000,
            "end_date_iso": "2026-03-28T00:00:00Z",
            "rewards": {"total_rewards": 1, "max_spread": 0.04, "min_size": 50},
            "tokens": [
                {"token_id": "sunrise_yes", "outcome": "Yes", "price": 0.99},
            ],
        },

        # === Should be FILTERED: Too close to expiry ===
        {
            "question": "Will it snow in NYC today?",
            "category": "Weather",
            "active": True,
            "volume_24h": 5000,
            "end_date_iso": "2026-03-27T00:00:00Z",
            "rewards": {"total_rewards": 3, "max_spread": 0.04, "min_size": 50},
            "tokens": [
                {"token_id": "snow_nyc_yes", "outcome": "Yes", "price": 0.10},
                {"token_id": "snow_nyc_no", "outcome": "No", "price": 0.90},
            ],
        },

        # === Geopolitics: No rebates ===
        {
            "question": "Will Russia-Ukraine ceasefire hold through April?",
            "category": "Geopolitics",
            "active": True,
            "volume_24h": 40000,
            "end_date_iso": "2026-04-30T00:00:00Z",
            "rewards": {"total_rewards": 30, "max_spread": 0.04, "min_size": 50},
            "tokens": [
                {"token_id": "ceasefire_yes", "outcome": "Yes", "price": 0.25},
                {"token_id": "ceasefire_no", "outcome": "No", "price": 0.75},
            ],
        },
    ]

    return markets


def generate_mock_orderbooks(markets):
    """Generate realistic orderbook data for competition assessment."""
    orderbooks = {}
    random.seed(42)

    depth_profiles = {
        # Niche markets: shallow books (low competition)
        "Sports": (2, 150),      # 2 levels, ~$150 per side
        "Science": (1, 80),
        "Weather": (1, 100),
        "Culture": (2, 200),
        # Medium markets
        "Finance": (8, 2000),
        "Economics": (5, 800),
        "Tech": (5, 1000),
        "Politics": (6, 1500),
        # Red ocean
        "Crypto": (15, 5000),
        "Geopolitics": (4, 600),
    }

    for market in markets:
        tokens = market.get("tokens", [])
        if not tokens:
            continue
        tid = tokens[0]["token_id"]
        mid = float(tokens[0]["price"])
        cat = market.get("category", "Other")
        levels, depth_per_side = depth_profiles.get(cat, (3, 500))

        bids = []
        asks = []
        for i in range(levels):
            size = depth_per_side / levels * random.uniform(0.5, 1.5)
            bids.append({"price": str(round(mid - 0.01 * (i + 1), 4)), "size": str(round(size, 1))})
            asks.append({"price": str(round(mid + 0.01 * (i + 1), 4)), "size": str(round(size, 1))})

        orderbooks[tid] = {"bids": bids, "asks": asks}

    return orderbooks


def generate_mock_price_histories(markets):
    """Generate mock price histories for volatility estimation."""
    histories = {}
    random.seed(42)

    volatility_profiles = {
        "Sports": 0.005,      # Very stable between events
        "Science": 0.003,     # Ultra stable
        "Weather": 0.004,
        "Culture": 0.006,
        "Finance": 0.015,     # Moderate
        "Economics": 0.010,
        "Tech": 0.012,
        "Politics": 0.020,    # News-driven spikes
        "Crypto": 0.035,      # Very volatile
        "Geopolitics": 0.025,
    }

    for market in markets:
        tokens = market.get("tokens", [])
        if not tokens:
            continue
        tid = tokens[0]["token_id"]
        mid = float(tokens[0]["price"])
        cat = market.get("category", "Other")
        vol = volatility_profiles.get(cat, 0.01)

        # Generate 288 data points (~1 day at 5-min intervals)
        prices = [mid]
        for _ in range(287):
            change = random.gauss(0, vol)
            new_price = max(0.01, min(0.99, prices[-1] + change))
            # Mean reversion
            new_price += (mid - new_price) * 0.02
            prices.append(round(new_price, 4))

        histories[tid] = prices

    return histories


def main():
    logger.info("=" * 70)
    logger.info("  Polymarket LP Farming Bot - Market Selection Demo")
    logger.info("  Strategy: 高奖励 + 低波动 + 低竞争 (NOT mainstream)")
    logger.info("=" * 70)
    print()

    # Generate realistic mock data
    markets = generate_realistic_markets()
    orderbooks = generate_mock_orderbooks(markets)
    price_histories = generate_mock_price_histories(markets)

    logger.info(f"Total markets available: {len(markets)}")
    print()

    # --- Step 1: Show all markets with scores ---
    logger.info("=" * 70)
    logger.info("  STEP 1: Score all markets")
    logger.info("=" * 70)

    all_scored = []
    for m in markets:
        tokens = m.get("tokens", [])
        tid = tokens[0]["token_id"] if tokens else ""
        ob = orderbooks.get(tid)
        ph = price_histories.get(tid)

        s, ri, ci, vol = score_market(
            m, orderbook=ob, price_history=ph, require_rewards=True,
        )
        all_scored.append((m, s, ri, ci, vol))

    # Sort by score
    all_scored.sort(key=lambda x: x[1], reverse=True)

    print()
    print(f"{'Rank':<5} {'Score':<8} {'Category':<12} {'Reward':<8} {'Vol¢':<7} {'Comp':<6} {'Mid':<6} {'Market'}")
    print("-" * 110)

    for i, (m, s, ri, ci, vol) in enumerate(all_scored):
        cat = m["category"]
        question = m["question"][:50]
        mid = m["tokens"][0]["price"] if m.get("tokens") else 0
        reward_str = f"{ri.total_rewards:.0f}" if ri.has_rewards else "---"
        comp_str = f"{ci.competition_score:.2f}"
        vol_str = f"{vol:.1f}" if vol > 0 else "?"

        if s > 0:
            status = f"#{i+1:<3}"
        else:
            status = " X  "

        print(f"{status}  {s:<8.3f} {cat:<12} {reward_str:<8} {vol_str:<7} {comp_str:<6} {mid:<6} {question}")

    print()

    # --- Step 2: Run selection with config ---
    logger.info("=" * 70)
    logger.info("  STEP 2: Select top markets (max_markets=5)")
    logger.info("=" * 70)

    config = {
        "require_rewards": True,
        "min_reward_pool": 1.0,
        "max_reward_pool": 100.0,
        "max_volatility_cents": 8.0,
        "min_daily_volume": 200,
        "min_probability": 0.15,
        "max_probability": 0.85,
        "min_days_to_expiry": 7,
    }

    selected = select_markets(
        markets,
        max_markets=5,
        config=config,
        orderbooks=orderbooks,
        price_histories=price_histories,
    )

    print()
    print("┌─────────────────────────────────────────────────────────────────────┐")
    print("│                    SELECTED MARKETS (Top 5)                         │")
    print("├─────────────────────────────────────────────────────────────────────┤")

    for i, m in enumerate(selected, 1):
        ri = m.reward_info
        ci = m.competition_info
        print(f"│ #{i}  [{m.category:<10}] {m.question[:48]:<48} │")
        print(f"│     Score: {m.score:.3f}  |  Reward: {ri.total_rewards:.0f}  |  "
              f"Vol: {m.volatility:.1f}¢  |  Comp: {ci.competition_score:.2f}  |  "
              f"Mid: {m.midpoint:.2f}   │")
        depth = ci.bid_depth + ci.ask_depth
        print(f"│     Volume: ${m.daily_volume:,.0f}/day  |  Book depth: ${depth:,.0f}  |  "
              f"Expiry: {m.days_to_expiry:.0f}d          │")
        if i < len(selected):
            print(f"│{'─' * 69}│")

    print("└─────────────────────────────────────────────────────────────────────┘")

    # --- Step 3: Show what was filtered and why ---
    print()
    logger.info("=" * 70)
    logger.info("  STEP 3: Filtered markets (and why)")
    logger.info("=" * 70)
    print()

    selected_tids = {m.token_id for m in selected}
    for m, s, ri, ci, vol in all_scored:
        tokens = m.get("tokens", [])
        tid = tokens[0]["token_id"] if tokens else ""
        if tid in selected_tids or s > 0:
            continue

        # Determine filter reason
        reasons = []
        if not ri.has_rewards:
            reasons.append("NO REWARDS")
        mid = m["tokens"][0]["price"] if m.get("tokens") else 0
        if mid < 0.15 or mid > 0.85:
            reasons.append(f"extreme prob ({mid:.2f})")
        question = m["question"].lower()
        for kw in ["up or down", "above or below", "5-minute", "15-minute"]:
            if kw in question:
                reasons.append(f"blacklisted keyword: '{kw}'")
        from src.market_selector import _get_days_to_expiry
        dte = _get_days_to_expiry(m)
        if dte < 7:
            reasons.append(f"expiry too soon ({dte:.0f}d)")
        if vol > 8.0:
            reasons.append(f"too volatile ({vol:.1f}¢)")

        reason_str = ", ".join(reasons) if reasons else "low score"
        print(f"  ✗ [{m['category']:<12}] {m['question'][:45]:<45} → {reason_str}")

    # --- Summary ---
    print()
    print("=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    passed = sum(1 for _, s, _, _, _ in all_scored if s > 0)
    filtered = len(all_scored) - passed
    print(f"  Total markets:    {len(all_scored)}")
    print(f"  Passed filters:   {passed}")
    print(f"  Filtered out:     {filtered}")
    print(f"  Selected for LP:  {len(selected)}")
    print()

    if selected:
        cats = [m.category for m in selected]
        print(f"  Selected categories: {', '.join(cats)}")
        avg_reward = sum(m.reward_info.total_rewards for m in selected) / len(selected)
        avg_vol = sum(m.volatility for m in selected) / len(selected)
        avg_comp = sum(m.competition_info.competition_score for m in selected) / len(selected)
        print(f"  Avg reward pool:  {avg_reward:.1f}")
        print(f"  Avg volatility:   {avg_vol:.1f}¢/day")
        print(f"  Avg competition:  {avg_comp:.2f} (lower = better)")
        print()
        print("  Strategy: niche markets with good rewards, low vol, few competing bots")
        print("  核心逻辑: 高奖励 + 低波动 + 低竞争 ✓")


if __name__ == "__main__":
    main()
