"""Parameter optimizer - grid search across strategy parameters.

Tests combinations of spread, order size, levels, and risk limits
to find the optimal settings for airdrop farming (maximize LP rewards
while keeping losses minimal).
"""

from dataclasses import dataclass

from backtest.data_generator import MarketHistory, generate_all_markets
from backtest.engine import BacktestConfig, BacktestResult, run_backtest, reset_fill_counter


@dataclass
class OptimizationResult:
    """Results for one parameter combination across all markets."""
    config: BacktestConfig
    market_results: list[BacktestResult]

    @property
    def total_pnl(self) -> float:
        return sum(r.realized_pnl for r in self.market_results)

    @property
    def total_lp_rewards(self) -> float:
        return sum(r.total_lp_rewards for r in self.market_results)

    @property
    def total_maker_rebates(self) -> float:
        return sum(r.total_maker_rebates for r in self.market_results)

    @property
    def total_spread_pnl(self) -> float:
        return sum(r.total_spread_pnl for r in self.market_results)

    @property
    def total_volume(self) -> float:
        return sum(r.total_volume for r in self.market_results)

    @property
    def total_fills(self) -> int:
        return sum(r.total_fills for r in self.market_results)

    @property
    def avg_sharpe(self) -> float:
        sharpes = [r.sharpe_ratio for r in self.market_results if r.daily_results]
        return sum(sharpes) / len(sharpes) if sharpes else 0.0

    @property
    def worst_drawdown(self) -> float:
        return max((r.max_drawdown for r in self.market_results), default=0.0)

    @property
    def avg_win_rate(self) -> float:
        rates = [r.win_rate for r in self.market_results if r.daily_results]
        return sum(rates) / len(rates) if rates else 0.0

    @property
    def realized_pnl(self) -> float:
        return sum(r.realized_pnl for r in self.market_results)

    @property
    def avg_daily_pnl(self) -> float:
        total_days = sum(len(r.daily_results) for r in self.market_results)
        if total_days == 0:
            return 0.0
        return self.realized_pnl / total_days


# Parameter grid for optimization
PARAM_GRID = {
    "spread_bps": [150, 200, 300, 400, 500],
    "order_size_usdc": [20, 30, 50],
    "order_levels": [1, 2, 3],
    "daily_loss_limit": [10, 20, 50],
}

# Reduced grid for faster runs
PARAM_GRID_FAST = {
    "spread_bps": [200, 300, 400],
    "order_size_usdc": [20, 30, 50],
    "order_levels": [1, 2],
    "daily_loss_limit": [15, 30],
}


def generate_configs(grid: dict | None = None) -> list[BacktestConfig]:
    """Generate all parameter combinations from grid."""
    if grid is None:
        grid = PARAM_GRID_FAST

    configs = []
    for spread in grid["spread_bps"]:
        for size in grid["order_size_usdc"]:
            for levels in grid["order_levels"]:
                for loss_limit in grid["daily_loss_limit"]:
                    configs.append(BacktestConfig(
                        spread_bps=spread,
                        order_size_usdc=size,
                        order_levels=levels,
                        daily_loss_limit=loss_limit,
                    ))
    return configs


def run_optimization(
    markets: list[MarketHistory] | None = None,
    grid: dict | None = None,
    verbose: bool = True,
) -> list[OptimizationResult]:
    """Run grid search optimization.

    Returns results sorted by total PnL (best first).
    """
    if markets is None:
        markets = generate_all_markets()

    configs = generate_configs(grid)

    if verbose:
        print(f"Running {len(configs)} parameter combos x {len(markets)} markets "
              f"= {len(configs) * len(markets)} backtests")
        print()

    all_results = []

    for i, config in enumerate(configs):
        reset_fill_counter()
        market_results = []

        for market in markets:
            result = run_backtest(market, config)
            market_results.append(result)

        opt_result = OptimizationResult(config=config, market_results=market_results)
        all_results.append(opt_result)

        if verbose and (i + 1) % 10 == 0:
            print(f"  Progress: {i + 1}/{len(configs)} combos tested...")

    # Sort by total PnL
    all_results.sort(key=lambda r: r.realized_pnl, reverse=True)

    return all_results


