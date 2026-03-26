"""PnL tracking and daily reporting.

Tracks all fills, rewards, and rebates to give a clear picture
of bot performance and whether the airdrop farming is net positive.
"""

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from loguru import logger


@dataclass
class Fill:
    """A single fill event."""
    timestamp: float
    token_id: str
    market_name: str
    side: str
    price: float
    size: float
    usdc_value: float


@dataclass
class DailyStats:
    """Aggregated daily statistics."""
    date: str
    fills_count: int = 0
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    estimated_spread_pnl: float = 0.0
    lp_rewards: float = 0.0
    maker_rebates: float = 0.0
    markets_active: int = 0
    uptime_hours: float = 0.0

    @property
    def total_pnl(self) -> float:
        return self.estimated_spread_pnl + self.lp_rewards + self.maker_rebates

    @property
    def total_volume(self) -> float:
        return self.buy_volume + self.sell_volume


class PnLTracker:
    """Tracks profit/loss and generates daily reports."""

    def __init__(self, data_dir: str = "data"):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)

        self.fills: list[Fill] = []
        self.daily_stats: dict[str, DailyStats] = {}
        self.bot_start_time: float = time.time()
        self._current_date = self._today()

        # Load existing data
        self._load_history()

    def record_fill(self, token_id: str, market_name: str, side: str,
                    price: float, size: float, midpoint: float):
        """Record a fill and estimate PnL impact."""
        usdc_value = price * size
        fill = Fill(
            timestamp=time.time(),
            token_id=token_id,
            market_name=market_name,
            side=side,
            price=price,
            size=size,
            usdc_value=usdc_value,
        )
        self.fills.append(fill)

        # Estimate spread PnL (how much we captured vs midpoint)
        if side == "BUY":
            spread_pnl = (midpoint - price) * size  # Bought below mid = profit
        else:
            spread_pnl = (price - midpoint) * size  # Sold above mid = profit

        # Update daily stats
        stats = self._get_today_stats()
        stats.fills_count += 1
        if side == "BUY":
            stats.buy_volume += usdc_value
        else:
            stats.sell_volume += usdc_value
        stats.estimated_spread_pnl += spread_pnl

        logger.info(
            f"Fill: {side} {size:.1f} shares @ {price:.4f} "
            f"(mid: {midpoint:.4f}) spread_pnl: ${spread_pnl:.4f}"
        )

    def record_lp_reward(self, amount: float):
        """Record daily LP reward payout."""
        stats = self._get_today_stats()
        stats.lp_rewards += amount
        logger.info(f"LP reward recorded: ${amount:.2f}")

    def record_maker_rebate(self, amount: float):
        """Record maker rebate payout."""
        stats = self._get_today_stats()
        stats.maker_rebates += amount

    def update_active_markets(self, count: int):
        """Update count of active markets."""
        stats = self._get_today_stats()
        stats.markets_active = max(stats.markets_active, count)

    def get_daily_report(self, date: str | None = None) -> str:
        """Generate a daily report string."""
        if date is None:
            date = self._today()

        stats = self.daily_stats.get(date)
        if not stats:
            return f"No data for {date}"

        uptime = (time.time() - self.bot_start_time) / 3600
        stats.uptime_hours = round(uptime, 1)

        report = f"""
╔══════════════════════════════════════════╗
║     LP Farming Bot - Daily Report        ║
║     {date}                         ║
╠══════════════════════════════════════════╣
║ Fills:           {stats.fills_count:>6}                 ║
║ Buy Volume:      ${stats.buy_volume:>10.2f}           ║
║ Sell Volume:     ${stats.sell_volume:>10.2f}           ║
║ Total Volume:    ${stats.total_volume:>10.2f}           ║
╠══════════════════════════════════════════╣
║ Spread PnL:     ${stats.estimated_spread_pnl:>+10.2f}           ║
║ LP Rewards:      ${stats.lp_rewards:>10.2f}           ║
║ Maker Rebates:   ${stats.maker_rebates:>10.2f}           ║
║ ─────────────────────────────────────    ║
║ TOTAL PnL:      ${stats.total_pnl:>+10.2f}           ║
╠══════════════════════════════════════════╣
║ Markets Active:  {stats.markets_active:>6}                 ║
║ Uptime (hrs):    {stats.uptime_hours:>6.1f}                 ║
╚══════════════════════════════════════════╝"""
        return report

    def get_cumulative_stats(self) -> dict:
        """Get all-time cumulative statistics."""
        total_fills = 0
        total_volume = 0.0
        total_spread_pnl = 0.0
        total_rewards = 0.0
        total_rebates = 0.0
        days_active = len(self.daily_stats)

        for stats in self.daily_stats.values():
            total_fills += stats.fills_count
            total_volume += stats.total_volume
            total_spread_pnl += stats.estimated_spread_pnl
            total_rewards += stats.lp_rewards
            total_rebates += stats.maker_rebates

        total_pnl = total_spread_pnl + total_rewards + total_rebates

        return {
            "days_active": days_active,
            "total_fills": total_fills,
            "total_volume": round(total_volume, 2),
            "total_spread_pnl": round(total_spread_pnl, 2),
            "total_lp_rewards": round(total_rewards, 2),
            "total_maker_rebates": round(total_rebates, 2),
            "total_pnl": round(total_pnl, 2),
            "avg_daily_pnl": round(total_pnl / max(days_active, 1), 2),
        }

    def save(self):
        """Persist tracking data to disk."""
        data = {
            "daily_stats": {},
            "bot_start_time": self.bot_start_time,
        }
        for date, stats in self.daily_stats.items():
            data["daily_stats"][date] = {
                "date": stats.date,
                "fills_count": stats.fills_count,
                "buy_volume": stats.buy_volume,
                "sell_volume": stats.sell_volume,
                "estimated_spread_pnl": stats.estimated_spread_pnl,
                "lp_rewards": stats.lp_rewards,
                "maker_rebates": stats.maker_rebates,
                "markets_active": stats.markets_active,
                "uptime_hours": stats.uptime_hours,
            }

        path = os.path.join(self.data_dir, "pnl_history.json")
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def _load_history(self):
        """Load historical data from disk."""
        path = os.path.join(self.data_dir, "pnl_history.json")
        if not os.path.exists(path):
            return

        try:
            with open(path) as f:
                data = json.load(f)

            for date, stats_dict in data.get("daily_stats", {}).items():
                self.daily_stats[date] = DailyStats(**stats_dict)

            logger.info(f"Loaded {len(self.daily_stats)} days of PnL history")
        except Exception as e:
            logger.warning(f"Failed to load PnL history: {e}")

    def _get_today_stats(self) -> DailyStats:
        today = self._today()
        if today != self._current_date:
            # Day rolled over - print yesterday's report and save
            yesterday = self._current_date
            if yesterday in self.daily_stats:
                logger.info(self.get_daily_report(yesterday))
            self.save()
            self._current_date = today

        if today not in self.daily_stats:
            self.daily_stats[today] = DailyStats(date=today)
        return self.daily_stats[today]

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
