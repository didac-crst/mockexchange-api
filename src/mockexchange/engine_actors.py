"""
Exchange engine implemented with Pykka actors.
Non-blocking, single-threaded semantics inside each actor.
"""
from __future__ import annotations

import itertools, os, random, threading, time
import base64, hashlib
from datetime import timedelta
from typing import Dict, List

import pykka
import logging
import redis

from .market import Market
from .orderbook import OrderBook, OPEN_STATUS, CLOSED_STATUS
from .portfolio import Portfolio
from ._types import AssetBalance, Order
from .logging_config import logger

logging.getLogger("pykka").setLevel(logging.WARNING)

# ────────────────────────────────────────────
MIN_TIME = float(os.getenv("MIN_TIME_ANSWER_ORDER_MARKET", 0))
MAX_TIME = float(os.getenv("MAX_TIME_ANSWER_ORDER_MARKET", 1))
SIGMA_FILL = float(os.getenv("SIGMA_FILL_MARKET_ORDER", 0.1))
# ────────────────────────────────────────────


# ---------- Domain actors ------------------------------------------------ #
class _BaseActor(pykka.ThreadingActor):
    """Keeps a thread-local Redis client."""
    def __init__(self, redis_url: str):
        super().__init__()
        self.redis = redis.from_url(redis_url, decode_responses=True)


class MarketActor(_BaseActor):
    def __init__(self, redis_url: str):
        super().__init__(redis_url)
        self.market = Market(self.redis)

    def last_price(self, symbol: str) -> float:
        return self.market.last_price(symbol)

    def fetch_ticker(self, symbol: str) -> dict | None:
        return self.market.fetch_ticker(symbol)

    def set_last_price(self, *args, **kwargs):
        return self.market.set_last_price(*args, **kwargs)

    @property
    def tickers(self) -> List[str]:
        return self.market.tickers


class PortfolioActor(_BaseActor):
    def __init__(self, redis_url: str):
        super().__init__(redis_url)
        self.portfolio = Portfolio(self.redis)

    def get(self, asset: str) -> AssetBalance:
        return self.portfolio.get(asset)

    def set(self, bal: AssetBalance):
        self.portfolio.set(bal)

    def all(self):
        return self.portfolio.all()

    def clear(self):
        self.portfolio.clear()


class OrderBookActor(_BaseActor):
    def __init__(self, redis_url: str):
        super().__init__(redis_url)
        self.ob = OrderBook(self.redis)

    # CRUD
    def add(self, o: Order):                self.ob.add(o)
    def update(self, o: Order):             self.ob.update(o)
    def get(self, oid: str) -> Order:       return self.ob.get(oid)
    def list(self, **kw) -> List[Order]:    return self.ob.list(**kw)
    def remove(self, oid: str):             self.ob.remove(oid)
    def clear(self):                        self.ob.clear()


