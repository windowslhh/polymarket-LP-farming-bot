"""Run backtest and parameter optimization.

Usage:
    python run_backtest.py              # Full optimization (grid search)
    python run_backtest.py --fast       # Fast mode (reduced grid)
    python run_backtest.py --single     # Single backtest with default params
"""

import argparse
import sys
import os
import time

sys.path.insert(0, os.path.dirname(__file__))

from backtest.data_generator import generate_all_markets, MARKET_TEMPLATES
from backtest.engine import BacktestConfig, run_backtest, reset_fill_counter
from backtest.optimizer import (
    run_optimization,
    print_top_results,
    print_worst_results,
    print_market_breakdown,
    print_parameter_sensitivity,
    PARAM_GRID,
    PARAM_GRID_FAST,
)


def run_single():
    """Run single backtest with default (current) config."""
    print("=" * 70)
    print("  SINGLE BACKTEST - Default Parameters")
    print("=" * 70)

    markets = generate_all_markets()
    config = BacktestConfig(
        spread_bps=300,
        order_size_usdc=30,
        order_levels=2,
        daily_loss_limit=20,
    )

    print(f"\nConfig: spread={config.spread_bps}bp, size=${config.order_size_usdc}, "
          f"levels={config.order_levels}, loss_limit=${config.daily_loss_limit}")
    print()

    total_pnl = 0
    total_lp = 0
    total_rebate = 0
    total_spread = 0
    total_fills = 0
    total_volume = 0

    print(f"{'Market':<35} {'Cat':<10} {'Days':>5} {'Realized':>9} {'LP$':>7} "
          f"{'Rebate':>7} {'Spread$':>8} {'Fills':>6} {'Sharpe':>7} {'Win%':>6}")
    print("-" * 110)

    for market in markets:
        reset_fill_counter()
        result = run_backtest(market, config)

        print(
            f"{result.market_name[:34]:<35} {result.category:<10} "
            f"{result.duration_days:>5} ${result.realized_pnl:>+8.2f} "
            f"${result.total_lp_rewards:>6.2f} ${result.total_maker_rebates:>6.2f} "
            f"${result.total_spread_pnl:>+7.2f} {result.total_fills:>6} "
            f"{result.sharpe_ratio:>+7.2f} {result.win_rate:>5.1f}%"
        )

        total_pnl += result.realized_pnl
        total_lp += result.total_lp_rewards
        total_rebate += result.total_maker_rebates
        total_spread += result.total_spread_pnl
        total_fills += result.total_fills
        total_volume += result.total_volume

    print("-" * 110)
    print(f"{'TOTAL':<35} {'':10} {'':>5} ${total_pnl:>+8.2f} "
          f"${total_lp:>6.2f} ${total_rebate:>6.2f} "
          f"${total_spread:>+7.2f} {total_fills:>6}")

    print(f"\n  Total volume traded: ${total_volume:,.0f}")
    print(f"  Net PnL: ${total_pnl:+.2f}")
    print(f"    Spread capture: ${total_spread:+.2f}")
    print(f"    LP rewards:     ${total_lp:.2f}")
    print(f"    Maker rebates:  ${total_rebate:.2f}")


def run_full_optimization(fast: bool = False):
    """Run grid search optimization."""
    grid = PARAM_GRID_FAST if fast else PARAM_GRID
    mode = "FAST" if fast else "FULL"

    print("=" * 70)
    print(f"  PARAMETER OPTIMIZATION [{mode}]")
    print("=" * 70)
    print()

    # Generate markets
    markets = generate_all_markets()
    print(f"Markets generated: {len(markets)}")
    for m in markets:
        print(f"  [{m.category}] {m.question} "
              f"({m.duration_days}d, vol=${m.total_volume:,.0f})")
    print()

    # Run optimization
    start = time.time()
    results = run_optimization(markets, grid=grid)
    elapsed = time.time() - start

    print(f"\nCompleted in {elapsed:.1f}s")

    # Print results
    print_top_results(results, top_n=15)
    print_worst_results(results, bottom_n=5)

    # Show best config per-market breakdown
    if results:
        print_market_breakdown(results[0])

    # Parameter sensitivity
    print_parameter_sensitivity(results)

    # Recommendation
    if results:
        best = results[0]
        c = best.config
        print()
        print("=" * 70)
        print("  RECOMMENDED CONFIGURATION")
        print("=" * 70)
        print(f"""
  Based on backtest across {len(markets)} markets:

  strategy:
    spread_bps: {c.spread_bps}
    order_size_usdc: {c.order_size_usdc}
    order_levels: {c.order_levels}

  risk:
    daily_loss_limit: {c.daily_loss_limit}

  Expected performance:
    Realized PnL:    ${best.realized_pnl:+.2f} over test period
    LP Rewards:      ${best.total_lp_rewards:.2f}
    Maker Rebates:   ${best.total_maker_rebates:.2f}
    Avg Daily PnL:   ${best.avg_daily_pnl:+.2f}
    Win Rate:        {best.avg_win_rate:.1f}%
    Avg Sharpe:      {best.avg_sharpe:+.2f}
    Max Drawdown:    ${best.worst_drawdown:.2f}
""")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket LP Backtest")
    parser.add_argument("--fast", action="store_true", help="Reduced parameter grid")
    parser.add_argument("--single", action="store_true", help="Single run with defaults")
    args = parser.parse_args()

    if args.single:
        run_single()
    else:
        run_full_optimization(fast=args.fast)
