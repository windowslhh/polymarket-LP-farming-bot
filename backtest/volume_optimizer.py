"""Parameter optimizer for volume farming stop-loss strategy.

Grid searches over:
- ev_buffer: EV stop-loss buffer zone
- ev_persistence_minutes: How long to wait before confirming EV exit
- hard_stop_probability: Absolute floor
- drift_sigma_threshold: Emergency exit sensitivity
- drawdown tolerances
"""

from dataclasses import dataclass, field

from backtest.volume_data import HighProbMarket
from backtest.volume_engine import VolumeBacktestConfig, VolumeBacktestResult, run_volume_backtest


# Parameter grids
VOLUME_PARAM_GRID = {
    "ev_stop_enabled": [False, True],
    "near_expiry_exit": [False],
    "ev_buffer": [0.03, 0.05, 0.08, 0.10, 0.15],
    "ev_persistence_minutes": [10.0, 30.0, 60.0, 120.0],
    "hard_stop_probability": [0.65, 0.70, 0.75, 0.80],
    "drift_sigma_threshold": [2.0, 3.0, 5.0],
}

VOLUME_PARAM_GRID_FAST = {
    "ev_stop_enabled": [False, True],
    "near_expiry_exit": [False],
    "ev_buffer": [0.05, 0.08, 0.12],
    "ev_persistence_minutes": [15.0, 60.0, 180.0],
    "hard_stop_probability": [0.70, 0.80],
    "drift_sigma_threshold": [2.0, 4.0],
}


def generate_volume_configs(grid: dict) -> list[VolumeBacktestConfig]:
    """Generate all parameter combinations."""
    configs = []
    ev_stop_options = grid.get("ev_stop_enabled", [False])
    near_expiry_options = grid.get("near_expiry_exit", [False])
    for ev_enabled in ev_stop_options:
        for near_expiry in near_expiry_options:
            for ev_buf in grid["ev_buffer"]:
                for persist in grid["ev_persistence_minutes"]:
                    for hard_stop in grid["hard_stop_probability"]:
                        for drift_sig in grid["drift_sigma_threshold"]:
                            configs.append(VolumeBacktestConfig(
                                ev_stop_enabled=ev_enabled,
                                near_expiry_exit=near_expiry,
                                ev_buffer=ev_buf,
                                ev_persistence_minutes=persist,
                                hard_stop_probability=hard_stop,
                                drift_sigma_threshold=drift_sig,
                            ))
    return configs


def run_volume_optimization(
    markets: list[HighProbMarket],
    grid: dict | None = None,
    verbose: bool = True,
) -> list[VolumeBacktestResult]:
    """Run grid search over stop-loss parameters.

    Returns results sorted by total net PnL (best first).
    """
    if grid is None:
        grid = VOLUME_PARAM_GRID_FAST

    configs = generate_volume_configs(grid)

    if verbose:
        print(f"Running {len(configs)} parameter combos x {len(markets)} markets")

    results = []
    for i, config in enumerate(configs):
        result = run_volume_backtest(markets, config)
        results.append(result)

        if verbose and (i + 1) % 10 == 0:
            print(f"  Progress: {i + 1}/{len(configs)}...")

    # Sort by total net PnL
    results.sort(key=lambda r: r.total_net_pnl, reverse=True)
    return results


def print_volume_top_results(results: list[VolumeBacktestResult], top_n: int = 10):
    """Print top parameter combinations."""
    print()
    print("=" * 120)
    print(f"  TOP {top_n} STOP-LOSS PARAMETER COMBINATIONS")
    print("=" * 120)
    print()
    print(
        f"{'#':<3} {'EV':>3} {'Exp':>3} {'EV_buf':>7} {'Persist':>8} {'Hard':>6} {'Drift':>6} "
        f"{'Net PnL':>9} {'Gross':>8} {'Fees':>7} {'Win%':>6} "
        f"{'W/L':>6} {'Whipsaw':>8} {'SL_exit':>8} {'AvgHold':>8} {'MaxLoss':>8} {'Volume':>9}"
    )
    print("-" * 130)

    for rank, r in enumerate(results[:top_n], 1):
        c = r.config
        ev_flag = "ON" if c.ev_stop_enabled else "off"
        exp_flag = "ON" if c.near_expiry_exit else "off"
        print(
            f"{rank:<3} {ev_flag:>3} {exp_flag:>3} {c.ev_buffer:>7.2f} {c.ev_persistence_minutes:>7.0f}m "
            f"{c.hard_stop_probability:>6.2f} {c.drift_sigma_threshold:>5.1f}σ "
            f"${r.total_net_pnl:>+8.3f} ${r.total_gross_pnl:>+7.3f} "
            f"${r.total_fees:>6.3f} {r.win_rate:>5.1f}% "
            f"{r.winning_trades}/{r.losing_trades:>3} {r.whipsaw_count:>8} "
            f"{r.stop_loss_exits:>8} {r.avg_hold_days:>6.1f}d "
            f"${r.max_single_loss:>+7.3f} ${r.total_volume:>8.0f}"
        )
    print()


