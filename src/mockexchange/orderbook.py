"""
Orders in a single hash `orders`  (field=id, value=json).
"""
from __future__ import annotations
import json, redis
from typing import List
from ._types import Order

class OrderBook:
    """
    Single Redis hash called ``orders`` (`<id>` ⇒ JSON).

    Benefits of a single key:

    * atomic writes (`HSET`)  
    * keeps the DB namespace tidy  
    * 1-to-1 with a SQL table (`id` PK) if you ever migrate
    """

    def __init__(self, conn: redis.Redis) -> None:
        self.conn, self.key = conn, "orders"

    # Basic CRUD ---------------------------------------------------------
    def add(self, order: Order) -> None:
        self.conn.hset(self.key, order.id, order.to_json())

    def update(self, order: Order) -> None:
        self.conn.hset(self.key, order.id, order.to_json())

    def get(self, oid: str) -> Order:
        return Order.from_json(self.conn.hget(self.key, oid))

    def list(
        self,
        *,
        status: str | None = None,
        symbol: str | None = None,
    ) -> List[Order]:
        """
        In-memory filter because the hash is tiny (<<10k records) in tests.

        For a production-scale book move data to a proper DB and filter
        server-side.
        """
        out: List[Order] = []
        for _, blob in self.conn.hscan_iter(self.key):
            o = Order.from_json(blob)
            if status and o.status != status:
                continue
            if symbol and o.symbol != symbol:
                continue
            out.append(o)
        out.sort(key=lambda o: o.ts_post)  # sort by timestamp
        return out

    def clear(self) -> None:
        self.conn.delete(self.key)