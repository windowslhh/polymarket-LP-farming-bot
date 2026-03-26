#!/usr/bin/env python3
"""Market selection - fetches real Polymarket data and runs selection pipeline.

Uses real CLOB API to:
1. Fetch all sampling/rewards markets
2. Fetch orderbooks for competition assessment
3. Run selection: 高奖励 + 低波动 + 低竞争

Usage:
    python run_market_selection.py              # Real API (default)
    python run_market_selection.py --mock       # Mock data (no API needed)
    python run_market_selection.py --top 10     # Show top 10
    python run_market_selection.py --all        # Show all scored markets
    python run_market_selection.py --debug      # Verbose logging
"""

import argparse
import json
import sys
import os
import time

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
    estimate_volatility, _is_blacklisted,
)


def fetch_real_markets():
    """Fetch markets from real Polymarket CLOB API (no auth needed)."""
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON

    HOST = "https://clob.polymarket.com"

    logger.info(f"Connecting to Polymarket CLOB API: {HOST}")
    client = ClobClient(HOST, chain_id=POLYGON)

    # Test connection
    try:
        ok = client.get_ok()
        if ok != "OK" and not (isinstance(ok, dict) and ok.get("status") == "OK"):
            raise ConnectionError(f"API returned: {ok}")
        logger.info("Connected to Polymarket CLOB API")
    except Exception as e:
        logger.error(f"Cannot connect to API: {e}")
        return None, None

    # 1. Fetch sampling markets (these are reward-eligible)
    logger.info("Fetching sampling/rewards markets...")
    all_markets = []
    cursor = "MA=="
    for page in range(10):  # Up to 10 pages
        try:
            result = client.get_sampling_simplified_markets(cursor)
        except Exception as e:
            logger.warning(f"Page {page + 1} failed: {e}")
            break

        if isinstance(result, list):
            all_markets.extend(result)
            break
        data = result.get("data", [])
        if not data:
            break
        all_markets.extend(data)
        cursor = result.get("next_cursor", "")
        logger.info(f"  Page {page + 1}: {len(data)} markets (total: {len(all_markets)})")
        if not cursor or cursor == "MA==":
            break
        time.sleep(0.3)  # Rate limit

    if not all_markets:
        # Fallback: try regular markets
        logger.info("No sampling markets, trying regular markets...")
        cursor = "MA=="
        for page in range(5):
            try:
                result = client.get_simplified_markets(cursor)
            except Exception as e:
                logger.warning(f"Page {page + 1} failed: {e}")
                break
            if isinstance(result, list):
                all_markets.extend(result)
                break
            data = result.get("data", [])
            if not data:
                break
            all_markets.extend(data)
            cursor = result.get("next_cursor", "")
            if not cursor or cursor == "MA==":
                break
            time.sleep(0.3)

    logger.info(f"Total markets fetched: {len(all_markets)}")

    if not all_markets:
        return None, None

    # 2. Fetch orderbooks for top candidate markets (for competition assessment)
    logger.info("Fetching orderbooks for competition assessment...")
    orderbooks = {}
    fetched = 0
    for market in all_markets:
        tokens = market.get("tokens", [])
        if not tokens:
            continue

        # Quick pre-filter: skip obviously bad markets
        if _is_blacklisted(market):
            continue

        tid = tokens[0].get("token_id", "")
        if not tid:
            continue

        try:
            ob = client.get_order_book(tid)
            orderbooks[tid] = ob
            fetched += 1
            if fetched % 10 == 0:
                logger.info(f"  Fetched {fetched} orderbooks...")
            time.sleep(0.2)  # Rate limit
        except Exception:
            pass

        if fetched >= 60:  # Limit API calls
            break

    logger.info(f"Fetched {fetched} orderbooks")

    return all_markets, orderbooks


