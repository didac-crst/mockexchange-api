"""
Redis-backed order book with secondary indexes:

* Hash  : orders          (id → json blob)             – canonical store
* Set   : open:set        (ids)                        – every open order
* Set   : open:{symbol}   (ids)                        – open orders per symbol
"""
from __future__ import annotations

import json
import redis
from typing import List
from ._types import Order

OPEN_STATUS = ("new", "partially_filled")  # open orders
CLOSED_STATUS = ("filled", "canceled", "partially_canceled", "expired", "rejected")  # closed orders

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
        """
        Up-sert and keep indexes in sync:
        – If status turned *open* → closed/canceled → drop from sets.
        – If closed → *open* (unlikely) → add to sets.
        """
        # Fetch old status (if any) to decide how to maintain the indexes
        old_blob = self.r.hget(self.HASH_KEY, order.id)
        if old_blob:
            old = Order.from_json(old_blob)
            if old.status in OPEN_STATUS and order.status not in OPEN_STATUS:
                self._index_rem(old)
            elif old.status not in OPEN_STATUS and order.status in OPEN_STATUS:
                self._index_add(order)
        else:
            # brand-new id
            self._index_add(order)

        self.r.hset(self.HASH_KEY, order.id, order.to_json())

    def get(self, oid: str) -> Order:
        blob = self.r.hget(self.HASH_KEY, oid)
        if blob is None:
            raise KeyError(f"order {oid} not found")
        return Order.from_json(blob)

    def list(
        self,
        *,
        status: str | None = None,
        symbol: str | None = None,
        side: str | None = None,
        tail: int | None = None,
    ) -> List[Order]:
        """
        List orders by status, symbol, side, and limit the tail size.
        Open orders are indexed by symbol, so they can be fetched quickly.
        """
        orders: list[Order]

        if status in OPEN_STATUS:
            # Use secondary indexes
            if symbol:
                ids = self.r.smembers(self.OPEN_SYM_KEY.format(sym=symbol))
            else:
                ids = self.r.smembers(self.OPEN_ALL_KEY)
            if not ids:
                return []
            blobs = self.r.hmget(self.HASH_KEY, *ids)          # 1 round-trip
            orders = [Order.from_json(b) for b in blobs if b]
        else:
            # Legacy full scan
            orders = [
                Order.from_json(blob)
                for _, blob in self.r.hscan_iter(self.HASH_KEY)
            ]
            if status: # Already fulfilled by if status=='open'
                orders = [o for o in orders if o.status == status]
            if symbol: # Already fulfilled by if status=='open' if symbol is not None
                orders = [o for o in orders if o.symbol == symbol]
        if side: # Not fulfilled by if status=='open'
            orders = [o for o in orders if o.side == side]

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