# ---------- Engine actor -------------------------------------------------- #
class ExchangeEngineActor(pykka.ThreadingActor):
    """
    One instance per process.  
    Every public method runs in this actor’s thread ⇒ no data races.
    """

    def __init__(self, *, redis_url: str, commission: float, cash_asset: str = "USDT"):
        super().__init__()
        self.cash_asset = cash_asset
        self.commission = commission
        self._oid = itertools.count(1)

        # Child actors (proxies)
        self.market = MarketActor.start(redis_url).proxy()
        self.portfolio = PortfolioActor.start(redis_url).proxy()
        self.order_book = OrderBookActor.start(redis_url).proxy()

        # Keep timer handles so we can cancel on shutdown
        self._timers: list[threading.Timer] = []

    # ---------- helpers ------------------------------------------------ #
    def _uid(self) -> str:
        ts = int(time.time())  # seconds
        raw = f"{int(ts*1000)}_{next(self._oid)}".encode()
        hash = base64.urlsafe_b64encode(hashlib.md5(raw).digest())[:6].decode() # Remove padding
        oid = f"{ts:010d}={hash}"
        return oid

    def _reserve(self, asset: str, qty: float) -> None:
        bal = self.portfolio.get(asset).get()
        if bal.free < qty:
            raise ValueError(f"insufficient {asset} to reserve")
        bal.free -= qty
        bal.used += qty
        self.portfolio.set(bal)

    def _release(self, asset: str, qty: float) -> float:
        bal = self.portfolio.get(asset).get()
        if bal.used < qty:
            qty = bal.used
        bal.used -= qty
        bal.free += qty
        if bal.free and bal.used / bal.free < 1e-10:
            bal.used = 0.0
        self.portfolio.set(bal)
        return qty

    @staticmethod
    def _filled_amount(amount: float, sigma: float = 0.1) -> float:
        """
        Simulate a random fill amount based on a mean and a delta.
        Half of the time it will fill exactly the amount requested,
        the other half it will fill less, but never more than requested.
        """
        mean = 1.0
        random_number = random.gauss(mean, sigma)
        if random_number < 0:
            random_number = 0.0
        if random_number > mean:
            random_number = mean
        return amount * random_number

    # ---------- logging ------------------------------------------------ #
    def _log_order(self, order: Order) -> None:
        px = order.price if order.price is not None else order.limit_price
        px_str = f"{px:.2f} {order.symbol}" if px else "MKT"
        fee_str = (
            "N/A" if order.fee_cost is None else f"{order.fee_cost:.2f} {order.fee_currency}"
        )
        asset = order.symbol.split("/")[0]
        base_msg = (
            f"Order {order.id} [{order.type}] "
            f"{order.side.upper()} {order.amount:.8f} {asset} at {px_str}, fee {fee_str}"
        )
        status_prefix = {"new": "Created",
                         "partially_filled": "Partially Filled",
                         "filled": "Executed",
                         "canceled": "Canceled",
                         "partially_canceled": "Partially Canceled",
                         "expired": "Expired",
                         "rejected": "Rejected",
                         }[order.status if isinstance(order.status, str) else order.status.value]
        logger.info("%s %s", status_prefix, base_msg)

    # ---------- core balance moves ------------------------------------ #
    def _execute_buy(
        self,
        *,
        base: str,
        quote: str,
        amount: float,
        prev_filled: float,
        prev_notion: float,
        prev_fee: float,
        price: float,
    ) -> Dict[str, float]:
        delta_filled = self._filled_amount(amount - prev_filled, SIGMA_FILL)
        filled_notion = delta_filled * price
        filled_fee = filled_notion * self.commission
        self._release(quote, filled_notion + filled_fee)
        cash = self.portfolio.get(quote).get()
        cash.free -= (filled_notion + filled_fee)
        self.portfolio.set(cash)

        asset = self.portfolio.get(base).get()
        asset.free += delta_filled
        self.portfolio.set(asset)
        total_notion = prev_notion + filled_notion
        total_fee = prev_fee + filled_fee
        total_filled = prev_filled + delta_filled
        new_price = total_notion / total_filled if total_filled > 0 else price
        return {"price": new_price, "notion": total_notion, "filled": total_filled, "fee": total_fee}

    def _execute_sell(
        self,
        *,
        base: str,
        quote: str,
        amount: float,
        prev_filled: float,
        prev_notion: float,
        prev_fee: float,
        price: float,
    ) -> Dict[str, float]:
        delta_filled = self._filled_amount(amount - prev_filled, SIGMA_FILL)
        filled_notion = delta_filled * price
        filled_fee = filled_notion * self.commission
        self._release(base, delta_filled)
        asset = self.portfolio.get(base).get()
        asset.free -= delta_filled
        self.portfolio.set(asset)

        self._release(quote, filled_fee)
        cash = self.portfolio.get(quote).get()
        cash.free += (filled_notion - filled_fee)
        self.portfolio.set(cash)
        total_notion = prev_notion + filled_notion
        total_fee = prev_fee + filled_fee
        total_filled = prev_filled + delta_filled
        new_price = total_notion / total_filled if total_filled > 0 else price
        return {"price": new_price, "notion": total_notion, "filled": total_filled, "fee": total_fee}

    # ---------- public API  ------------------------------------------- #
    @property
    def tickers(self):
        return self.market.tickers.get()

    def fetch_ticker(self, symbol: str):
        tick = self.market.fetch_ticker(symbol).get()
        if tick is None:
            raise ValueError(f"Ticker {symbol} not available")
        return tick

    def fetch_balance(self, asset: str | None = None):
        info = {k: info.to_dict() for k, info in self.portfolio.all().get().items()}
        bal = info if asset is None else info.get(asset, {})
        sorted_bal = {k: bal[k] for k in sorted(bal.keys())}  # sort by asset name
        return sorted_bal

    def fetch_balance_list(self):
        all_bal = list(self.portfolio.all().get().keys())
        all_bal.sort()  # sort by asset name
        return {"length": len(all_bal), "assets": all_bal}

    def create_order(
        self, *, symbol: str, side: str, type: str, amount: float, limit_price: float | None = None
    ):
        # validation
        if symbol not in self.market.tickers.get():
            raise ValueError(f"Ticker {symbol} does not exist")
        if amount <= 0:
            raise ValueError("amount must be > 0")
        if type not in {"market", "limit"}:
            raise ValueError("type must be market | limit")
        if type == "limit" and (limit_price is None or limit_price < 0):
            raise ValueError("limit_price must be ≥ 0 for limit orders")
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy | sell")

        last = self.market.last_price(symbol).get()
        px = last
        if type == "limit":
            if limit_price is None:
                raise ValueError("limit orders need limit_price")
            if side == "buy":
                px = limit_price
            else:  # sell – must reserve against worst-case (higher) price
                px = max(limit_price, last)

        base, quote = symbol.split("/")
        notion, fee = amount * px, amount * px * self.commission

        # funds check
        if side == "buy":
            have = self.portfolio.get(quote).get().free
            if have < notion + fee:
                raise ValueError(f"need {notion+fee:.2f} {quote}, have {have:.2f}")
        else:
            have = self.portfolio.get(base).get().free
            if have < amount:
                raise ValueError(f"need {amount:.8f} {base}, have {have:.8f}")

        # reserve
        if side == "buy":
            self._reserve(quote, notion + fee)
        else:
            self._reserve(base, amount)
            self._reserve(quote, fee)
            notion = 0.0 # no notion booked for sell orders

        # open order
        ts = int(time.time() * 1000)
        order = Order(
            id=self._uid(),
            symbol=symbol,
            side=side,
            type=type,
            amount=amount,
            limit_price=None if type == "market" else limit_price,
            notion_currency=quote,
            fee_rate=self.commission,
            fee_currency=quote,
            booked_notion=notion,
            booked_fee=fee,
            status="new",
            filled=0.0,
            ts_create=ts,
            ts_update=ts,
            ts_exec=None,
        )
        self.order_book.add(order)
        self._log_order(order)

        # market order ⇒ schedule async settle
        if type == "market":
            delay = random.uniform(MIN_TIME, MAX_TIME)
            t = threading.Timer(
                delay, lambda: self.actor_ref.tell({"cmd": "_settle_market", "oid": order.id})
            )
            t.start()
            self._timers.append(t)

        return order.__dict__

    # expose await-able helper
    def create_order_async(self, **kw):
        return self.create_order(**kw)  # caller will .get_async()

    def cancel_order(self, oid: str):
        o = self.order_book.get(oid).get()
        if o.status not in OPEN_STATUS:
            raise ValueError("Only *open* orders can be canceled")
        base, quote = o.symbol.split("/")
        px = o.limit_price or self.market.last_price(o.symbol).get()

        if o.side == "buy":
            remaining_notion = (o.amount - o.filled) * px
            remaining_fee    = remaining_notion * o.fee_rate
            released_quote   = remaining_notion + remaining_fee
            self._release(quote, released_quote)
            released_base = 0.0
        else:
            remaining_base = o.amount - o.filled
            remaining_fee  = remaining_base * px * o.fee_rate
            released_base  = self._release(base, remaining_base)
            released_quote = self._release(quote, remaining_fee)
        o.status = "canceled" if o.filled == 0 else "partially_canceled"
        ts = int(time.time() * 1000)
        o.ts_exec = o.ts_update = ts
        self.order_book.update(o)
        self._log_order(o)
        return {"canceled_order": o.__dict__, "freed": {base: released_base, quote: released_quote}}

    # ---------- price-tick & housekeeping ----------------------------- #
    def process_price_tick(self, symbol: str):
        ticker = self.market.fetch_ticker(symbol).get()
        if ticker is None:
            logger.warning("Ticker %s not found in market", symbol)
            return

        ask, bid = ticker["ask"], ticker["bid"]
        ask_vol = float(ticker.get("ask_volume", 0))
        bid_vol = float(ticker.get("bid_volume", 0))
        new_orders = self.order_book.list(status="new", symbol=symbol).get()
        partially_filled_orders = self.order_book.list(status="partially_filled", symbol=symbol).get()
        open_orders = new_orders + partially_filled_orders
        for o in open_orders:
            if o.type == "market":
                fillable = (
                    (o.side == "buy" and o.amount <= ask_vol)
                    or (o.side == "sell" and o.amount <= bid_vol)
                )
            elif o.limit_price is None:
                pass # should not happen, but just in case

            else:
                fillable = (
                    (o.side == "buy" and ask <= o.limit_price and o.amount <= ask_vol)
                    or (o.side == "sell" and bid >= o.limit_price and o.amount <= bid_vol)
                )
            if not fillable:
                continue

            base, quote = symbol.split("/")
            tx = (
                self._execute_buy
                if o.side == "buy"
                else self._execute_sell
            )(
                base=base,
                quote=quote,
                amount=o.amount,
                prev_filled=o.filled,
                prev_notion=o.notion,
                prev_fee=o.fee_cost,
                price=ask if o.side == "buy" else bid,
            )
            ts = int(time.time() * 1000)
            o.ts_update = ts

            o.price   = tx["price"]
            o.filled  = tx["filled"]
            o.notion  = tx["notion"]
            o.fee_cost = tx["fee"]

            full = o.filled >= o.amount - 1e-12
            if full:
                o.status = "filled"
                o.ts_exec = ts
            else:
                o.status = "partially_filled"
            self.order_book.update(o)
            self._log_order(o)

    def prune_orders_older_than(
        self,
        *,
        age: timedelta,
        statuses: tuple[str, ...] = CLOSED_STATUS
    ) -> int:
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - int(age.total_seconds() * 1000)
        removed = 0
        for s in statuses:
            for o in self.order_book.list(status=s).get():
                if o.status in CLOSED_STATUS:
                    ts = o.ts_exec
                    if ts < cutoff:
                        self.order_book.remove(o.id)
                        removed += 1
        if removed:
            logger.info("Pruned %d stale orders older than %s", removed, age)
        else:
            logger.info("No stale orders older than %s found", age)
        return removed

    # ----- dry-run helper --------------------------------------------------- #
    def can_execute(self, *, symbol: str, side: str,
                    amount: float, price: float | None = None):
        px = price or self.market.last_price(symbol).get()
        base, quote = symbol.split("/")
        fee = amount * px * self.commission
        if side == "buy":
            have = self.portfolio.get(quote).get().free
            need = amount * px + fee
            ok = have >= need
            reason = None if ok else f"need {need:.2f} {quote}, have {have:.2f}"
        else:  # sell
            have = self.portfolio.get(base).get().free
            ok = have >= amount
            reason = None if ok else f"need {amount:.8f} {base}, have {have:.8f}"
        return {"ok": ok, "reason": reason}

    # ----- admin helpers ---------------------------------------------------- #
    def set_balance(self, asset: str, *, free: float = 0.0, used: float = 0.0):
        if free < 0 or used < 0:
            raise ValueError("free/used must be ≥ 0")
        self.portfolio.set(AssetBalance(asset, free, used))
        return self.portfolio.get(asset).get().to_dict()

    def fund_asset(self, asset: str, amount: float):
        if amount <= 0:
            raise ValueError("amount must be > 0")
        bal = self.portfolio.get(asset).get()
        bal.free += amount
        self.portfolio.set(bal)
        return bal.to_dict()

    def set_ticker(
        self,
        symbol: str,
        price: float,
        ts: float | None = None,
        bid: float | None = None,
        ask: float | None = None,
        bid_volume: float | None = None,
        ask_volume: float | None = None,
    ):
        if symbol not in self.market.tickers.get():
            raise ValueError(f"Ticker {symbol} does not exist")

        # 1️⃣ fire‑and‑forget the price update
        self.market.set_last_price(
            symbol, price, ts, bid, ask, bid_volume, ask_volume
        ).get()           # we still wait here to ensure the write has finished

        # 2️⃣ read the now‑current ticker snapshot and hand it back
        return self.market.fetch_ticker(symbol).get()

    def reset(self):
        self.portfolio.clear()
        self.order_book.clear()
        self._oid = itertools.count(1)

    # ---------- message handler & lifecycle --------------------------- #
    def on_receive(self, msg):
        if msg.get("cmd") == "_settle_market":
            oid = msg["oid"]
            o = self.order_book.get(oid).get()
            if o.status not in OPEN_STATUS:
                return
            base, quote = o.symbol.split("/")
            price = self.market.last_price(o.symbol).get()
            tx = (
                self._execute_buy
                if o.side == "buy"
                else self._execute_sell
            )(
                base=base,
                quote=quote,
                amount=o.amount,
                prev_filled=o.filled,
                prev_notion=o.notion,
                prev_fee=o.fee_cost,
                price=price,
            )
            ts = int(time.time() * 1000)
            o.ts_update = ts

            o.price   = tx["price"]
            o.filled  = tx["filled"]
            o.notion  = tx["notion"]
            o.fee_cost = tx["fee"]

            full = o.filled >= o.amount - 1e-12
            if full:
                o.status = "filled"
                o.ts_exec = ts
            else:
                o.status = "partially_filled"
            self.order_book.update(o)
            self._log_order(o)

    def on_stop(self):
        # cancel pending timers
        for t in self._timers:
            t.cancel()
        for a in (self.market, self.portfolio, self.order_book):
            a.stop()


# ---------- façade helper ---------------------------------------------- #
def start_engine(redis_url: str, commission: float) -> pykka.ActorProxy:
    """
    Convenience for tests / server:
        engine = start_engine("redis://127.0.0.1:6379/0", commission=0.001)
    """
    return ExchangeEngineActor.start(redis_url=redis_url, commission=commission).proxy()