def print_top_results(results: list[OptimizationResult], top_n: int = 10):
    """Print the top parameter combinations."""
    print()
    print("=" * 100)
    print(f"  TOP {top_n} PARAMETER COMBINATIONS (sorted by total PnL)")
    print("=" * 100)
    print()
    print(f"{'Rank':<5} {'Spread':>7} {'Size':>6} {'Lvls':>5} {'Loss$':>6} "
          f"{'Total PnL':>10} {'LP Rwds':>8} {'Rebates':>8} {'Spread$':>8} "
          f"{'Volume':>10} {'Fills':>6} {'Sharpe':>7} {'WinRate':>8} {'MaxDD':>7}")
    print("-" * 100)

    for rank, r in enumerate(results[:top_n], 1):
        c = r.config
        print(
            f"{rank:<5} {c.spread_bps:>5}bp {c.order_size_usdc:>5.0f} "
            f"{c.order_levels:>5} {c.daily_loss_limit:>5.0f} "
            f"${r.realized_pnl:>+9.2f} ${r.total_lp_rewards:>7.2f} "
            f"${r.total_maker_rebates:>7.2f} ${r.total_spread_pnl:>+7.2f} "
            f"${r.total_volume:>9.0f} {r.total_fills:>6} "
            f"{r.avg_sharpe:>+7.2f} {r.avg_win_rate:>6.1f}% "
            f"${r.worst_drawdown:>6.2f}"
        )

    print()
    print("=" * 100)


def print_worst_results(results: list[OptimizationResult], bottom_n: int = 5):
    """Print the worst parameter combinations (what to avoid)."""
    print()
    print(f"  WORST {bottom_n} (what to avoid)")
    print("-" * 100)

    for rank, r in enumerate(results[-bottom_n:], 1):
        c = r.config
        print(
            f"{rank:<5} {c.spread_bps:>5}bp {c.order_size_usdc:>5.0f} "
            f"{c.order_levels:>5} {c.daily_loss_limit:>5.0f} "
            f"${r.realized_pnl:>+9.2f} ${r.total_lp_rewards:>7.2f} "
            f"${r.total_maker_rebates:>7.2f} ${r.total_spread_pnl:>+7.2f} "
            f"${r.total_volume:>9.0f} {r.total_fills:>6} "
            f"{r.avg_sharpe:>+7.2f} {r.avg_win_rate:>6.1f}% "
            f"${r.worst_drawdown:>6.2f}"
        )
    print()


def print_market_breakdown(result: OptimizationResult):
    """Print per-market breakdown for a single parameter combo."""
    c = result.config
    print()
    print(f"Market Breakdown for: spread={c.spread_bps}bp, size=${c.order_size_usdc}, "
          f"levels={c.order_levels}, loss_limit=${c.daily_loss_limit}")
    print("-" * 95)
    print(f"{'Market':<30} {'Cat':<10} {'Days':>5} {'PnL':>9} {'LP$':>7} "
          f"{'Rebate':>7} {'Spread$':>8} {'Fills':>6} {'Win%':>6} {'MaxDD':>7}")
    print("-" * 95)

    for r in result.market_results:
        print(
            f"{r.market_name[:29]:<30} {r.category:<10} {r.duration_days:>5} "
            f"${r.realized_pnl:>+8.2f} ${r.total_lp_rewards:>6.2f} "
            f"${r.total_maker_rebates:>6.2f} ${r.total_spread_pnl:>+7.2f} "
            f"{r.total_fills:>6} {r.win_rate:>5.1f}% ${r.max_drawdown:>6.2f}"
        )
    print()


def print_parameter_sensitivity(results: list[OptimizationResult]):
    """Analyze which parameters matter most."""
    print()
    print("=" * 70)
    print("  PARAMETER SENSITIVITY ANALYSIS")
    print("=" * 70)

    # Group by each parameter
    for param_name in ["spread_bps", "order_size_usdc", "order_levels", "daily_loss_limit"]:
        print(f"\n  {param_name}:")
        groups: dict[float, list[float]] = {}

        for r in results:
            val = getattr(r.config, param_name)
            if val not in groups:
                groups[val] = []
            groups[val].append(r.realized_pnl)

        for val in sorted(groups.keys()):
            pnls = groups[val]
            avg = sum(pnls) / len(pnls)
            best = max(pnls)
            worst = min(pnls)
            print(f"    {val:>8}: avg=${avg:>+8.2f}  best=${best:>+8.2f}  worst=${worst:>+8.2f}  (n={len(pnls)})")
