"""Entry point for Polymarket LP Farming Bot.

Usage:
    python main.py              # Run in live mode
    python main.py --dry-run    # Dry run (no real orders)
    python main.py --report     # Show PnL report
"""

import argparse
import os
import sys

import yaml
from dotenv import load_dotenv
from loguru import logger

from src.bot import LPFarmingBot
from src.client import PolymarketClient
from src.pnl_tracker import PnLTracker


def setup_logging(debug: bool = False):
    """Configure loguru logger."""
    logger.remove()
    level = "DEBUG" if debug else "INFO"
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
        level=level,
    )
    os.makedirs("logs", exist_ok=True)
    logger.add(
        "logs/bot_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        retention="30 days",
        level="DEBUG",
    )


def load_config() -> dict:
    """Load configuration from YAML file."""
    config_path = os.path.join(os.path.dirname(__file__), "config", "config.yaml")
    with open(config_path) as f:
        return yaml.safe_load(f)


def show_report():
    """Display PnL report without running the bot."""
    tracker = PnLTracker()
    print(tracker.get_daily_report())
    print()
    cumulative = tracker.get_cumulative_stats()
    print("=== Cumulative Stats ===")
    for key, value in cumulative.items():
        label = key.replace("_", " ").title()
        print(f"  {label}: {value}")


def main():
    parser = argparse.ArgumentParser(description="Polymarket LP Farming Bot")
    parser.add_argument("--dry-run", action="store_true",
                        help="Calculate quotes without placing real orders")
    parser.add_argument("--report", action="store_true",
                        help="Show PnL report and exit")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    load_dotenv()
    setup_logging(debug=args.debug)

    # Report mode
    if args.report:
        show_report()
        return

    # Load config
    config = load_config()
    poly_cfg = config.get("polymarket", {})

    # Get private key
    private_key = os.getenv("PRIVATE_KEY")
    if not private_key:
        logger.error("PRIVATE_KEY not set in .env file. See .env.example")
        sys.exit(1)

    # Security checks before doing anything with the key
    from src.security import run_all_checks
    if not run_all_checks(private_key):
        logger.error("Security checks failed. Fix issues above before running.")
        sys.exit(1)

    # Initialize client
    client = PolymarketClient(
        host=poly_cfg.get("host", "https://clob.polymarket.com"),
        private_key=private_key,
        chain_id=poly_cfg.get("chain_id", 137),
        signature_type=poly_cfg.get("signature_type", 0),
    )

    # Authenticate
    try:
        client.authenticate()
    except Exception as e:
        logger.error(f"Authentication failed: {e}")
        logger.error("Check your PRIVATE_KEY and signature_type in config")
        sys.exit(1)

    # Wallet diagnostic
    wallet_addr = client.get_wallet_address()
    logger.info(f"Bot wallet address: {wallet_addr}")

    onchain_usdc = client.get_onchain_usdc_balance()
    native_usdc = client.get_onchain_native_usdc_balance()
    exchange_usdc = client.get_usdc_balance()
    matic = client.get_matic_balance()

    logger.info(f"On-chain USDC.e balance (Polymarket uses this): ${onchain_usdc:.2f}")
    logger.info(f"On-chain native USDC balance: ${native_usdc:.2f}")
    logger.info(f"Polymarket exchange USDC: ${exchange_usdc:.2f}")
    logger.info(f"MATIC/POL (gas): {matic:.4f}")

    if native_usdc > 1.0 and onchain_usdc < 1.0 and exchange_usdc < 1.0:
        logger.error(
            f"You have ${native_usdc:.2f} native USDC, but Polymarket uses USDC.e! "
            "You need to swap native USDC → USDC.e on a DEX (e.g. QuickSwap), "
            "or deposit native USDC through the Polymarket website which handles conversion."
        )
        sys.exit(1)

    if exchange_usdc < 1.0 and onchain_usdc > 1.0:
        logger.warning(
            "You have USDC.e in your wallet but NOT on Polymarket exchange! "
            "Go to https://polymarket.com and deposit USDC first, "
            "or the bot cannot place orders."
        )
        sys.exit(1)

    if exchange_usdc < 1.0 and onchain_usdc < 1.0 and native_usdc < 1.0:
        logger.error(
            f"No USDC found! Wallet {wallet_addr} has $0. "
            "Please transfer USDC (Polygon) to this address."
        )
        sys.exit(1)

    if matic < 0.001:
        logger.warning("Very low MATIC balance — may not have enough for gas fees")

    # Create and run bot
    bot = LPFarmingBot(client=client, config=config, dry_run=args.dry_run)
    bot.run()


if __name__ == "__main__":
    main()
