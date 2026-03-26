# Polymarket LP Farming Bot

Conservative market-making bot for Polymarket, designed to maximize POLY airdrop eligibility while minimizing trading losses.

## Strategy

The bot operates as a liquidity provider on Polymarket's CLOB (Central Limit Order Book):

- **Wide spreads (300 bps)** — reduces adverse selection risk
- **Small order sizes ($30/order)** — limits exposure per trade
- **Always two-sided** — bid + ask for LP reward bonus
- **Multi-market (3-5 markets)** — diversifies risk, shows broad participation
- **Fee-aware** — accounts for taker fees and maker rebates by category
- **Auto market selection** — scores markets by reward density, rebate tier, safety

### Why LP for Airdrops?

- Polymarket confirmed POLY token airdrop (5-10% of supply)
- LP activity is a strong differentiator — most users only trade
- Daily LP rewards (USDC) help cover costs
- Maker rebates (25-50% of taker fees) add extra income
- Consistent activity over time is a key eligibility signal

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env and set your PRIVATE_KEY

# 3. Adjust strategy parameters (optional)
# Edit config/config.yaml

# 4. Test with dry run (no real orders)
python main.py --dry-run

# 5. Run live
python main.py

# 6. Check PnL report
python main.py --report
```

## Configuration

### Environment Variables (`.env`)

```
PRIVATE_KEY=your_wallet_private_key_without_0x
```

### Strategy Parameters (`config/config.yaml`)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `spread_bps` | 300 | Spread width in basis points (3%) |
| `order_size_usdc` | 30 | USDC per order layer |
| `order_levels` | 2 | Layers per side (bid/ask) |
| `refresh_interval_sec` | 15 | Quote refresh interval |

### Risk Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_position_per_market` | 200 | Max exposure per market (USDC) |
| `max_total_position` | 800 | Max total exposure (USDC) |
| `daily_loss_limit` | 20 | Stop if daily loss exceeds this |
| `min_probability` | 0.10 | Skip extreme low probability markets |
| `max_probability` | 0.90 | Skip extreme high probability markets |
| `min_days_to_expiry` | 5 | Skip markets expiring soon |

## Architecture

```
src/
├── client.py           # py-clob-client wrapper (auth + retry)
├── strategy.py         # Fee-aware conservative quoting
├── market_selector.py  # Market scoring (rewards, rebates, safety)
├── risk_manager.py     # Position limits, daily loss cap, skew control
├── order_manager.py    # Cancel-replace order lifecycle
├── pnl_tracker.py      # PnL tracking and daily reports
├── websocket_client.py # Real-time orderbook via WebSocket
└── bot.py              # Main loop orchestrating everything
```

## Commands

```bash
python main.py              # Run live
python main.py --dry-run    # Simulate without placing orders
python main.py --report     # Show PnL report
python main.py --debug      # Verbose logging
```

## Fee Structure (as of March 2026)

| Category | Max Taker Fee | Maker Rebate |
|----------|--------------|--------------|
| Finance | 1.0% | **50%** |
| Politics | 1.0% | 25% |
| Crypto | 1.8% | 25% |
| Sports | 0.44% | 25% |
| Geopolitics | 0% | N/A |

The bot prioritizes Finance markets for their 2x rebate advantage.

## Risk Disclaimer

This bot is for educational and research purposes. Trading on prediction markets involves risk of loss. Never risk more than you can afford to lose. The airdrop is not guaranteed.
