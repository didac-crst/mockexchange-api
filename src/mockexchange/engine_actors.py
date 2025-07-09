"""
Exchange engine implemented with Pykka actors.
Non-blocking, single-threaded semantics inside each actor.
"""
from __future__ import annotations

import itertools, os, random, threading, time
from datetime import timedelta
from typing import Dict, List
import base64, hashlib, itertools, random, time

import pykka
import logging
import redis

from .market import Market
from .orderbook import OrderBook
from .portfolio import Portfolio
from ._types import AssetBalance, Order
from .logging_config import logger

logging.getLogger("pykka").setLevel(logging.WARNING)

# ────────────────────────────────────────────
MIN_TIME = float(os.getenv("MIN_TIME_ANSWER_ORDER_MARKET", 0))
MAX_TIME = float(os.getenv("MAX_TIME_ANSWER_ORDER_MARKET", 1))
MIN_FILL = float(os.getenv("MIN_MARKET_ORDER_FILL_FACTOR", 1))
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
    def list(self, **kw):                   return self.ob.list(**kw)
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
    def _filled_amount(amount: float, min_fill: float = 1.0) -> float:
        return amount * random.uniform(min_fill, 1.0)

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
        status_prefix = {"open": "Created", "closed": "Executed", "canceled": "Canceled"}[
            order.status
        ]
        logger.info("%s %s", status_prefix, base_msg)

    # ---------- core balance moves ------------------------------------ #
    def _execute_buy(
        self,
        *,
        base: str,
        quote: str,
        amount: float,
        booked_notion: float,
        booked_fee: float,
        filled: float,
        price: float,
    ) -> Dict[str, float]:
        self._release(quote, booked_notion + booked_fee)
        cash = self.portfolio.get(quote).get()
        notion = filled * price
        fee = notion * self.commission
        cash.free -= notion + fee
        self.portfolio.set(cash)

        asset = self.portfolio.get(base).get()
        asset.free += filled
        self.portfolio.set(asset)
        return {"price": price, "notion": notion, "filled": filled, "fee": fee}

    def _execute_sell(
        self,
        *,
        base: str,
        quote: str,
        amount: float,
        booked_notion: float,
        booked_fee: float,
        filled: float,
        price: float,
    ) -> Dict[str, float]:
        self._release(base, amount)
        asset = self.portfolio.get(base).get()
        asset.free -= filled
        self.portfolio.set(asset)

        self._release(quote, booked_fee)
        cash = self.portfolio.get(quote).get()
        notion = filled * price
        fee = notion * self.commission
        cash.free += notion - fee
        self.portfolio.set(cash)
        return {"price": price, "notion": notion, "filled": filled, "fee": fee}

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
        if type not in {"market", "limit"}:
            raise ValueError("type must be market | limit")
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
            notion = None  # no need to book notion for sell orders

        # open order
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
            status="open",
            filled=None,
            ts_post=int(time.time() * 1000),
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
        if o.status != "open":
            raise ValueError("Only *open* orders can be canceled")

        base, quote = o.symbol.split("/")
        px = o.limit_price or self.market.last_price(o.symbol).get()

        if o.side == "buy":
            released_base = 0.0
            notion = o.amount * px
            fee = notion * o.fee_rate
            released_quote = notion + fee
            self._release(quote, released_quote)
        else:
            released_base = o.amount
            fee = o.amount * px * o.fee_rate
            released_quote = fee
            self._release(base, released_base)
            self._release(quote, released_quote)

        o.status = "canceled"
        o.ts_exec = int(time.time() * 1000)
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

        for o in self.order_book.list(status="open", symbol=symbol).get():
            if o.type == "market" or o.limit_price is None:
                continue

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
                booked_notion=o.booked_notion,
                booked_fee=o.booked_fee,
                filled=self._filled_amount(o.amount, MIN_FILL),
                price=ask if o.side == "buy" else bid,
            )

            o.status = "closed"
            o.price = tx["price"]
            o.filled = tx["filled"]
            o.notion = tx["notion"]
            o.fee_cost = tx["fee"]
            o.ts_exec = int(time.time() * 1000)
            self.order_book.update(o)
            self._log_order(o)

    def prune_orders_older_than(
        self,
        *,
        age: timedelta,
        statuses: tuple[str, ...] = ("closed", "canceled"),
    ) -> int:
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - int(age.total_seconds() * 1000)
        removed = 0
        for s in statuses:
            for o in self.order_book.list(status=s).get():
                ts = o.ts_exec or o.ts_post
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
        return self.market.set_last_price(
            symbol, price, ts, bid, ask, bid_volume, ask_volume
        ).get()

    def reset(self):
        self.portfolio.clear()
        self.order_book.clear()
        self._oid = itertools.count(1)

    # ---------- message handler & lifecycle --------------------------- #
    def on_receive(self, msg):
        if msg.get("cmd") == "_settle_market":
            oid = msg["oid"]
            o = self.order_book.get(oid).get()
            if o.status != "open":
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
                booked_notion=o.booked_notion,
                booked_fee=o.booked_fee,
                filled=self._filled_amount(o.amount, MIN_FILL),
                price=price,
            )
            o.status = "closed"
            o.price = tx["price"]
            o.filled = tx["filled"]
            o.notion = tx["notion"]
            o.fee_cost = tx["fee"]
            o.ts_exec = int(time.time() * 1000)
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
