"""
Read-only ticker feed (Valkey hashes `sym_<SYMBOL>`).
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict
import redis
import time

from .logging_config import logger

@dataclass
class Market:
    """
    Minimal reader for price hashes named ``sym_<PAIR>``.

    The hash *must* contain at least:

        {"price": "...", "timestamp": "..."}

    Optional fields `bid`, `ask`, `bid_volume`, `ask_volume`
    default to sensible fall-backs.
    """

    conn: redis.Redis

    # Public API ---------------------------------------------------------
    @property
    def tickers(self) -> list[str]:
        """
        Return a list of all known tickers.

        This is a list of strings, e.g. ``["BTC/USDT", "ETH/USDT"]``.
        """
        return [k[4:] for k in self.conn.scan_iter("sym_*")]

    def fetch_ticker(self, ticker: str) -> Dict[str, Any] | None:
        """
        Return a *ccxt-ish* ticker – just the keys our engine needs.

        :raises ValueError: if the hash is missing
        :raises RuntimeError: if mandatory fields cannot be parsed
        """
        h = self.conn.hgetall(f"sym_{ticker}")
        if not h:
            return None                            # ticker vanished – treat as absent
        try:
            price = float(h["price"])
            ts    = float(h["timestamp"])
        except (KeyError, ValueError):
            # Just log once and skip this ticker
            logger.warning("Malformed ticker blob for %s: %s", ticker, h)
            return None
        return {
            "symbol": ticker,
            "last":   price,
            "timestamp": ts,
            "bid":  float(h.get("bid", price)),
            "ask":  float(h.get("ask", price)),
            "bid_volume": float(h.get("bidVolume", 0.0)),
            "ask_volume": float(h.get("askVolume", 0.0)),
            "info": h,
        }
    
    def last_price(self, symbol: str) -> float:
        """
        Return the last price of the ticker.

        :raises RuntimeError: if the ticker is not available
        """
        ticker = self.fetch_ticker(symbol)
        if ticker is None:
            raise RuntimeError(f"Ticker for {symbol} not available")
        return ticker["last"]
    
    def set_last_price(self, symbol: str, price: float, ts: float | None = None, bid: float | None = None, ask: float | None = None,
                       bid_volume: float | None = None, ask_volume: float | None = None) -> None:
        """
        Set the last price of the ticker.

        :param symbol: The ticker symbol, e.g. "BTC/USDT".
        :param price: The last price to set.
        :param ts: Optional timestamp in seconds since epoch; if not provided, current time is used.
        :param bid: Optional bid price; if not provided, it will be set to the same value as `price`.
        :param ask: Optional ask price; if not provided, it will be set to the same value as `price`.
        :param bid_volume: Optional bid volume; if not provided, it defaults to 0.0.
        :param ask_volume: Optional ask volume; if not provided, it defaults to 0.0.

        This method is only for testing purposes and should not be used in production.
        """
        if ts is None:
            ts = time.time() / 1000  # current time in seconds since epoch
        if bid is None:
            bid = price
        if ask is None:
            ask = price
        if bid_volume is None:
            bid_volume = 0.0
        if ask_volume is None:
            ask_volume = 0.0
        fields = {
            "symbol": symbol,
            "price": price,
            "timestamp": ts,
            "bid": bid,
            "ask": ask,
            "bidVolume": bid_volume,
            "askVolume": ask_volume,
        }

        # Redis won’t store None, so drop them first.
        clean = {k: v for k, v in fields.items() if v is not None}

        self.conn.hset(f"sym_{symbol}", mapping=clean)