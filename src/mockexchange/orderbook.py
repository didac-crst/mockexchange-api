"""
Redis-backed order book with secondary indexes:

* Hash  : orders          (id → json blob)             – canonical store
* Set   : open:set        (ids)                        – every open order
* Set   : open:{symbol}   (ids)                        – open orders per symbol
"""
# orderbook.py
from __future__ import annotations

import redis
from typing import List
from .constants import OPEN_STATUS, CLOSED_STATUS
from ._types import Order

class OrderBook:
    HASH_KEY      = "orders"
    OPEN_ALL_KEY  = "open:set"
    OPEN_SYM_KEY  = "open:{sym}"        # .format(sym=symbol)

    def __init__(self, conn: redis.Redis) -> None:
        self.r = conn

    # ------------ internal helpers ------------------------------------ #
    def _index_add(self, order: Order) -> None:
        """Add id to the open indexes (only if order is OPEN)."""
        if order.status not in OPEN_STATUS:
            # Only add to indexes if the order is open (new or partially filled)
            return
        self.r.sadd(self.OPEN_ALL_KEY, order.id)
        self.r.sadd(self.OPEN_SYM_KEY.format(sym=order.symbol), order.id)

    def _index_rem(self, order: Order) -> None:
        """Remove id from the open indexes."""
        self.r.srem(self.OPEN_ALL_KEY, order.id)
        self.r.srem(self.OPEN_SYM_KEY.format(sym=order.symbol), order.id)

    # ------------ CRUD ------------------------------------------------- #
    def add(self, order: Order) -> None:
        self.r.hset(self.HASH_KEY, order.id, order.to_json())
        self._index_add(order)

    def update(self, order: Order) -> None:
        """Update an existing order."""
        self.r.hset(self.HASH_KEY, order.id, order.to_json(include_history=True))
        
    def get(self, oid: str, *, include_history: bool = False) -> Order:
        blob = self.r.hget(self.HASH_KEY, oid)
        if blob is None:
            raise ValueError(f"Order {oid} not found")
        else:
            return Order.from_json(blob, include_history=include_history)

    def list(
        self,
        *,
        status: list[str] | str | None = None,
        symbol: str | None = None,
        side: str | None = None,
        tail: int | None = None,
        include_history: bool = False
    ) -> List[Order]:
        """
        List orders by status, symbol, side, and limit the tail size.
        Open orders are indexed by symbol, so they can be fetched quickly.
        """
        orders: list[Order]
        if isinstance(status, str):
            status = [status]
        if status is None:
            status = OPEN_STATUS + CLOSED_STATUS
        # Only if all statuses are OPEN_STATUS, we can use the indexes
        if all(s in OPEN_STATUS for s in status):
            # Use secondary indexes
            if symbol:
                ids = self.r.smembers(self.OPEN_SYM_KEY.format(sym=symbol))
            else:
                ids = self.r.smembers(self.OPEN_ALL_KEY)
            if not ids:
                return []
            blobs = self.r.hmget(self.HASH_KEY, *ids)          # 1 round-trip
            orders = [Order.from_json(b, include_history=include_history) for b in blobs if b]
        else:
            # Legacy full scan
            orders = [
                Order.from_json(blob, include_history=include_history)
                for _, blob in self.r.hscan_iter(self.HASH_KEY)
            ]
            if symbol: # Already fulfilled by if status in OPEN_STATUS if symbol is not None
                orders = [o for o in orders if o.symbol == symbol]
        # Filter for both cases
        if side: # Not fulfilled by if status in OPEN_STATUS
            orders = [o for o in orders if o.side == side]
        # Make sure we only return orders with the requested status
        orders = [o for o in orders if o.status in status]

        # chronological order
        orders.sort(key=lambda o: o.ts_update, reverse=True)
        if tail is not None and tail > 0:
            orders = orders[:tail]
        return orders

    # ---------- hard delete ------------------------------------------ #
    def remove(self, oid: str) -> None:
        """Erase an order from storage and all indexes. Idempotent."""
        blob = self.r.hget(self.HASH_KEY, oid)
        if not blob:                       # already gone
            return
        o = Order.from_json(blob)
        if o.status in OPEN_STATUS:            # keep indexes consistent
            self._index_rem(o)
        pipe = self.r.pipeline()
        pipe.hdel(self.HASH_KEY, oid)
        pipe.execute()

    # ---------- admin ------------------------------------------ #
    def clear(self) -> None:
        pipe = self.r.pipeline()
        pipe.delete(self.HASH_KEY)
        pipe.delete(self.OPEN_ALL_KEY)
        # nuke every per-symbol set in one pass
        for key in self.r.keys(self.OPEN_SYM_KEY.format(sym="*")):
            pipe.delete(key)
        pipe.execute()
