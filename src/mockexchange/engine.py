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

MIN_FILL = 1.0  # minimum fill factor for market orders, 1.0 = 100%

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

    def _get_booked_real_amounts(self, amount: float, filled: float, price: float) -> Dict[str, float]:
        """ Calculate booked and real amounts for an order. """
        fee_rate = self.commission
        booked_notion = amount * price
        booked_fee = booked_notion * fee_rate
        real_notion = filled * price
        real_fee = real_notion * fee_rate
        return { "booked_notion": booked_notion, "booked_fee": booked_fee, "real_notion": real_notion, "real_fee": real_fee }

    def _execute_buy(self,
                     base: str,
                     quote: str,
                     amount: float,
                     filled: float,
                     price: float) -> Dict[str, float]:
        """
        Execute a buy order by:
        """
        _real_amounts = self._get_booked_real_amounts(amount, filled, price)
        booked_notion = _real_amounts["booked_notion"]
        booked_fee = _real_amounts["booked_fee"]
        real_notion = _real_amounts["real_notion"]
        real_fee = _real_amounts["real_fee"]
        # Release reserved quote (notion + fee)
        # and reduces cash from quote balance
        self._release(quote, booked_notion + booked_fee)
        cash = self.portfolio.get(quote)
        cash.free -= (real_notion + real_fee)
        self.portfolio.set(cash)
        # Increase asset amount in portfolio
        asset = self.portfolio.get(base)
        asset.free += filled
        self.portfolio.set(asset)
        transaction_info = {
            "price": price,
            "notion": real_notion,
            "filled": filled,
            "fee": real_fee,
        }
        return transaction_info
    
    def _execute_sell(self,
                      base: str,
                      quote: str,
                      amount: float,
                      filled: float,
                      price: float) -> Dict[str, float]:
        """
        Execute a sell order by:
        """
        _real_amounts = self._get_booked_real_amounts(amount, filled, price)
        booked_fee = _real_amounts["booked_fee"]
        real_notion = _real_amounts["real_notion"]
        real_fee = _real_amounts["real_fee"]
        # Release reserved base (asset)
        # and reduces asset amount in portfolio
        self._release(base, amount)
        asset = self.portfolio.get(base)
        asset.free -= filled   # subtract sold amount
        self.portfolio.set(asset)
        # Release reserved fee (quote)
        # and increases cash in quote balance
        self._release(quote, booked_fee)
        cash = self.portfolio.get(quote)
        cash.free -= real_fee
        cash.free += real_notion
        self.portfolio.set(cash)
        transaction_info = {
            "price": price,
            "notion": real_notion,
            "filled": filled,
            "fee": real_fee,
        }
        return transaction_info

    @staticmethod
    def _filled_amount(amount: float, min_fill: float = 1.0) -> float:
        """
        Returns the filled amount based on a random factor.
        """
        return amount * random.uniform(min_fill, 1.0)

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
        ticker = self.market.fetch_ticker(symbol)
        if ticker is None:
            raise ValueError(f"Ticker for {symbol} not available")
        return ticker

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

        px      = price or self.market.last_price(symbol)
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
        _price = None if type == "market" else price
        order = Order(
            id=self._uid(), symbol=symbol, side=side, type=type,
            amount=amount, price=_price, notion_currency=quote,
            fee_rate=self.commission, fee_currency=quote,
            status="open", filled=0.0,
            ts_post=int(time.time()*1000), ts_exec=None,
        )
        self.order_book.add(order)             # first write

        # ---------- market ⇒ wait & fill ----------------------------------
        if type == "market":
            await asyncio.sleep(random.uniform(1.0, 5.0))       # dev-only latency
            price = self.market.last_price(symbol)
            # --- settle ----------------------------------------------------
            if side == "buy":
                transaction_info = self._execute_buy(
                    base=base,
                    quote=quote,
                    amount=amount,
                    filled=self._filled_amount(amount, MIN_FILL),
                    price=price,
                )
            else:  # sell
                transaction_info = self._execute_sell(
                    base=base,
                    quote=quote,
                    amount=amount,
                    filled=self._filled_amount(amount, MIN_FILL),
                    price=price,
                )

            # --- flip order to CLOSED -------------------------------------
            order.status  = "closed"
            order.price   = transaction_info["price"]
            order.filled  = transaction_info["filled"]
            order.notion  = transaction_info["notion"]
            order.fee_cost = transaction_info["fee"]
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
            price = o.price
            if price is None:  # market order
                raise ValueError("Cannot process market orders in process_price_tick()")

            # release reserved & settle
            if o.side == "buy":
                transaction_info = self._execute_buy(
                    base=base,
                    quote=quote,
                    amount=o.amount,
                    filled=self._filled_amount(o.amount, MIN_FILL),
                    price=price,
                )
            else:  # sell
                transaction_info = self._execute_sell(
                    base=base,
                    quote=quote,
                    amount=o.amount,
                    filled=self._filled_amount(o.amount, MIN_FILL),
                    price=price,
                )

            o.status = "closed"
            o.price = transaction_info["price"]
            o.notion = transaction_info["notion"]
            o.fee_cost = transaction_info["fee"]
            o.filled = transaction_info["filled"]
            o.ts_exec = int(time.time()*1000)
            self.order_book.update(o)

    # ---------------------- admin helpers ----------------------------- #
    def set_balance(self, asset: str, free: float = 0.0, used: float = 0.0) -> Dict[str, Any]:
        if free < 0 or used < 0:
            raise ValueError("free/used must be ≥ 0")
        self.portfolio.set(AssetBalance(asset, free, used))
        return self.portfolio.get(asset).to_dict()

    def fund_asset(self, asset: str, amount: float) -> Dict[str, Any]:
        if amount <= 0:
            raise ValueError("amount must be > 0")
        bal = self.portfolio.get(asset)
        bal.free += amount
        self.portfolio.set(bal)
        return bal.to_dict()

    def reset(self) -> None:
        self.portfolio.clear(); self.order_book.clear()
        self._oid = itertools.count(1)

    # ---------------------- dry-run helper ---------------------------- #
    def can_execute(self, *, symbol: str, side: str,
                    amount: float, price: float | None = None) -> Dict[str, Any]:
        px = price or self.market.last_price(symbol)
        ok, reason = self._can_execute(symbol, side, amount, px)
        return {"ok": ok, "reason": reason}
    
