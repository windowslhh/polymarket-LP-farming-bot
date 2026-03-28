"""Run volume farming backtest and stop-loss parameter optimization.

Usage:
    python run_volume_backtest.py              # Full optimization
    python run_volume_backtest.py --fast       # Fast mode (reduced grid)
    python run_volume_backtest.py --single     # Single run with defaults
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

from backtest.volume_data import generate_all_volume_markets, VOLUME_MARKET_SCENARIOS
from backtest.volume_engine import VolumeBacktestConfig, run_volume_backtest
from backtest.volume_optimizer import (
    VOLUME_PARAM_GRID,
    VOLUME_PARAM_GRID_FAST,
    print_exit_reason_analysis,
    print_parameter_sensitivity,
    print_position_details,
    print_volume_top_results,
    print_volume_worst_results,
    run_volume_optimization,
)


def run_single():
    """Run single backtest with default stop-loss parameters."""
    print("=" * 70)
    print("  VOLUME FARMING BACKTEST - Default Parameters")
    print("=" * 70)

    markets = generate_all_volume_markets()
    config = VolumeBacktestConfig()

    print(f"\nConfig:")
    print(f"  Capital: ${config.capital:.0f}")
    print(f"  Kelly fraction: {config.kelly_fraction}")
    print(f"  Max position: {config.max_position_pct:.0%} of capital")
    print(f"  Edge assumption: +{config.edge_assumption:.0%}")
    print(f"  EV buffer: {config.ev_buffer}")
    print(f"  EV persistence: {config.ev_persistence_minutes}min")
    print(f"  Hard stop: {config.hard_stop_probability}")
    print(f"  Drift threshold: {config.drift_sigma_threshold}σ")
    print(f"  Drawdown tiers: {config.drawdown_by_entry_prob}")
    print()

    print(f"Markets ({len(markets)}):")
    for m in markets:
        print(f"  [{m.category:<10}] {m.question[:45]:<46} "
              f"p={m.initial_price:.2f} res={m.resolution} "
              f"{m.duration_days}d vol=${m.daily_volume:,.0f}")
    print()

    result = run_volume_backtest(markets, config)

    # Print detailed results
    print_position_details(result)
    print_exit_reason_analysis(result)

    # Summary
    print("=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    print(f"  Total trades:      {result.total_trades}")
    print(f"  Winning trades:    {result.winning_trades}")
    print(f"  Losing trades:     {result.losing_trades}")
    print(f"  Win rate:          {result.win_rate:.1f}%")
    print(f"  Whipsawed:         {result.whipsaw_count}")
    print(f"  Stop-loss exits:   {result.stop_loss_exits}")
    print(f"  Avg hold:          {result.avg_hold_days:.1f} days")
    print()
    print(f"  Gross PnL:         ${result.total_gross_pnl:+.4f}")
    print(f"  Total fees:        ${result.total_fees:.4f}")
    print(f"  Net PnL:           ${result.total_net_pnl:+.4f}")
    print(f"  Max single loss:   ${result.max_single_loss:+.4f}")
    print(f"  Total volume:      ${result.total_volume:,.0f}")
    print(f"  Avg drawdown:      {result.avg_drawdown:.1%}")
    print()

    # Key insight
    if result.whipsaw_count > 0:
        whip_loss = sum(p.net_pnl for p in result.positions if p.was_whipsawed)
        print(f"  ⚠ Whipsaw analysis: {result.whipsaw_count} trades exited at loss but would have profited.")
        print(f"    Total whipsaw cost: ${whip_loss:+.4f}")
        print(f"    Consider: wider ev_buffer or longer persistence_minutes")
    print()


def run_optimization(fast: bool = False):
    """Run grid search optimization over stop-loss parameters."""
    grid = VOLUME_PARAM_GRID_FAST if fast else VOLUME_PARAM_GRID
    mode = "FAST" if fast else "FULL"

    print("=" * 70)
    print(f"  VOLUME FARMING STOP-LOSS OPTIMIZATION [{mode}]")
    print("=" * 70)
    print()

    # Generate markets
    markets = generate_all_volume_markets()
    print(f"Markets: {len(markets)}")
    for m in markets:
        scenario = next((s for s in VOLUME_MARKET_SCENARIOS if s["name"] == m.question), {})
        archetype = scenario.get("archetype", "unknown")
        print(f"  [{archetype:<16}] {m.question[:40]:<41} "
              f"p={m.initial_price:.2f} res={m.resolution}")
    print()

    # Run optimization
    start = time.time()
    results = run_volume_optimization(markets, grid=grid)
    elapsed = time.time() - start

    print(f"\nCompleted {len(results)} combos in {elapsed:.1f}s\n")

    # Print results
    print_volume_top_results(results, top_n=15)
    print_volume_worst_results(results, bottom_n=5)

    # Best config details
    if results:
        print_position_details(results[0])
        print_exit_reason_analysis(results[0])

    # Parameter sensitivity
    print_parameter_sensitivity(results)

    # Recommendation
    if results:
        best = results[0]
        c = best.config
        print()
        print("=" * 70)
        print("  RECOMMENDED STOP-LOSS CONFIGURATION")
        print("=" * 70)
        print(f"""
  Based on backtest across {len(markets)} scenarios:

  volume_farming:
    ev_buffer: {c.ev_buffer}
    ev_persistence_minutes: {c.ev_persistence_minutes}
    hard_stop_probability: {c.hard_stop_probability}
    drift_sigma_threshold: {c.drift_sigma_threshold}

  Performance:
    Net PnL:          ${best.total_net_pnl:+.4f}
    Win Rate:         {best.win_rate:.1f}%
    Whipsaws:         {best.whipsaw_count}
    Stop-loss exits:  {best.stop_loss_exits}
    Avg Hold:         {best.avg_hold_days:.1f} days
    Total Volume:     ${best.total_volume:,.0f}
    Max Single Loss:  ${best.max_single_loss:+.4f}
""")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Volume Farming Backtest")
    parser.add_argument("--fast", action="store_true", help="Reduced parameter grid")
    parser.add_argument("--single", action="store_true", help="Single run with defaults")
    args = parser.parse_args()

    if args.single:
        run_single()
    else:
        run_optimization(fast=args.fast)
