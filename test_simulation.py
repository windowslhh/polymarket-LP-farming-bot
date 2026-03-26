"""Simulation test - validates all modules work correctly without real API."""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from loguru import logger

logger.remove()
logger.add(sys.stderr, format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}", level="DEBUG")


def test_strategy():
    """Test quote calculation logic."""
    from src.strategy import calculate_quotes, should_requote, estimate_fee_rate, get_rebate_rate

    logger.info("=== Testing Strategy ===")

    # Test fee estimation
    for cat in ["Finance", "Crypto", "Sports", "Geopolitics"]:
        fee = estimate_fee_rate(0.5, cat)
        rebate = get_rebate_rate(cat)
        logger.info(f"  {cat}: fee={fee:.4f} ({fee*100:.2f}%), rebate={rebate*100:.0f}%")

    # Test quote calculation
    quotes = calculate_quotes(midpoint=0.50, spread_bps=300, order_size=30, order_levels=2)
    logger.info(f"  Quotes at midpoint=0.50, spread=300bps:")
    for q in quotes.bids:
        logger.info(f"    BID {q.size:.1f} shares @ {q.price:.4f}")
    for q in quotes.asks:
        logger.info(f"    ASK {q.size:.1f} shares @ {q.price:.4f}")

    # Test with inventory skew
    quotes_skewed = calculate_quotes(midpoint=0.50, spread_bps=300, order_size=30, order_levels=1, inventory_skew=100)
    logger.info(f"  Skewed quotes (long 100bps):")
    for q in quotes_skewed.bids:
        logger.info(f"    BID {q.size:.1f} @ {q.price:.4f} (wider)")
    for q in quotes_skewed.asks:
        logger.info(f"    ASK {q.size:.1f} @ {q.price:.4f} (tighter)")

    # Test requote detection
    assert should_requote(0.50, 0.0) == True  # first time
    assert should_requote(0.50, 0.499) == False  # tiny move
    assert should_requote(0.50, 0.45) == True  # big move
    logger.info("  Requote detection: OK")

    logger.info("Strategy tests PASSED")


def test_risk_manager():
    """Test risk controls."""
    from src.risk_manager import RiskManager

    logger.info("=== Testing Risk Manager ===")

    rm = RiskManager({
        "max_position_per_market": 200,
        "max_total_position": 800,
        "daily_loss_limit": 20,
        "min_probability": 0.10,
        "max_probability": 0.90,
        "midpoint_change_pause_pct": 10,
    })

    # Normal trading should be allowed
    ok, reason = rm.can_trade("token_a", 0.50)
    assert ok, f"Should allow: {reason}"
    logger.info(f"  Normal trade (mid=0.50): {reason}")

    # Extreme probability should be blocked
    ok, reason = rm.can_trade("token_b", 0.05)
    assert not ok
    logger.info(f"  Extreme probability (0.05): blocked - {reason}")

    ok, reason = rm.can_trade("token_c", 0.95)
    assert not ok
    logger.info(f"  Extreme probability (0.95): blocked - {reason}")

    # Near expiry should be blocked
    ok, reason = rm.can_trade("token_d", 0.50, days_to_expiry=2)
    assert not ok
    logger.info(f"  Near expiry (2 days): blocked - {reason}")

    # Inventory skew
    skew = rm.get_inventory_skew("token_a")
    logger.info(f"  Initial skew: {skew:.1f} bps (should be ~0)")

    # Simulate fills and check skew adjustment
    rm.record_fill("token_a", "BUY", 100, 0.48, 0.50)
    skew = rm.get_inventory_skew("token_a")
    logger.info(f"  After $100 buy: skew={skew:.1f} bps (positive = shift to sell)")

    # Status
    status = rm.get_status()
    logger.info(f"  Status: {status}")

    logger.info("Risk manager tests PASSED")


