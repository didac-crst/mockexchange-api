"""
Redis-backed order book with secondary indexes:

* Hash  : orders          (id → json blob)             – canonical store
* Set   : open:set        (ids)                        – every open order
* Set   : open:{symbol}   (ids)                        – open orders per symbol
"""
# orderbook.py
from __future__ import annotations

import redis
from typing import Iterable, Union, Optional, TypeAlias
from .constants import OPEN_STATUS, OrderState, OrderSide, OPEN_STATUS_STR
from ._types import Order

StatusArg: TypeAlias = Union[str, OrderState]        # one element
SideArg: TypeAlias = Union[str, OrderSide]        # one element

class OrderBook:
    HASH_KEY      = "orders"
    OPEN_ALL_KEY  = "open:set"
    OPEN_SYM_KEY  = "open:{sym}"        # .format(sym=symbol)

    def __init__(self, conn: redis.Redis) -> None:
        self.r = conn

    # ------------ internal helpers ------------------------------------ #
    def _index_add(self, order: Order) -> None:
        """Add id to the open indexes (only if order is OPEN)."""
        is_open = (
            order.status in OPEN_STATUS or
            (isinstance(order.status, str) and order.status in OPEN_STATUS_STR)
        )
        if not is_open:
            # If the order is not open, we don't need to index it.
            # This is important for performance, as we don't want to index
            # orders that are already closed (filled, canceled, etc.).
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
        status: Optional[Union[StatusArg, Iterable[StatusArg]]] = None,
        symbol: str | None = None,
        side: Optional[SideArg] = None,
        tail: int | None = None,
        include_history: bool = False
    ) -> list[Order]:
        """
        List orders by status, symbol, side, and limit the tail size.
        Open orders are indexed by symbol, so they can be fetched quickly.
        """
        orders: list[Order]
        # ── normalise `status` to a *set of raw-string values* ───────────────
        if status is None:
            status = {s.value for s in OrderState}
        elif isinstance(status, OrderState):
            status = {status.value}
        elif isinstance(status, str):
            status = {status}
        else:                     # iterable of str | OrderState
            status = {
                s.value if isinstance(s, OrderState) else s
                for s in status
            }
        # ── normalise `side` to a *set of raw-string values* ────────────────
        if side is None:
            side_set = None                      # means “no filtering”
        elif isinstance(side, OrderSide):
            side_set = {side.value}
        elif isinstance(side, str):
            side_set = {side}
        else:                                    # iterable of str | OrderSide
            side_set = {
                s.value if isinstance(s, OrderSide) else s
                for s in side
            }
        # Only if all statuses are OPEN_STATUS, we can use the indexes
        if all(s in OPEN_STATUS_STR for s in status):
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
                orders = [o for o in orders if o.symbol == symbol] # Symbol is a plain text
        # Filter for both cases
        # 1) side-filter
        if side_set is not None:
            orders = [
                o for o in orders
                if (o.side.value if isinstance(o.side, OrderSide) else o.side) in side_set
            ]

        # 2) status-filter
        orders = [
            o for o in orders
            if (o.status.value if isinstance(o.status, OrderState) else o.status) in status
        ]

        # chronological order on update timestamp
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
        is_open = (
            o.status in OPEN_STATUS or
            (isinstance(o.status, str) and o.status in OPEN_STATUS_STR)
        )
        if is_open:
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
