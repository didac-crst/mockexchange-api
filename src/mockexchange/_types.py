"""
Shared tiny enums / dataclasses used across the package.
Keeps circular-import headaches away from the business logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional
import time
import json

# ─── Domain constants ────────────────────────────────────────────────────
OrderSide  = type("OrderSide",  (), {"BUY": "buy",  "SELL": "sell"})
OrderType  = type("OrderType",  (), {"MARKET": "market", "LIMIT": "limit"})
OrderState = type("OrderState", (), {"OPEN": "open", "CLOSED": "closed", "CANCELED": "canceled"})

# ─── Data classes ────────────────────────────────────────────────────────
@dataclass
class AssetBalance:
    """
    One row inside the portfolio hash.

    *Why not store ``total``?*  
    It’s always `free + used`, so we compute it on the fly.
    """

    asset: str
    free: float = 0.0
    used: float = 0.0

    # Derived ------------------------------------------------------------
    @property
    def total(self) -> float:
        return self.free + self.used

    # (De)serialise ------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "asset": self.asset,
            "free": self.free,
            "used": self.used,
            "total": self.total,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AssetBalance":
        return cls(
            asset=d["asset"],
            free=float(d.get("free", 0.0)),
            used=float(d.get("used", 0.0)),
        )

# @dataclass
# class Order:
#     id: str
#     symbol: str                # e.g. "BTC/USDT"
#     side: str                  # OrderSide.BUY / SELL
#     type: str                  # OrderType.MARKET / LIMIT
#     amount: float
#     price: Optional[float]     # None for market
#     fee_rate: float
#     fee_cost: float
#     fee_currency: str          # typically USDT
#     status: str = "open"
#     filled: float = 0.0
#     ts_post: int = field(default_factory=lambda: int(time.time() * 1000))
#     ts_exec: Optional[int] = None               # filled when CLOSED

#     # (de)serialise helpers
#     def dumps(self) -> Dict[str, Any]:
#         return {k: (v if not isinstance(v, set) else list(v)) for k, v in self.__dict__.items()}

#     @classmethod
#     def loads(cls, d: Dict[str, Any]) -> "Order":
#         # tolerate older blobs that miss ts_exec
#         if "ts_exec" not in d:
#             d["ts_exec"] = None
#         return cls(**d)

@dataclass
class Order:
    """
    Internal order representation (kept small on purpose).

    Fields
    ------
    id          unique, URL-safe token  
    type        ``market`` / ``limit``  
    side        ``buy`` / ``sell``  
    status      ``open`` / ``closed`` / ``canceled``  
    ts_post     millis when the order was *created*  
    ts_exec     millis when it got *filled* (or None until then)

    Fees are quoted in the *quote* currency (usually `USDT`).
    """

    id: str
    symbol: str
    side: str
    type: str
    amount: float
    notion_currency: str  # usually the quote currency, e.g. USDT
    fee_currency: str
    fee_rate: float
    # Runtime-mutable fields
    price: Optional[float] = None  # None for market orders
    status: str = "open"
    filled: float = 0.0 # until filled
    notion: float = 0.0 # until filled
    fee_cost: float = 0.0 #  until filled
    ts_post: int = field(default_factory=lambda: int(time.time() * 1000))
    ts_exec: Optional[int] = None  # updated when status→closed

    # (De)serialise ------------------------------------------------------
    def to_json(self) -> str:
        return json.dumps(self.__dict__, separators=(",", ":"))

    @classmethod
    def from_json(cls, blob: str) -> "Order":
        return cls(**json.loads(blob))