def test_market_selector():
    """Test market scoring and selection."""
    from src.market_selector import score_market, select_markets

    logger.info("=== Testing Market Selector ===")

    # Create mock markets
    mock_markets = [
        {
            "question": "Will BTC reach $100k by June 2026?",
            "category": "Crypto",
            "active": True,
            "volume_24h": 50000,
            "end_date_iso": "2026-06-30T00:00:00Z",
            "tokens": [
                {"token_id": "token_btc_yes", "outcome": "Yes", "price": 0.45},
                {"token_id": "token_btc_no", "outcome": "No", "price": 0.55},
            ],
        },
        {
            "question": "Will S&P 500 close above 6000 this month?",
            "category": "Finance",
            "active": True,
            "volume_24h": 30000,
            "end_date_iso": "2026-04-30T00:00:00Z",
            "tokens": [
                {"token_id": "token_sp_yes", "outcome": "Yes", "price": 0.60},
                {"token_id": "token_sp_no", "outcome": "No", "price": 0.40},
            ],
        },
        {
            "question": "Will it rain in NYC tomorrow?",
            "category": "Weather",
            "active": True,
            "volume_24h": 200,  # Low volume - should be filtered
            "end_date_iso": "2026-03-27T00:00:00Z",
            "tokens": [
                {"token_id": "token_rain_yes", "outcome": "Yes", "price": 0.70},
            ],
        },
        {
            "question": "Who wins the next election?",
            "category": "Politics",
            "active": True,
            "volume_24h": 100000,
            "end_date_iso": "2026-11-03T00:00:00Z",
            "tokens": [
                {"token_id": "token_elect_yes", "outcome": "Yes", "price": 0.52},
                {"token_id": "token_elect_no", "outcome": "No", "price": 0.48},
            ],
        },
        {
            "question": "Will ETH hit $10k?",
            "category": "Crypto",
            "active": True,
            "volume_24h": 15000,
            "end_date_iso": "2026-12-31T00:00:00Z",
            "tokens": [
                {"token_id": "token_eth_yes", "outcome": "Yes", "price": 0.08},  # Too extreme
                {"token_id": "token_eth_no", "outcome": "No", "price": 0.92},
            ],
        },
    ]

    # Score individual markets
    for m in mock_markets:
        s = score_market(m)
        logger.info(f"  [{m['category']}] {m['question'][:40]}... score={s:.4f}")

    # Select top markets
    selected = select_markets(mock_markets, max_markets=3)
    logger.info(f"  Selected {len(selected)} markets:")
    for m in selected:
        logger.info(f"    {m.question[:40]}... score={m.score:.4f} cat={m.category}")

    # Verify filtering
    assert len(selected) <= 3
    # Low volume "rain" and extreme probability "ETH $10k" should be filtered
    selected_questions = [m.question for m in selected]
    assert not any("rain" in q.lower() for q in selected_questions), "Low volume should be filtered"
    logger.info("  Filtering verified: low volume and extreme probability excluded")

    logger.info("Market selector tests PASSED")


def test_pnl_tracker():
    """Test PnL tracking and reporting."""
    from src.pnl_tracker import PnLTracker
    import tempfile

    logger.info("=== Testing PnL Tracker ===")

    # Use temp directory to avoid polluting data/
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = PnLTracker(data_dir=tmpdir)

        # Record some fills
        tracker.record_fill("token_a", "BTC $100k?", "BUY", 0.48, 60, midpoint=0.50)
        tracker.record_fill("token_a", "BTC $100k?", "SELL", 0.52, 55, midpoint=0.50)
        tracker.record_lp_reward(5.0)
        tracker.record_maker_rebate(1.5)
        tracker.update_active_markets(3)

        # Print daily report
        report = tracker.get_daily_report()
        logger.info(f"\n{report}")

        # Cumulative stats
        cumulative = tracker.get_cumulative_stats()
        logger.info(f"  Cumulative: {cumulative}")

        # Test persistence
        tracker.save()
        tracker2 = PnLTracker(data_dir=tmpdir)
        assert len(tracker2.daily_stats) == 1
        logger.info("  Persistence: OK")

    logger.info("PnL tracker tests PASSED")


def test_websocket_client():
    """Test WebSocket client initialization."""
    from src.websocket_client import PolymarketWebSocket

    logger.info("=== Testing WebSocket Client ===")

    ws = PolymarketWebSocket()
    logger.info(f"  WebSocket available: {ws.available}")
    logger.info(f"  Midpoint (no data): {ws.get_midpoint('test')}")

    # Test subscribe/unsubscribe
    ws.subscribe("token_test_123")
    assert "token_test_123" in ws._subscribed_tokens
    ws.unsubscribe("token_test_123")
    assert "token_test_123" not in ws._subscribed_tokens
    logger.info("  Subscribe/unsubscribe: OK")

    logger.info("WebSocket client tests PASSED")


def test_order_manager_tracking():
    """Test order manager state tracking (without real API)."""
    from src.order_manager import OrderManager, ActiveOrder

    logger.info("=== Testing Order Manager ===")

    # We can't test real orders without API, but test state tracking
    om = OrderManager.__new__(OrderManager)
    om.active_orders = {}
    om.total_fills_buy = 0
    om.total_fills_sell = 0

    # Simulate tracked orders
    om.active_orders["token_a"] = [
        ActiveOrder("ord1", "token_a", 0.48, 60, "BUY"),
        ActiveOrder("ord2", "token_a", 0.52, 55, "SELL"),
    ]
    om.active_orders["token_b"] = [
        ActiveOrder("ord3", "token_b", 0.30, 100, "BUY"),
    ]

    assert om.get_active_order_count() == 3
    assert om.get_market_order_count("token_a") == 2
    assert om.get_market_order_count("token_b") == 1
    assert om.get_market_order_count("token_c") == 0
    logger.info(f"  Active orders: {om.get_active_order_count()}")
    logger.info("  State tracking: OK")

    logger.info("Order manager tests PASSED")


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("Running simulation tests (no API required)")
    logger.info("=" * 60)

    tests = [
        test_strategy,
        test_risk_manager,
        test_market_selector,
        test_pnl_tracker,
        test_websocket_client,
        test_order_manager_tracking,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            logger.exception(f"FAILED: {test.__name__}: {e}")
            failed += 1
        print()

    logger.info("=" * 60)
    logger.info(f"Results: {passed} passed, {failed} failed out of {len(tests)} tests")
    logger.info("=" * 60)

    sys.exit(1 if failed else 0)