def print_volume_worst_results(results: list[VolumeBacktestResult], bottom_n: int = 5):
    """Print worst parameter combinations."""
    print(f"  WORST {bottom_n}")
    print("-" * 120)
    for rank, r in enumerate(results[-bottom_n:], 1):
        c = r.config
        ev_flag = "ON" if c.ev_stop_enabled else "off"
        exp_flag = "ON" if c.near_expiry_exit else "off"
        print(
            f"{rank:<3} {ev_flag:>3} {exp_flag:>3} {c.ev_buffer:>7.2f} {c.ev_persistence_minutes:>7.0f}m "
            f"{c.hard_stop_probability:>6.2f} {c.drift_sigma_threshold:>5.1f}σ "
            f"${r.total_net_pnl:>+8.3f} ${r.total_gross_pnl:>+7.3f} "
            f"${r.total_fees:>6.3f} {r.win_rate:>5.1f}% "
            f"{r.winning_trades}/{r.losing_trades:>3} {r.whipsaw_count:>8} "
            f"{r.stop_loss_exits:>8} {r.avg_hold_days:>6.1f}d "
            f"${r.max_single_loss:>+7.3f} ${r.total_volume:>8.0f}"
        )
    print()


def print_position_details(result: VolumeBacktestResult):
    """Print per-position breakdown."""
    print()
    print("=" * 130)
    print("  POSITION DETAILS")
    print("=" * 130)
    print(
        f"{'Market':<35} {'Cat':<9} {'Entry':>6} {'Exit':>6} {'Res':>4} "
        f"{'Hold':>6} {'Size$':>7} {'Net PnL':>9} {'MaxDD':>7} {'MinP':>6} "
        f"{'Whip':>5} {'Exit Reason':<30}"
    )
    print("-" * 130)

    for p in result.positions:
        whip = "YES" if p.was_whipsawed else ""
        print(
            f"{p.market_name[:34]:<35} {p.category:<9} "
            f"{p.entry_price:>6.3f} {p.exit_price:>6.3f} {p.resolution:>4} "
            f"{p.hold_days:>5.1f}d ${p.size_usdc:>6.1f} "
            f"${p.net_pnl:>+8.4f} {p.max_drawdown_pct:>6.1%} "
            f"{p.min_price_seen:>6.3f} {whip:>5} {p.exit_reason[:29]:<30}"
        )
    print()


def print_exit_reason_analysis(result: VolumeBacktestResult):
    """Print summary of exit reasons."""
    print()
    print("  EXIT REASON ANALYSIS")
    print("-" * 40)
    reasons = result.exit_reason_summary()
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        pct = count / result.total_trades * 100
        # Average PnL for this exit reason
        pnls = [p.net_pnl for p in result.positions if (p.exit_reason or "resolved") == reason]
        avg_pnl = sum(pnls) / len(pnls) if pnls else 0
        print(f"  {reason[:30]:<32} {count:>3} ({pct:>5.1f}%)  avg PnL: ${avg_pnl:>+.4f}")
    print()


def print_parameter_sensitivity(results: list[VolumeBacktestResult]):
    """Analyze which parameters matter most."""
    print()
    print("=" * 70)
    print("  PARAMETER SENSITIVITY")
    print("=" * 70)

    params = [
        ("ev_stop_enabled", lambda c: c.ev_stop_enabled),
        ("near_expiry_exit", lambda c: c.near_expiry_exit),
        ("ev_buffer", lambda c: c.ev_buffer),
        ("ev_persistence_min", lambda c: c.ev_persistence_minutes),
        ("hard_stop_prob", lambda c: c.hard_stop_probability),
        ("drift_sigma", lambda c: c.drift_sigma_threshold),
    ]

    for name, getter in params:
        print(f"\n  {name}:")
        groups: dict[float, list[float]] = {}
        for r in results:
            val = getter(r.config)
            if val not in groups:
                groups[val] = []
            groups[val].append(r.total_net_pnl)

        for val in sorted(groups.keys()):
            pnls = groups[val]
            avg = sum(pnls) / len(pnls)
            best = max(pnls)
            worst = min(pnls)
            print(f"    {val:>8}: avg=${avg:>+.4f}  best=${best:>+.4f}  worst=${worst:>+.4f}  (n={len(pnls)})")
