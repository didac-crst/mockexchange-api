"""
Read-only ticker feed (Valkey hashes `sym_<SYMBOL>`).
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict
import redis

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
    def fetch_ticker(self, symbol: str) -> Dict[str, Any] | None:
        """
        Return a *ccxt-ish* ticker – just the keys our engine needs.

        :raises ValueError: if the hash is missing
        :raises RuntimeError: if mandatory fields cannot be parsed
        """
        h = self.conn.hgetall(f"sym_{symbol}")
        if not h:
            return None                            # symbol vanished – treat as absent
        try:
            price = float(h["price"])
            ts    = float(h["timestamp"])
        except (KeyError, ValueError):
            # Just log once and skip this symbol
            logger.warning("Malformed ticker blob for %s: %s", symbol, h)
            return None

        return {
            "symbol": symbol,
            "last":   price,
            "timestamp": ts,
            "bid":  float(h.get("bid", price)),
            "ask":  float(h.get("ask", price)),
            "bid_volume": float(h.get("bidVolume", 0.0)),
            "ask_volume": float(h.get("askVolume", 0.0)),
            "info": h,
        }