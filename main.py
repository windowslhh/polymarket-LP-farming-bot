"""Entry point for Polymarket LP Farming Bot.

Usage:
    1. Copy .env.example to .env and set your PRIVATE_KEY
    2. Adjust config/config.yaml to your preferences
    3. Run: python main.py
"""

import os
import sys

import yaml
from dotenv import load_dotenv
from loguru import logger

from src.bot import LPFarmingBot
from src.client import PolymarketClient


def setup_logging():
    """Configure loguru logger."""
    logger.remove()  # Remove default handler
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
        level="INFO",
    )
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


def main():
    load_dotenv()
    setup_logging()

    # Load config
    config = load_config()
    poly_cfg = config.get("polymarket", {})

    # Get private key
    private_key = os.getenv("PRIVATE_KEY")
    if not private_key:
        logger.error("PRIVATE_KEY not set in .env file. See .env.example")
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

    # Create and run bot
    bot = LPFarmingBot(client=client, config=config)
    bot.run()


if __name__ == "__main__":
    main()