def generate_mock_markets():
    """Generate mock markets for testing without API."""
    # (Same mock data as before, but imported from the demo)
    import random
    random.seed(42)

    markets = [
        {"question": "Baylor Bears vs Minnesota Golden Gophers - NCAA Tournament", "category": "Sports",
         "active": True, "volume_24h": 2500, "end_date_iso": "2026-04-07T00:00:00Z",
         "rewards": {"total_rewards": 8, "max_spread": 0.04, "min_size": 50},
         "tokens": [{"token_id": "ncaa_baylor_yes", "outcome": "Yes", "price": 0.55},
                     {"token_id": "ncaa_baylor_no", "outcome": "No", "price": 0.45}]},
        {"question": "Oklahoma Sooners vs Colorado Buffaloes - NCAA", "category": "Sports",
         "active": True, "volume_24h": 1800, "end_date_iso": "2026-04-07T00:00:00Z",
         "rewards": {"total_rewards": 6, "max_spread": 0.04, "min_size": 50},
         "tokens": [{"token_id": "ncaa_oklahoma_yes", "outcome": "Yes", "price": 0.62},
                     {"token_id": "ncaa_oklahoma_no", "outcome": "No", "price": 0.38}]},
        {"question": "Inter Milan vs Juventus - Serie A Match Result", "category": "Sports",
         "active": True, "volume_24h": 3200, "end_date_iso": "2026-04-15T00:00:00Z",
         "rewards": {"total_rewards": 10, "max_spread": 0.04, "min_size": 50},
         "tokens": [{"token_id": "seriea_inter_yes", "outcome": "Yes", "price": 0.48},
                     {"token_id": "seriea_inter_no", "outcome": "No", "price": 0.52}]},
        {"question": "Will Manchester City win the Premier League 2025-26?", "category": "Sports",
         "active": True, "volume_24h": 8000, "end_date_iso": "2026-05-25T00:00:00Z",
         "rewards": {"total_rewards": 15, "max_spread": 0.04, "min_size": 50},
         "tokens": [{"token_id": "epl_mancity_yes", "outcome": "Yes", "price": 0.35},
                     {"token_id": "epl_mancity_no", "outcome": "No", "price": 0.65}]},
        {"question": "Will Arctic sea ice extent reach new record low in 2026?", "category": "Science",
         "active": True, "volume_24h": 600, "end_date_iso": "2026-09-30T00:00:00Z",
         "rewards": {"total_rewards": 5, "max_spread": 0.04, "min_size": 50},
         "tokens": [{"token_id": "arctic_ice_yes", "outcome": "Yes", "price": 0.30},
                     {"token_id": "arctic_ice_no", "outcome": "No", "price": 0.70}]},
        {"question": "Will global average temperature anomaly exceed 1.6°C in 2026?", "category": "Science",
         "active": True, "volume_24h": 400, "end_date_iso": "2026-12-31T00:00:00Z",
         "rewards": {"total_rewards": 4, "max_spread": 0.04, "min_size": 50},
         "tokens": [{"token_id": "temp_anomaly_yes", "outcome": "Yes", "price": 0.45},
                     {"token_id": "temp_anomaly_no", "outcome": "No", "price": 0.55}]},
        {"question": "Will a Category 5 hurricane make US landfall in 2026?", "category": "Weather",
         "active": True, "volume_24h": 500, "end_date_iso": "2026-11-30T00:00:00Z",
         "rewards": {"total_rewards": 6, "max_spread": 0.04, "min_size": 50},
         "tokens": [{"token_id": "hurricane_cat5_yes", "outcome": "Yes", "price": 0.20},
                     {"token_id": "hurricane_cat5_no", "outcome": "No", "price": 0.80}]},
        {"question": "Will Taylor Swift announce a new album before July 2026?", "category": "Culture",
         "active": True, "volume_24h": 2000, "end_date_iso": "2026-07-01T00:00:00Z",
         "rewards": {"total_rewards": 7, "max_spread": 0.04, "min_size": 50},
         "tokens": [{"token_id": "swift_album_yes", "outcome": "Yes", "price": 0.60},
                     {"token_id": "swift_album_no", "outcome": "No", "price": 0.40}]},
        {"question": "Will S&P 500 close above 6000 on April 30?", "category": "Finance",
         "active": True, "volume_24h": 85000, "end_date_iso": "2026-04-30T00:00:00Z",
         "rewards": {"total_rewards": 60, "max_spread": 0.04, "min_size": 100},
         "tokens": [{"token_id": "sp500_6000_yes", "outcome": "Yes", "price": 0.58},
                     {"token_id": "sp500_6000_no", "outcome": "No", "price": 0.42}]},
        {"question": "Will Fed cut rates at May 2026 FOMC meeting?", "category": "Finance",
         "active": True, "volume_24h": 120000, "end_date_iso": "2026-05-07T00:00:00Z",
         "rewards": {"total_rewards": 80, "max_spread": 0.04, "min_size": 100},
         "tokens": [{"token_id": "fed_cut_may_yes", "outcome": "Yes", "price": 0.35},
                     {"token_id": "fed_cut_may_no", "outcome": "No", "price": 0.65}]},
        {"question": "Will BTC be above $100k on April 1?", "category": "Crypto",
         "active": True, "volume_24h": 200000, "end_date_iso": "2026-04-01T00:00:00Z",
         "rewards": {"total_rewards": 100, "max_spread": 0.02, "min_size": 100},
         "tokens": [{"token_id": "btc_100k_apr_yes", "outcome": "Yes", "price": 0.65},
                     {"token_id": "btc_100k_apr_no", "outcome": "No", "price": 0.35}]},
        {"question": "Will BTC go up or down in the next 5 minutes?", "category": "Crypto",
         "active": True, "volume_24h": 500000, "end_date_iso": "2026-03-27T00:00:00Z",
         "rewards": {"total_rewards": 50, "max_spread": 0.01, "min_size": 200},
         "tokens": [{"token_id": "btc_5min_yes", "outcome": "Up", "price": 0.50},
                     {"token_id": "btc_5min_no", "outcome": "Down", "price": 0.50}]},
        {"question": "Will SpaceX Starship complete orbital flight before July 2026?", "category": "Tech",
         "active": True, "volume_24h": 25000, "end_date_iso": "2026-07-01T00:00:00Z",
         "rewards": {"total_rewards": 20, "max_spread": 0.04, "min_size": 50},
         "tokens": [{"token_id": "spacex_orbital_yes", "outcome": "Yes", "price": 0.72},
                     {"token_id": "spacex_orbital_no", "outcome": "No", "price": 0.28}]},
        {"question": "Will Trump approval rating exceed 50% in April 2026?", "category": "Politics",
         "active": True, "volume_24h": 45000, "end_date_iso": "2026-04-30T00:00:00Z",
         "rewards": {"total_rewards": 35, "max_spread": 0.04, "min_size": 50},
         "tokens": [{"token_id": "trump_approval_yes", "outcome": "Yes", "price": 0.42},
                     {"token_id": "trump_approval_no", "outcome": "No", "price": 0.58}]},
        {"question": "No rewards market - should be filtered", "category": "Politics",
         "active": True, "volume_24h": 5000, "end_date_iso": "2026-08-01T00:00:00Z",
         "tokens": [{"token_id": "noreward", "outcome": "Yes", "price": 0.50}]},
    ]

    # Generate mock orderbooks
    orderbooks = {}
    depth_profiles = {
        "Sports": (2, 150), "Science": (1, 80), "Weather": (1, 100),
        "Culture": (2, 200), "Finance": (8, 2000), "Economics": (5, 800),
        "Tech": (5, 1000), "Politics": (6, 1500), "Crypto": (15, 5000),
    }
    for market in markets:
        tokens = market.get("tokens", [])
        if not tokens:
            continue
        tid = tokens[0]["token_id"]
        mid = float(tokens[0]["price"])
        cat = market.get("category", "Other")
        levels, depth = depth_profiles.get(cat, (3, 500))
        bids = [{"price": str(round(mid - 0.01 * (i + 1), 4)),
                 "size": str(round(depth / levels * 0.8, 1))} for i in range(levels)]
        asks = [{"price": str(round(mid + 0.01 * (i + 1), 4)),
                 "size": str(round(depth / levels * 0.8, 1))} for i in range(levels)]
        orderbooks[tid] = {"bids": bids, "asks": asks}

    return markets, orderbooks


