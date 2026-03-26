"""WebSocket client for real-time Polymarket orderbook updates.

Replaces HTTP polling with streaming data for faster reaction
to price movements, reducing stale order risk.
"""

import json
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field

from loguru import logger

try:
    import websocket
    HAS_WEBSOCKET = True
except ImportError:
    HAS_WEBSOCKET = False


WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass
class LiveOrderBook:
    """Cached orderbook from WebSocket updates."""
    bids: list[dict] = field(default_factory=list)
    asks: list[dict] = field(default_factory=list)
    midpoint: float = 0.5
    last_update: float = 0.0


class PolymarketWebSocket:
    """WebSocket client for streaming orderbook and trade data.

    Falls back gracefully to HTTP polling if websocket-client
    is not installed or connection fails.
    """

    def __init__(self):
        if not HAS_WEBSOCKET:
            logger.warning(
                "websocket-client not installed. "
                "Install with: pip install websocket-client"
            )

        self._ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._subscribed_tokens: set[str] = set()
        self._orderbooks: dict[str, LiveOrderBook] = defaultdict(LiveOrderBook)
        self._trade_callbacks: list = []
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return HAS_WEBSOCKET and self._running

    def start(self):
        """Start WebSocket connection in background thread."""
        if not HAS_WEBSOCKET:
            logger.info("WebSocket unavailable, will use HTTP polling")
            return

        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("WebSocket client started")

    def stop(self):
        """Stop WebSocket connection."""
        self._running = False
        if self._ws:
            self._ws.close()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("WebSocket client stopped")

    def subscribe(self, token_id: str):
        """Subscribe to orderbook updates for a token."""
        self._subscribed_tokens.add(token_id)
        if self._ws and self._running:
            self._send_subscribe(token_id)

    def unsubscribe(self, token_id: str):
        """Unsubscribe from a token's updates."""
        self._subscribed_tokens.discard(token_id)
        if self._ws and self._running:
            msg = json.dumps({
                "type": "unsubscribe",
                "channel": "book",
                "assets_id": token_id,
            })
            try:
                self._ws.send(msg)
            except Exception:
                pass

    def get_midpoint(self, token_id: str) -> float | None:
        """Get cached midpoint, or None if no data."""
        with self._lock:
            ob = self._orderbooks.get(token_id)
            if ob and ob.last_update > 0:
                # Consider data stale after 30 seconds
                if time.time() - ob.last_update < 30:
                    return ob.midpoint
        return None

    def get_orderbook(self, token_id: str) -> LiveOrderBook | None:
        """Get cached orderbook, or None if stale/missing."""
        with self._lock:
            ob = self._orderbooks.get(token_id)
            if ob and ob.last_update > 0:
                if time.time() - ob.last_update < 30:
                    return ob
        return None

    def on_trade(self, callback):
        """Register callback for trade events: callback(token_id, side, price, size)."""
        self._trade_callbacks.append(callback)

    def _run(self):
        """WebSocket event loop with reconnection."""
        while self._running:
            try:
                self._ws = websocket.WebSocketApp(
                    WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                logger.error(f"WebSocket error: {e}")

            if self._running:
                logger.info("WebSocket reconnecting in 5s...")
                time.sleep(5)

    def _on_open(self, ws):
        logger.info("WebSocket connected")
        # Resubscribe to all tokens
        for token_id in self._subscribed_tokens:
            self._send_subscribe(token_id)

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            msg_type = data.get("type", "")

            if msg_type == "book":
                self._handle_book_update(data)
            elif msg_type == "trade":
                self._handle_trade(data)
        except Exception as e:
            logger.debug(f"WebSocket message parse error: {e}")

    def _on_error(self, ws, error):
        logger.warning(f"WebSocket error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        logger.info(f"WebSocket closed: {close_status_code} {close_msg}")

    def _send_subscribe(self, token_id: str):
        msg = json.dumps({
            "type": "subscribe",
            "channel": "book",
            "assets_id": token_id,
        })
        try:
            self._ws.send(msg)
            logger.debug(f"Subscribed to {token_id[:8]}...")
        except Exception as e:
            logger.debug(f"Subscribe failed: {e}")

    def _handle_book_update(self, data: dict):
        token_id = data.get("asset_id", "")
        if not token_id:
            return

        with self._lock:
            ob = self._orderbooks[token_id]
            ob.bids = data.get("bids", ob.bids)
            ob.asks = data.get("asks", ob.asks)
            ob.last_update = time.time()

            # Recalculate midpoint
            best_bid = float(ob.bids[0]["price"]) if ob.bids else 0.0
            best_ask = float(ob.asks[0]["price"]) if ob.asks else 1.0
            ob.midpoint = (best_bid + best_ask) / 2

    def _handle_trade(self, data: dict):
        token_id = data.get("asset_id", "")
        side = data.get("side", "")
        price = float(data.get("price", 0))
        size = float(data.get("size", 0))

        for cb in self._trade_callbacks:
            try:
                cb(token_id, side, price, size)
            except Exception as e:
                logger.debug(f"Trade callback error: {e}")
