"""
Core business-logic — no FastAPI, no Docker.
"""
from __future__ import annotations
from dataclasses import dataclass
import base64, hashlib, itertools, random, time
from typing import Dict, Any, Tuple, Optional
import redis
import asyncio
from .market import Market
from .portfolio import Portfolio
from .orderbook import OrderBook
from ._types import Order, OrderSide, OrderType, OrderState, AssetBalance

@dataclass
class ExchangeEngine:
    """
    High-level façade gluing together *market*, *portfolio* and *order book*.

    No network, no threading – call its methods directly from tests, FastAPI,
    or a small CLI.
    """

    redis_url: str
    cash_asset: str = "USDT"
    commission: float = 0.00075  # 0.075 %

    # boot ------------------------------------------------------------- #
    def __post_init__(self) -> None:
        self.redis: redis.Redis = redis.from_url(self.redis_url, decode_responses=True)
        self.market     = Market(self.redis)
        self.portfolio  = Portfolio(self.redis)
        self.order_book = OrderBook(self.redis)
        self._oid = itertools.count(1)

    # id helper -------------------------------------------------------- #
    def _uid(self) -> str:
        ts = int(time.time())  # seconds
        raw = f"{int(ts*1000)}_{next(self._oid)}".encode()
        hash = base64.urlsafe_b64encode(hashlib.md5(raw).digest())[:6].decode() # Remove padding
        oid = f"{ts:010d}={hash}"
        return oid

    # ------------------------------------------------ balance helpers -- #
    def _reserve(self, asset: str, qty: float) -> None:
        """Move *qty* from free → used."""
        bal = self.portfolio.get(asset)
        if bal.free < qty:
            raise ValueError(f"insufficient {asset} to reserve")
        bal.free -= qty;  bal.used += qty
        self.portfolio.set(bal)

    def _release(self, asset: str, qty: float) -> None:
        """Move *qty* from used → free (called on cancel / fill)."""
        bal = self.portfolio.get(asset)
        if bal.used < qty:                         # sanity
            qty = bal.used
        bal.used -= qty;  bal.free += qty
        self.portfolio.set(bal)

    # ------------------------------------------------ can-execute ------ #
    def _can_execute(self, symbol: str, side: str,
                     amount: float, px: float) -> Tuple[bool, str | None]:
        base, quote = symbol.split("/")
        notion, fee = amount * px, amount * px * self.commission

        if side == "buy":
            have = self.portfolio.get(quote).free
            need = notion + fee
            if have < need:
                return False, f"need {need:.2f} {quote}, have {have:.2f}"
        else:
            have = self.portfolio.get(base).free
            if have < amount:
                return False, f"need {amount:.8f} {base}, have {have:.8f}"
        return True, None

    # ------------------------------------------------ public API ------- #
    def fetch_ticker(self, symbol: str) -> Dict[str, Any]:
        return self.market.fetch_ticker(symbol)

    def fetch_balance(self) -> Dict[str, Any]:
        return {a: b.to_dict() for a, b in self.portfolio.all().items()}

    # ----------------------- ORDER CREATION --------------------------- #
    async def create_order_async(
        self, *,
        symbol: str, side: str,
        type: str, amount: float,
        price: float | None = None,
    ) -> Dict[str, Any]:
        """
        • market  -> write OPEN order, wait 1-2 s, settle, mark CLOSED  
        • limit   -> reserve funds/asset+fee, keep OPEN until price-tick closes it
        """
        if type not in {"market", "limit"}:
            raise ValueError("type must be market | limit")
        if type == "limit" and price is None:
            raise ValueError("limit orders need price")

        px      = price or self.market.fetch_ticker(symbol)["last"]
        base, quote = symbol.split("/")
        notion  = amount * px
        fee     = notion * self.commission

        ok, reason = self._can_execute(symbol, side, amount, px)  # uses latest price
        if not ok:
            raise ValueError(reason)

        # ---------- reservations (always done up-front) --------------------
        if side == "buy":
            self._reserve(quote, notion + fee)
        else:                                  # sell
            self._reserve(base,  amount)
            self._reserve(quote, fee)

        # ---------- write OPEN order to Valkey ----------------------------
        order = Order(
            id=self._uid(), symbol=symbol, side=side, type=type,
            amount=amount, price=px,
            fee_rate=self.commission, fee_cost=fee, fee_currency=quote,
            status="open", filled=0.0,
            ts_post=int(time.time()*1000), ts_exec=None,
        )
        self.order_book.add(order)             # first write

        # ---------- market ⇒ wait & fill ----------------------------------
        if type == "market":
            await asyncio.sleep(random.uniform(1.0, 5.0))       # dev-only latency

            # --- settle ----------------------------------------------------
            if side == "buy":
                # release reservation, then final booking
                self._release(quote, notion + fee)
                cash  = self.portfolio.get(quote); cash.free -= fee
                asset = self.portfolio.get(base);  asset.free += amount
                self.portfolio.set(cash); self.portfolio.set(asset)
            else:  # sell
                self._release(base, amount)
                self._release(quote, fee)             # fee leaves ‘used’
                cash  = self.portfolio.get(quote)
                cash.free += (notion - fee)
                self.portfolio.set(cash)

            # --- flip order to CLOSED -------------------------------------
            order.status  = "closed"
            order.filled  = amount
            order.ts_exec = int(time.time()*1000)
            self.order_book.update(order)             # overwrite

        # ---------- limit ⇒ nothing else (stays OPEN) ---------------------
        return order.__dict__

    # --------------------- LIMIT-FILL TRIGGER ------------------------- #
    def process_price_tick(self, symbol: str) -> None:
        """Call after each price update to check if any OPEN limit hits."""
        ticker = self.market.fetch_ticker(symbol)
        if ticker is None:                  # malformed or missing – just skip
            return
        last = ticker["last"]
        for o in self.order_book.list(status="open", symbol=symbol):
            hit = ((o.side == "buy"  and last <= o.price) or
                   (o.side == "sell" and last >= o.price))
            if not hit:
                continue

            base, quote = symbol.split("/")
            notion = o.amount * o.price
            fee = notion * o.fee_rate

            # release reserved & settle
            if o.side == "buy":
                self._release(quote, notion + fee)          # unlock cash
                cash = self.portfolio.get(quote)
                cash.free -= notion + fee                        # total cost paid
                asset = self.portfolio.get(base)
                asset.free += o.amount
                self.portfolio.set(cash)
                self.portfolio.set(asset)
            else:  # sell
                self._release(base, o.amount)
                cash = self.portfolio.get(quote)
                cash.used  -= o.fee_cost            # fee consumed
                cash.free  += (notion - o.fee_cost) # net proceeds
                self.portfolio.set(cash)
                asset = self.portfolio.get(base)    # nothing more to charge
                asset.free -= o.amount   # ← subtract sold amount
                self.portfolio.set(asset)

            o.status = "closed"; o.filled = o.amount
            o.ts_exec = int(time.time()*1000)
            self.order_book.update(o)

    # ---------------------- admin helpers ----------------------------- #
    def set_balance(self, asset: str, free: float = 0.0, used: float = 0.0) -> None:
        if free < 0 or used < 0:
            raise ValueError("free/used must be ≥ 0")
        self.portfolio.set(AssetBalance(asset, free, used))

    def fund_asset(self, asset: str, amount: float) -> Dict[str, Any]:
        if amount <= 0:
            raise ValueError("amount must be > 0")
        bal = self.portfolio.get(asset); bal.free += amount
        self.portfolio.set(bal)
        return bal.to_dict()

    def reset(self) -> None:
        self.portfolio.clear(); self.order_book.clear()
        self._oid = itertools.count(1)

    # ---------------------- dry-run helper ---------------------------- #
    def can_execute(self, *, symbol: str, side: str,
                    amount: float, price: float | None = None) -> Dict[str, Any]:
        px = price or self.market.fetch_ticker(symbol)["last"]
        ok, reason = self._can_execute(symbol, side, amount, px)
        return {"ok": ok, "reason": reason}
    
