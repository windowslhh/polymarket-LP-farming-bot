"""Polymarket CLOB client wrapper.

Wraps py-clob-client with retry logic, error handling,
and a simplified interface for the bot.
"""

import time
from dataclasses import dataclass

from loguru import logger
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.constants import POLYGON

# Polygon mainnet contract addresses
_CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
_USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
_POLYGON_RPC = "https://polygon-rpc.com"
_USDC_DECIMALS = 6  # USDC and CTF conditional tokens both use 6 decimals

# Minimal ABI for CTF split/merge and ERC20 approve
_CTF_ABI = [
    {
        "name": "splitPosition",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "partition", "type": "uint256[]"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "name": "mergePositions",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "partition", "type": "uint256[]"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [],
    },
]

_ERC20_ABI = [
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"type": "bool"}],
    },
    {
        "name": "allowance",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"type": "uint256"}],
    },
]


@dataclass
class MarketInfo:
    """Simplified market data."""

    token_id: str
    condition_id: str
    question: str
    outcome: str  # "Yes" or "No"
    active: bool
    end_date_iso: str
    min_tick_size: float
    # Complementary token for the other side
    complement_token_id: str | None = None


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    midpoint: float


class PolymarketClient:
    """Wrapper around py-clob-client with retry and error handling."""

    MAX_RETRIES = 3
    RETRY_DELAY = 2  # seconds, doubles each retry

    def __init__(self, host: str, private_key: str, chain_id: int = POLYGON,
                 signature_type: int = 0, funder: str | None = None):
        self.host = host
        self._private_key = private_key  # kept in memory for on-chain txns only
        self._client = ClobClient(
            host,
            key=private_key,
            chain_id=chain_id,
            signature_type=signature_type,
            funder=funder,
        )
        self._api_creds = None
        self._w3 = None  # lazy-init web3 only when needed

    def authenticate(self):
        """Derive L2 API credentials from private key."""
        logger.info("Deriving API credentials...")
        self._api_creds = self._client.create_or_derive_api_creds()
        self._client.set_api_creds(self._api_creds)
        logger.info("API credentials set successfully")

    def _retry(self, func, *args, **kwargs):
        """Execute with exponential backoff retry."""
        last_error = None
        for attempt in range(self.MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_error = e
                if attempt < self.MAX_RETRIES - 1:
                    delay = self.RETRY_DELAY * (2 ** attempt)
                    logger.warning(
                        f"Request failed (attempt {attempt + 1}/{self.MAX_RETRIES}): {e}. "
                        f"Retrying in {delay}s..."
                    )
                    time.sleep(delay)
        raise last_error

    # -- Market data (no auth required) --

    def get_markets(self, max_pages: int = 5) -> list[dict]:
        """Get all available markets with pagination."""
        return self._paginate(self._client.get_markets, max_pages)

    def get_simplified_markets(self, max_pages: int = 5) -> list[dict]:
        """Get simplified market listing with pagination."""
        return self._paginate(self._client.get_simplified_markets, max_pages)

    def get_sampling_simplified_markets(self, max_pages: int = 3) -> list[dict]:
        """Get sampling/rewards-eligible simplified markets."""
        return self._paginate(self._client.get_sampling_simplified_markets, max_pages)

    def get_sampling_markets(self, max_pages: int = 3) -> list[dict]:
        """Get sampling/rewards-eligible markets (full detail)."""
        return self._paginate(self._client.get_sampling_markets, max_pages)

    def _paginate(self, api_func, max_pages: int = 5) -> list[dict]:
        """Fetch paginated results from a CLOB API endpoint."""
        all_data = []
        cursor = "MA=="
        for page in range(max_pages):
            result = self._retry(api_func, cursor)
            if isinstance(result, list):
                all_data.extend(result)
                break  # No pagination info
            data = result.get("data", [])
            if not data:
                break
            all_data.extend(data)
            cursor = result.get("next_cursor", "")
            if not cursor or cursor == "MA==":
                break
            logger.debug(f"Fetched page {page + 1}, {len(data)} markets (total: {len(all_data)})")
        return all_data

    def get_orderbook(self, token_id: str) -> OrderBook:
        """Get order book for a token."""
        raw = self._retry(self._client.get_order_book, token_id)
        bids = [OrderBookLevel(float(b["price"]), float(b["size"])) for b in raw.get("bids", [])]
        asks = [OrderBookLevel(float(a["price"]), float(a["size"])) for a in raw.get("asks", [])]

        # Calculate midpoint
        best_bid = bids[0].price if bids else 0.0
        best_ask = asks[0].price if asks else 1.0
        midpoint = (best_bid + best_ask) / 2

        return OrderBook(bids=bids, asks=asks, midpoint=midpoint)

    def get_midpoint(self, token_id: str) -> float:
        """Get midpoint price for a token."""
        result = self._retry(self._client.get_midpoint, token_id)
        if isinstance(result, dict):
            return float(result.get("mid", 0.5))
        return float(result)

    def get_price(self, token_id: str, side: str = "buy") -> float:
        """Get current price on one side."""
        result = self._retry(self._client.get_price, token_id, side)
        if isinstance(result, dict):
            return float(result.get("price", 0.5))
        return float(result)

    # -- Trading (auth required) --

    def place_limit_order(self, token_id: str, price: float, size: float,
                          side: str) -> dict:
        """Place a limit order. side: 'BUY' or 'SELL'."""
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
        )
        signed_order = self._retry(self._client.create_order, order_args)
        result = self._retry(self._client.post_order, signed_order, OrderType.GTC)
        logger.debug(f"Placed {side} order: {size} @ {price} on {token_id[:8]}...")
        return result

    def cancel_order(self, order_id: str) -> dict:
        """Cancel a single order."""
        result = self._retry(self._client.cancel, order_id)
        logger.debug(f"Cancelled order {order_id}")
        return result

    def cancel_all_orders(self) -> dict:
        """Cancel all open orders."""
        result = self._retry(self._client.cancel_all)
        logger.info("Cancelled all orders")
        return result

    def get_open_orders(self, market: str | None = None) -> list[dict]:
        """Get open orders, optionally filtered by market."""
        params = {}
        if market:
            params["market"] = market
        return self._retry(self._client.get_orders, params)

    def get_trades(self) -> list[dict]:
        """Get trade history."""
        return self._retry(self._client.get_trades)

    # -- Token balances and on-chain operations --

    def get_usdc_balance(self) -> float:
        """Get available USDC balance on Polymarket exchange (in dollars)."""
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            result = self._retry(self._client.get_balance_allowance, params)
            raw = int(result.get("balance", 0))
            return raw / 10 ** _USDC_DECIMALS
        except Exception as e:
            logger.warning(f"Could not fetch USDC balance: {e}")
            return 0.0

    def get_onchain_usdc_balance(self) -> float:
        """Get on-chain USDC balance in the wallet (not Polymarket exchange)."""
        try:
            w3 = self._get_w3()
            wallet = self._get_wallet_address()
            usdc = w3.eth.contract(
                address=w3.to_checksum_address(_USDC_ADDRESS),
                abi=_ERC20_ABI + [
                    {
                        "name": "balanceOf",
                        "type": "function",
                        "stateMutability": "view",
                        "inputs": [{"name": "account", "type": "address"}],
                        "outputs": [{"type": "uint256"}],
                    }
                ],
            )
            raw = usdc.functions.balanceOf(wallet).call()
            return raw / 10 ** _USDC_DECIMALS
        except Exception as e:
            logger.warning(f"Could not fetch on-chain USDC balance: {e}")
            return 0.0

    def get_matic_balance(self) -> float:
        """Get native MATIC/POL balance for gas fees."""
        try:
            w3 = self._get_w3()
            wallet = self._get_wallet_address()
            raw = w3.eth.get_balance(wallet)
            return raw / 10 ** 18
        except Exception as e:
            logger.warning(f"Could not fetch MATIC balance: {e}")
            return 0.0

    def get_wallet_address(self) -> str:
        """Public method to get the wallet address."""
        return self._get_wallet_address()

    def get_conditional_balance(self, token_id: str) -> float:
        """Get YES/NO conditional token balance (in shares)."""
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
            )
            result = self._retry(self._client.get_balance_allowance, params)
            raw = int(result.get("balance", 0))
            return raw / 10 ** _USDC_DECIMALS
        except Exception as e:
            logger.warning(f"Could not fetch conditional balance for {token_id[:8]}...: {e}")
            return 0.0

    def _get_w3(self):
        """Lazy-init Web3 connection."""
        if self._w3 is None:
            from web3 import Web3
            self._w3 = Web3(Web3.HTTPProvider(_POLYGON_RPC))
        return self._w3

    def _get_wallet_address(self) -> str:
        """Derive wallet address from private key."""
        w3 = self._get_w3()
        account = w3.eth.account.from_key(self._private_key)
        return account.address

    def split_position(self, condition_id: str, amount_usdc: float) -> bool:
        """Split USDC into equal YES + NO tokens.

        1 USDC → 1 YES token + 1 NO token
        Requires USDC allowance for CTF contract.

        Args:
            condition_id: The market's condition ID (hex string).
            amount_usdc: Amount of USDC to split (in dollars).

        Returns:
            True on success.
        """
        if amount_usdc <= 0:
            return True

        w3 = self._get_w3()
        wallet = self._get_wallet_address()
        amount_raw = int(amount_usdc * 10 ** _USDC_DECIMALS)

        usdc = w3.eth.contract(
            address=w3.to_checksum_address(_USDC_ADDRESS),
            abi=_ERC20_ABI,
        )
        ctf = w3.eth.contract(
            address=w3.to_checksum_address(_CTF_ADDRESS),
            abi=_CTF_ABI,
        )

        # Ensure CTF has USDC allowance
        allowance = usdc.functions.allowance(wallet, _CTF_ADDRESS).call()
        if allowance < amount_raw:
            logger.info(f"Approving CTF to spend ${amount_usdc:.2f} USDC...")
            approve_tx = usdc.functions.approve(
                w3.to_checksum_address(_CTF_ADDRESS),
                2 ** 256 - 1,  # max approval
            ).build_transaction({
                "from": wallet,
                "nonce": w3.eth.get_transaction_count(wallet),
                "gas": 100_000,
                "gasPrice": w3.eth.gas_price,
            })
            signed = w3.eth.account.sign_transaction(approve_tx, self._private_key)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            logger.info("USDC approval confirmed")

        # condition_id as bytes32
        cid_bytes = bytes.fromhex(condition_id.replace("0x", "").zfill(64))

        logger.info(f"Splitting ${amount_usdc:.2f} USDC into YES+NO tokens for {condition_id[:10]}...")
        split_tx = ctf.functions.splitPosition(
            w3.to_checksum_address(_USDC_ADDRESS),  # collateral
            b"\x00" * 32,                            # parentCollectionId = bytes32(0)
            cid_bytes,                               # conditionId
            [1, 2],                                  # binary partition: [YES, NO]
            amount_raw,
        ).build_transaction({
            "from": wallet,
            "nonce": w3.eth.get_transaction_count(wallet),
            "gas": 300_000,
            "gasPrice": w3.eth.gas_price,
        })
        signed = w3.eth.account.sign_transaction(split_tx, self._private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        if receipt.status == 1:
            logger.info(f"Split confirmed: ${amount_usdc:.2f} USDC → {amount_usdc:.2f} YES + {amount_usdc:.2f} NO")
            return True
        else:
            logger.error(f"Split transaction failed: {tx_hash.hex()}")
            return False

    def merge_positions(self, condition_id: str, amount: float) -> bool:
        """Merge equal YES + NO tokens back into USDC.

        1 YES + 1 NO → 1 USDC
        Used to clean up inventory when exiting a market.

        Args:
            condition_id: The market's condition ID.
            amount: Number of token pairs to merge (= USDC recovered).

        Returns:
            True on success.
        """
        if amount <= 0:
            return True

        w3 = self._get_w3()
        wallet = self._get_wallet_address()
        amount_raw = int(amount * 10 ** _USDC_DECIMALS)
        cid_bytes = bytes.fromhex(condition_id.replace("0x", "").zfill(64))

        ctf = w3.eth.contract(
            address=w3.to_checksum_address(_CTF_ADDRESS),
            abi=_CTF_ABI,
        )

        logger.info(f"Merging {amount:.2f} YES+NO → ${amount:.2f} USDC for {condition_id[:10]}...")
        merge_tx = ctf.functions.mergePositions(
            w3.to_checksum_address(_USDC_ADDRESS),
            b"\x00" * 32,
            cid_bytes,
            [1, 2],
            amount_raw,
        ).build_transaction({
            "from": wallet,
            "nonce": w3.eth.get_transaction_count(wallet),
            "gas": 300_000,
            "gasPrice": w3.eth.gas_price,
        })
        signed = w3.eth.account.sign_transaction(merge_tx, self._private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        if receipt.status == 1:
            logger.info(f"Merge confirmed: {amount:.2f} YES+NO → ${amount:.2f} USDC")
            return True
        else:
            logger.error(f"Merge transaction failed: {tx_hash.hex()}")
            return False

    # -- Utilities --

    def check_connection(self) -> bool:
        """Verify API connection."""
        try:
            result = self._client.get_ok()
            ok = result == "OK" or (isinstance(result, dict) and result.get("status") == "OK")
            if ok:
                logger.info("Connected to Polymarket CLOB")
            return ok
        except Exception as e:
            logger.error(f"Connection check failed: {e}")
            return False
