"""
Exchange engine implemented with Pykka actors.
Non-blocking, single-threaded semantics inside each actor.
"""
# engine_actors.py
from __future__ import annotations

import itertools, os, random, threading, time
import base64, hashlib
from datetime import timedelta
from typing import Dict, List
from collections import defaultdict
import math

import pykka
import logging
import redis

from .market import Market
from .orderbook import OrderBook
from .portfolio import Portfolio
from .constants import OPEN_STATUS, CLOSED_STATUS
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
    def get(self, oid: str, include_history: bool = False) -> Order:
        return self.ob.get(oid, include_history=include_history)
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
            # In case of cancellation, we need to release everything even if used < qty due to rounding
            # and ensure to have used == 0.0
            qty = bal.used
        bal.used -= qty
        bal.free += qty
        if bal.free and bal.used / bal.free < 1e-10:
            bal.used = 0.0
        self.portfolio.set(bal)
        return qty

    @staticmethod
    def _slippage_simulate(amount: float, sigma: float = 0.5) -> float:
        """
        Simulate a random slippage based on a mean and a delta.
        Half of the time it will be exactly the amount requested,
        the other half it will be less, but never more than requested.
        """
        mean = 1.0
        random_number = random.gauss(mean, sigma)
        if random_number < 0:
            random_number = 0.0
        if random_number > mean:
            random_number = mean
        return amount * random_number

    # ---- reservation guard ------------------------------------------- #
    def _cancel_for_insufficient_reserve(self, o: Order, reason: str) -> None:
        """
        Cancel (or partially cancel) *o* because the amounts still reserved
        in the portfolio are not enough to execute the next fill.

        The function
        • releases whatever is still reserved,
        • sets the status to canceled / partially_canceled,
        • appends a history entry,
        • squashes the residual booking,
        • updates the order book and emits a log entry.
        """
        base, quote = o.symbol.split("/")

        # release leftovers
        if o.side == "buy":
            self._release(quote, o.residual_quote())
        else:
            self._release(base,  o.residual_base())
            self._release(quote, o.residual_quote())

        # final order status
        o.status = "canceled" if o.actual_filled == 0 else "partially_canceled"
        ts = int(time.time() * 1000)
        o.ts_update = o.ts_finish = ts
        o.comment = reason
        o.add_history(ts=ts, status=o.status, comment=o.comment)
        o.squash_booking()

        # persist and log
        self.order_book.update(o)
        self._log_order(o)

    # ---------- logging ------------------------------------------------ #
    def _log_order(self, order: Order) -> None:
        px = order.price if order.price is not None else order.limit_price
        fee_str = f"{order.actual_fee:.2f} {order.fee_currency}" if order.actual_fee is not None else "N/A"
        asset = order.symbol.split("/")[0]
        if order.status in ("partially_filled","filled"):
            order_hist = order.last_history
            if order_hist is None:
                logger.warning("No history for order %s", order.id)
                return
            amount = order_hist.actual_filled
            px_str = f"{order_hist.price:.2f} {order.symbol}"
            fee_str = f"{order_hist.actual_fee:.2f} {order.fee_currency}"
        else:
            amount = order.amount - order.actual_filled
            px_str = f"{px:.2f} {order.symbol}" if px else "MKT"
            fee_str = "N/A"
        base_msg = (
            f"Order {order.id} [{order.type}] "
            f"{order.side.upper()} {amount:.8f} {asset} at {px_str}, fee {fee_str}"
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
        fillable_amount: float,
        price: float,
    ) -> Dict[str, float]:
        filled_notion = fillable_amount * price
        filled_fee = filled_notion * self.commission
        # Reduce the cash balance
        self._release(quote, filled_notion + filled_fee)
        cash = self.portfolio.get(quote).get()
        cash.free -= (filled_notion + filled_fee)
        self.portfolio.set(cash)
        # Increase the asset balance
        asset = self.portfolio.get(base).get()
        asset.free += fillable_amount
        self.portfolio.set(asset)
        return {"filled_notion": filled_notion, "filled_fee": filled_fee}

    def _execute_sell(
        self,
        *,
        base: str,
        quote: str,
        fillable_amount: float,
        price: float,
    ) -> Dict[str, float]:
        filled_notion = fillable_amount * price
        filled_fee = filled_notion * self.commission
        # Reduce the asset balance
        self._release(base, fillable_amount)
        asset = self.portfolio.get(base).get()
        asset.free -= fillable_amount
        self.portfolio.set(asset)
        # Increase the cash balance
        self._release(quote, filled_fee)
        cash = self.portfolio.get(quote).get()
        cash.free += (filled_notion - filled_fee)
        self.portfolio.set(cash)
        return {"filled_notion": filled_notion, "filled_fee": filled_fee}
    
    # ---------- consistency checks ------------------------------------ #
    def check_consistency(self, eps: float = 1e-9):
        """
        Compare what *should* be reserved (from open orders) with what's in portfolio.used.
        Returns a dict of mismatches.
        """
        open_orders = (
            self.order_book.list(status="new").get() +
            self.order_book.list(status="partially_filled").get()
        )

        expected = defaultdict(float)
        for o in open_orders:
            base, quote = o.symbol.split("/")
            if o.side == "buy":
                expected[quote] += max(o.reserved_notion_left, 0.0)
                expected[quote] += max(o.reserved_fee_left,    0.0)
            else:
                expected[base]  += max(o.residual_base(),      0.0)
                expected[quote] += max(o.reserved_fee_left,    0.0)

        mismatches = {}

        # current balances
        bals = self.portfolio.all().get()

        # check all assets that appear anywhere
        assets = set(bals.keys()) | set(expected.keys())
        for asset in assets:
            used_now = bals.get(asset, AssetBalance(asset)).used
            used_should = expected.get(asset, 0.0)
            if not math.isclose(used_now, used_should, rel_tol=0.0, abs_tol=eps):
                mismatches[asset] = {"used_now": used_now, "used_should": used_should}

        if mismatches:
            logger.error("Reservation mismatches: %s", mismatches)
        return mismatches
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
        comment = None
        enough_funds = False
        if side == "buy":
            have = self.portfolio.get(quote).get().free
            if have >= notion + fee:
                enough_funds = True
            else:
                comment = (
                    f"Need {notion + fee:.2f} {quote}, have {have:.2f}"
                )
        else:
            # Test if we have enough base to sell
            have = self.portfolio.get(base).get().free
            if have >= amount:
                enough_funds = True
            else:
                comment = (
                    f"Need {amount:.8f} {base}, have {have:.8f}"
                )
            # Test if we have enough quote to pay the fee
            have = self.portfolio.get(quote).get().free
            if have >= fee:
                enough_funds = enough_funds and True
            else:
                comment = (
                    f"Need {fee:.2f} {quote}, have {have:.2f}"
                ) if comment is None else comment + f", need {fee:.2f} {quote}, have {have:.2f}"
        if enough_funds:
            if side == "buy":
                self._reserve(quote, notion + fee)
            else:
                self._reserve(base, amount)
                self._reserve(quote, fee)
        # set booked values per side
        booked_notion = notion if side == "buy" else 0.0
        status = "new" if enough_funds else "rejected"
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
            fee_currency=quote,
            fee_rate=self.commission,
            initial_booked_notion=booked_notion,
            reserved_notion_left=booked_notion,
            initial_booked_fee=fee,
            reserved_fee_left=fee,
            status=status,
            ts_create=ts,
            ts_update=ts,
            ts_finish=ts if status in CLOSED_STATUS else None,
            comment=comment,
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
        if o.side == "buy":
            rq = o.residual_quote()
            released_quote = self._release(quote, rq)
            released_base  = 0.0
        else:
            rb = o.residual_base()
            rq = o.residual_quote()
            released_base  = self._release(base, rb)
            released_quote = self._release(quote, rq)
        o.status = "canceled" if o.actual_filled == 0 else "partially_canceled"
        ts = int(time.time() * 1000)
        o.ts_finish = o.ts_update = ts
        o.comment = "Order canceled by user"
        o.add_history(
            ts=ts,
            status=o.status,
            comment=o.comment,
        )
        o.squash_booking()
        self.order_book.update(o)
        self._log_order(o)
        # Sanity checks (idempotency + no leaks)
        if o.side == "buy":
            assert o.reserved_notion_left >= -1e-9
            assert o.reserved_fee_left    >= -1e-9
        else:
            assert o.residual_base()      >= -1e-9
            assert o.reserved_fee_left    >= -1e-9

        return {
            "canceled_order": o.__dict__,
            "freed": {base: released_base, quote: released_quote},
        }

    # ---------- price-tick & housekeeping ----------------------------- #
    def get_market_state(self, symbol: str):
        """Get the current market state for the given symbol.
        Returns a dict with the current ask, bid, ask_volume, and bid_volume.
        """
        ticker = self.market.fetch_ticker(symbol).get()
        if ticker is None:
            raise ValueError(f"Ticker {symbol} not found in market")
        return {
            "ask": float(ticker["ask"]),
            "bid": float(ticker["bid"]),
            "ask_volume": float(ticker.get("ask_volume", 0)),
            "bid_volume": float(ticker.get("bid_volume", 0)),
        }

    def process_single_order(self, o: Order, ask: float, bid: float, ask_volume: float, bid_volume: float):
        """
        Process a single order based on the current market state.
        This simulates order fills based on the current market state.
        """

        o_fresh = self.order_book.get(o.id, include_history=False).get()
        if o_fresh.status not in OPEN_STATUS or o_fresh.actual_filled >= o_fresh.amount - 1e-12:
            return
        o = o_fresh
        fillable = False
        need_amount = o.amount - o.actual_filled
        # Simulate slippage
        total_amount_available = ask_volume if o.side == "buy" else bid_volume
        amount_available = self._slippage_simulate(total_amount_available, SIGMA_FILL)
        if amount_available < need_amount:
            fillable_amount = amount_available
        else:
            fillable_amount = need_amount
        if fillable_amount <= 0:
            return
        if o.type == "market":
            fillable = True
        elif o.type == "limit":
            if o.limit_price is None:
                raise ValueError("Limit orders must have a limit_price")
            fillable = (
                (o.side == "buy" and ask <= o.limit_price)
                or (o.side == "sell" and bid >= o.limit_price)
            )
        if not fillable:
            return
        base, quote = o.symbol.split("/")
        px = ask if o.side == "buy" else bid

        # ---------------- reservation check ---------------------------
        # If the order is not fillable due to insufficient reserves,
        # we cancel it and release the reserved amounts.
        if o.side == "buy":
            need_quote = fillable_amount * px * (1 + self.commission)
            total_balance_q = self.portfolio.get(quote).get().total
            if total_balance_q + 1e-12 < need_quote:
                self._cancel_for_insufficient_reserve(
                    o,
                    reason=f"Insufficient {quote} reserved to buy (need {need_quote:.8f} {quote}, have {total_balance_q:.8f} {quote})"
                )
                return
        else:  # sell
            need_asset = fillable_amount
            need_fee_q = fillable_amount * px * self.commission
            total_balance_a = self.portfolio.get(base).get().total
            total_balance_q = self.portfolio.get(quote).get().total
            if total_balance_a + 1e-12 < need_asset:
                self._cancel_for_insufficient_reserve(
                    o,
                    reason=(
                        f"Insufficient {base} reserved for sell (need {need_asset:.8f} {base}, have {total_balance_a:.8f} {base})"
                    ),
                )
                return
            elif total_balance_q + 1e-12 < need_fee_q:
                self._cancel_for_insufficient_reserve(
                    o,
                    reason=(
                        f"Insufficient {quote} reserved for sell to pay fee "
                        f"(need {need_fee_q:.8f} {quote}, have {total_balance_q:.8f} {quote})"
                    ),
                )
                return

        tx = (
            self._execute_buy
            if o.side == "buy"
            else self._execute_sell
        )(
            base=base,
            quote=quote,
            fillable_amount=fillable_amount,
            price=px,
        )
        # Calculate the new order state
        ts = int(time.time() * 1000)
        total_filled = o.actual_filled + fillable_amount
        total_notion = o.actual_notion + tx["filled_notion"]
        total_fee    = o.actual_fee + tx["filled_fee"]
        # Update the order
        o.ts_update = ts
        o.actual_filled = total_filled
        o.actual_notion = total_notion
        o.actual_fee    = total_fee
        o.price         = o.avg_price

        # shrink reservations
        if o.side == "buy":
            o.reserved_notion_left = max(o.reserved_notion_left - tx["filled_notion"], 0.0)
            o.reserved_fee_left    = max(o.reserved_fee_left    - tx["filled_fee"],    0.0)
        else:
            # sell: only fee was reserved in quote, base reservation shrinks via residual_base()
            o.reserved_fee_left    = max(o.reserved_fee_left    - tx["filled_fee"],    0.0)

        # free leftovers
        full = o.actual_filled >= (o.amount - 1e-12)
        if full:
            base, quote = o.symbol.split("/")
            # Any leftovers of reservations -> release here
            if o.side == "buy":
                rq = o.residual_quote()
                if rq > 1e-9:
                    self._release(quote, rq)
            else:
                rb = o.residual_base()
                rq = o.residual_quote()
                if rb > 1e-9: self._release(base, rb)
                if rq > 1e-9: self._release(quote, rq)
            new_status = "filled"
            o.ts_finish = ts
            o.squash_booking()
        else:
            new_status = "partially_filled"
        o.status = new_status
        o.add_history(
            ts=ts,
            status=new_status,
            price=px,
            amount_remain=o.amount_remain,
            actual_filled=fillable_amount,
            actual_notion=tx["filled_notion"],
            actual_fee=tx["filled_fee"],
            reserved_notion_left=o.reserved_notion_left,
            reserved_fee_left=o.reserved_fee_left,
        )
        # Update the order book
        self.order_book.update(o)
        self._log_order(o)


    def process_price_tick(self, symbol: str):
        """
        Process a price tick for the given symbol.
        This simulates order fills based on the current market state.
        """
        market_state = self.get_market_state(symbol)  # ask, bid, ask_volume, bid_volume
        open_orders = self.order_book.list(status=OPEN_STATUS, symbol=symbol).get()
        for o in open_orders:
            self.process_single_order(o, **market_state)

    def prune_orders_older_than(
        self,
        *,
        age: timedelta,
    ) -> int:
        """
        Prune orders that are older than the specified age.
        This removes orders that are in CLOSED_STATUS and older than the specified age.
        Returns the number of orders removed.
        """
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - int(age.total_seconds() * 1000)
        removed = 0
        for s in CLOSED_STATUS:
            for o in self.order_book.list(status=s).get():
                if o.status in CLOSED_STATUS:
                    ts = o.ts_finish
                    if ts < cutoff:
                        self.order_book.remove(o.id)
                        removed += 1
        if removed:
            logger.info("Pruned %d stale orders older than %s", removed, age)
        else:
            logger.info("No stale orders older than %s found", age)
        return removed

    def expire_orders_older_than(
        self,
        *,
        age: timedelta,
    ) -> int:
        """
        Expire open orders that are older than the specified age.
        """
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - int(age.total_seconds() * 1000)
        expired = 0
        for s in OPEN_STATUS:
            for o in self.order_book.list(status=s).get():
                if o.status in OPEN_STATUS:
                    ts = o.ts_update
                    if ts < cutoff:
                        o.status = "expired"
                        o.ts_finish = ts
                        o.add_history(
                            ts=ts,
                            status="expired",
                            comment="Order expired due to inactivity"
                        )
                        self.order_book.update(o)
                        self._log_order(o)
                        expired += 1
        if expired:
            logger.info("Expired %d inactive orders older than %s", expired, age)
        else:
            logger.info("No expired orders older than %s found", age)
        return expired

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
        # cancel and drain timers
        for t in self._timers:
            t.cancel()
        self._timers.clear()
        self._oid = itertools.count(1)

    # ---------- message handler & lifecycle --------------------------- #
    def on_receive(self, msg):
        if msg.get("cmd") == "_settle_market":
            oid = msg["oid"]
            try:
                o = self.order_book.get(oid).get()
            except KeyError:
                logger.warning("Order %s vanished before settle; ignoring", oid)
                return
            if o.status not in OPEN_STATUS or o.actual_filled >= o.amount - 1e-12:
                return
            market_state = self.get_market_state(o.symbol)
            self.process_single_order(o, **market_state)

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