def display_results(all_scored, selected, show_all=False):
    """Display scoring results in a nice table."""
    print()
    print("=" * 110)
    print("  MARKET RANKING (sorted by score)")
    print("=" * 110)
    print()
    print(f"{'Rank':<5} {'Score':<8} {'Category':<12} {'Reward':<8} {'Vol¢':<7} {'Comp':<6} {'Mid':<6} {'Market'}")
    print("-" * 110)

    display = all_scored if show_all else all_scored[:20]
    for i, (m, s, ri, ci, vol) in enumerate(display):
        cat = m.get("category", "?")
        tokens = m.get("tokens", [])
        mid = float(tokens[0].get("price", 0)) if tokens else 0
        question = m.get("question", "?")[:50]
        reward_str = f"{ri.total_rewards:.0f}" if ri.has_rewards else "---"
        comp_str = f"{ci.competition_score:.2f}"
        vol_str = f"{vol:.1f}" if vol > 0 else "?"

        if s > 0:
            status = f"#{i+1:<3}"
        else:
            status = " X  "

        print(f"{status}  {s:<8.3f} {cat:<12} {reward_str:<8} {vol_str:<7} {comp_str:<6} {mid:<6.2f} {question}")

    if not show_all and len(all_scored) > 20:
        print(f"  ... and {len(all_scored) - 20} more (use --all to show all)")

    # Selected markets detail
    print()
    print("┌─────────────────────────────────────────────────────────────────────────┐")
    print(f"│                    SELECTED MARKETS (Top {len(selected)})                          │")
    print("├─────────────────────────────────────────────────────────────────────────┤")

    for i, m in enumerate(selected, 1):
        ri = m.reward_info
        ci = m.competition_info
        print(f"│ #{i}  [{m.category:<10}] {m.question[:50]:<50}│")
        print(f"│     Score: {m.score:.3f}  |  Reward: {ri.total_rewards:.0f}  |  "
              f"Vol: {m.volatility:.1f}¢  |  Comp: {ci.competition_score:.2f}  |  "
              f"Mid: {m.midpoint:.2f} │")
        depth = ci.bid_depth + ci.ask_depth
        print(f"│     Volume: ${m.daily_volume:,.0f}/day  |  Book: ${depth:,.0f}  |  "
              f"Expiry: {m.days_to_expiry:.0f}d  |  "
              f"Spread<{ri.max_spread:.2f} MinSh>{ri.min_shares:.0f}  │")
        if i < len(selected):
            print(f"│{'─' * 73}│")

    print("└─────────────────────────────────────────────────────────────────────────┘")

    # Filtered markets
    print()
    selected_tids = {m.token_id for m in selected}
    filtered = [(m, s, ri, ci, vol) for m, s, ri, ci, vol in all_scored
                if s == 0 and m.get("tokens", [{}])[0].get("token_id", "") not in selected_tids]

    if filtered:
        print(f"Filtered out ({len(filtered)} markets):")
        for m, s, ri, ci, vol in filtered[:10]:
            reasons = []
            if not ri.has_rewards:
                reasons.append("NO REWARDS")
            if _is_blacklisted(m):
                reasons.append("BLACKLISTED")
            tokens = m.get("tokens", [])
            mid = float(tokens[0].get("price", 0)) if tokens else 0
            if mid < 0.15 or mid > 0.85:
                reasons.append(f"prob={mid:.2f}")
            from src.market_selector import _get_days_to_expiry
            dte = _get_days_to_expiry(m)
            if dte < 7:
                reasons.append(f"expiry={dte:.0f}d")
            reason_str = ", ".join(reasons) if reasons else "low score"
            print(f"  ✗ [{m.get('category', '?'):<12}] {m.get('question', '?')[:45]:<45} → {reason_str}")
        if len(filtered) > 10:
            print(f"  ... and {len(filtered) - 10} more")

    # Summary
    print()
    print("=" * 70)
    passed = sum(1 for _, s, _, _, _ in all_scored if s > 0)
    print(f"  Total: {len(all_scored)} markets | Passed: {passed} | Filtered: {len(all_scored) - passed} | Selected: {len(selected)}")
    if selected:
        cats = [m.category for m in selected]
        avg_reward = sum(m.reward_info.total_rewards for m in selected) / len(selected)
        avg_vol = sum(m.volatility for m in selected) / len(selected)
        avg_comp = sum(m.competition_info.competition_score for m in selected) / len(selected)
        print(f"  Categories: {', '.join(cats)}")
        print(f"  Avg reward: {avg_reward:.1f} | Avg volatility: {avg_vol:.1f}¢/day | Avg competition: {avg_comp:.2f}")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="Polymarket LP market selection")
    parser.add_argument("--mock", action="store_true", help="Use mock data instead of real API")
    parser.add_argument("--top", type=int, default=5, help="Number of markets to select (default: 5)")
    parser.add_argument("--all", action="store_true", help="Show all scored markets")
    parser.add_argument("--debug", action="store_true", help="Verbose logging")
    parser.add_argument("--save", type=str, help="Save raw market data to JSON file")
    args = parser.parse_args()

    if args.debug:
        logger.remove()
        logger.add(sys.stderr, format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}", level="DEBUG")

    print()
    logger.info("=" * 70)
    logger.info("  Polymarket LP Farming Bot - Market Selection")
    logger.info("  Strategy: 高奖励 + 低波动 + 低竞争")
    logger.info("=" * 70)
    print()

    # Fetch data
    use_mock = args.mock
    markets = None
    orderbooks = None

    if not use_mock:
        try:
            markets, orderbooks = fetch_real_markets()
        except Exception as e:
            logger.error(f"Real API failed: {e}")
            markets = None

        if not markets:
            logger.warning("Cannot reach Polymarket API. Falling back to mock data...")
            logger.warning("To use real data, run this script on a machine with internet access.")
            use_mock = True

    if use_mock:
        logger.info("Using MOCK data for demonstration")
        markets, orderbooks = generate_mock_markets()

    if not markets:
        logger.error("No market data available")
        sys.exit(1)

    # Save raw data if requested
    if args.save:
        with open(args.save, "w") as f:
            json.dump({"markets": markets, "count": len(markets)}, f, indent=2, default=str)
        logger.info(f"Saved raw market data to {args.save}")

    # Score all markets
    logger.info(f"Scoring {len(markets)} markets...")
    all_scored = []
    for m in markets:
        tokens = m.get("tokens", [])
        tid = tokens[0].get("token_id", "") if tokens else ""
        ob = orderbooks.get(tid) if orderbooks else None

        s, ri, ci, vol = score_market(
            m, orderbook=ob, require_rewards=True,
        )
        all_scored.append((m, s, ri, ci, vol))

    all_scored.sort(key=lambda x: x[1], reverse=True)

    # Select top markets
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
        max_markets=args.top,
        config=config,
        orderbooks=orderbooks or {},
    )

    # Display results
    data_source = "MOCK" if use_mock else "LIVE API"
    print(f"\n  Data source: {data_source}")
    display_results(all_scored, selected, show_all=args.all)

    if use_mock:
        print()
        print("  NOTE: Using mock data. For real results, run on a machine with internet:")
        print("    git clone git@github.com:windowslhh/polymarket-LP-farming-bot.git")
        print("    cd polymarket-LP-farming-bot && pip install -r requirements.txt")
        print("    python run_market_selection.py")


if __name__ == "__main__":
    main()
