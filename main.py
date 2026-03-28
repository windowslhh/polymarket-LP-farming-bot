"""Entry point for Polymarket LP Farming Bot.

Usage:
    python main.py                      # Run LP farming (default)
    python main.py --dry-run            # Dry run (no real orders)
    python main.py --volume-farm        # Run volume farming only
    python main.py --volume-farm --dry-run  # Volume farming dry run
    python main.py --report             # Show PnL report
"""

import argparse
import os
import sys
import threading

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
    parser.add_argument("--volume-farm", action="store_true",
                        help="Run volume farming strategy (buy high-prob outcomes)")
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
    funder = os.getenv("FUNDER_ADDRESS")
    signature_type = poly_cfg.get("signature_type", 0)
    if funder:
        logger.info(f"Using funder (proxy wallet): {funder}")
    client = PolymarketClient(
        host=poly_cfg.get("host", "https://clob.polymarket.com"),
        private_key=private_key,
        chain_id=poly_cfg.get("chain_id", 137),
        signature_type=signature_type,
        funder=funder,
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
    funder = os.getenv("FUNDER_ADDRESS")
    proxy_mode = bool(funder)

    logger.info(f"Bot signing wallet: {wallet_addr}")
    if proxy_mode:
        logger.info(f"Proxy wallet (funder): {funder}")

    exchange_usdc = client.get_usdc_balance()
    matic = client.get_matic_balance()
    logger.info(f"Polymarket exchange USDC: ${exchange_usdc:.2f}")
    logger.info(f"MATIC/POL (gas): {matic:.4f}")

    if not proxy_mode:
        # EOA mode: funds must be on-chain in the signing wallet
        onchain_usdc = client.get_onchain_usdc_balance()
        native_usdc = client.get_onchain_native_usdc_balance()
        logger.info(f"On-chain USDC.e: ${onchain_usdc:.2f}")
        logger.info(f"On-chain native USDC: ${native_usdc:.2f}")

        if native_usdc > 1.0 and onchain_usdc < 1.0 and exchange_usdc < 1.0:
            logger.error(
                f"You have ${native_usdc:.2f} native USDC, but Polymarket uses USDC.e! "
                "Swap on QuickSwap or deposit through polymarket.com."
            )
            sys.exit(1)
        if exchange_usdc < 1.0 and onchain_usdc > 1.0:
            logger.warning(
                "USDC.e is in your wallet but not on Polymarket exchange. "
                "Deposit at polymarket.com first."
            )
            sys.exit(1)
        if exchange_usdc < 1.0 and onchain_usdc < 1.0 and native_usdc < 1.0:
            logger.error(f"No USDC found in wallet {wallet_addr}. Transfer USDC (Polygon) first.")
            sys.exit(1)
    else:
        # Proxy mode: funds are in the proxy wallet, EOA on-chain balance is irrelevant
        if exchange_usdc < 1.0:
            logger.error(
                f"Polymarket exchange shows $0. Proxy wallet: {funder}\n"
                "Check that you deposited USDC at polymarket.com with this wallet."
            )
            sys.exit(1)

    if matic < 0.001:
        logger.warning("Very low MATIC/POL balance — gas fees may fail")

    # Determine capital allocation
    vf_cfg = config.get("volume_farming", {})
    cap_cfg = config.get("capital_allocation", {})
    volume_farm_enabled = args.volume_farm or vf_cfg.get("enabled", False)

    if volume_farm_enabled:
        from src.volume_bot import VolumeFarmingBot

        vf_capital_pct = cap_cfg.get("volume_farming_pct", 0.10)
        vf_capital = exchange_usdc * vf_capital_pct

        if args.volume_farm and not vf_cfg.get("enabled", False):
            # --volume-farm flag: run volume farming only
            logger.info(f"Volume farming mode: capital ${vf_capital:.0f}")
            vf_bot = VolumeFarmingBot(
                client=client, config=config,
                dry_run=args.dry_run, capital=vf_capital,
            )
            vf_bot.run()
            return

        # Both enabled: LP in main thread, volume farming in background
        logger.info(
            f"Parallel mode: LP ${exchange_usdc * cap_cfg.get('lp_farming_pct', 0.80):.0f} "
            f"+ Volume ${vf_capital:.0f}"
        )
        vf_bot = VolumeFarmingBot(
            client=client, config=config,
            dry_run=args.dry_run, capital=vf_capital,
        )
        vf_thread = threading.Thread(target=vf_bot.run, daemon=True, name="volume-farm")
        vf_thread.start()

    # Create and run LP bot (default, or parallel with volume farming)
    bot = LPFarmingBot(client=client, config=config, dry_run=args.dry_run)
    bot.run()


if __name__ == "__main__":
    main()
