"""
server.py
~~~~~~~~~

FastAPI façade over :class:`mockexchange.engine.ExchangeEngine`.

*   **No business logic** lives here – we merely translate HTTP ⇆ Python.
*   Designed to run **inside a Docker container** (host-network mode is fine).

Endpoints
---------
GET  /ticker/{symbol}          single ticker  
GET  /balance                  full account snapshot  
POST /orders                   create (market|limit) order  
POST /orders/can_execute       dry-run balance check  
GET  /orders                   list open|closed|canceled  
POST /orders/{oid}/cancel      cancel *open* order  
POST /balances                 create/overwrite balance row  
POST /balances/{asset}/fund    credit asset in *free* column  
POST /admin/reset              wipe balances & orders
"""
from __future__ import annotations

import asyncio, os
from typing import List, Literal
import time

from fastapi import FastAPI, HTTPException, Query, Depends, Header
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from mockexchange.engine import ExchangeEngine
from mockexchange.logging_config import logger

# ─────────────────────────── Pydantic models ─────────────────────────── #

class OrderReq(BaseModel):
    symbol: str
    side:   Literal["buy", "sell"]
    type:   Literal["market", "limit"] = "market"
    amount: float
    price:  float | None = None      # only for limit orders

class BalanceReq(BaseModel):
    asset: str = "USDT"
    free:  float = Field(100000.0, ge=0)
    used:  float = Field(0.0, ge=0)

class FundReq(BaseModel):
    asset: str = "USDT"
    amount: float = Field(1.0, gt=0)


# ───────────────────── initialise singleton engine ───────────────────── #

REFRESH_S = int(os.getenv("TICK_LOOP_SEC", "10"))
TEST_ENV = os.getenv("TEST_ENV", "FALSE").lower() in ("1", "true", "yes")
API_KEY = os.getenv("API_KEY", "invalid-key")  # default is invalid key

async def verify_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API Key")

prod_depends = [Depends(verify_key)] if not TEST_ENV else []

ENGINE = ExchangeEngine(
    redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    commission= float(os.getenv("COMMISSION", "0.001")),  # 0.1% default
)

# ───────────────────────────── FastAPI app ───────────────────────────── #

app = FastAPI(title="MockExchange API",
                version="0.2",
                description="A mock exchange API for testing purposes",
                swagger_ui_parameters={
                    "tryItOutEnabled": True,  # enable "Try it out" button
                },
                docs_url="/docs" if TEST_ENV else None,  # disable in production
            )

# Helpers: wrap calls so every endpoint is ≤ 3 lines -------------------- #
def _try(fn):
    try:
        return fn()
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    
# ─────────────────────────────── Endpoints ───────────────────────────── #

# Default root endpoint --------------------------------------------- #
@app.get("/", include_in_schema=False)
def root() -> dict:
    return RedirectResponse(url="/docs")

# Market endpoints ------------------------------------------------------ #
@app.get("/symbols", tags=["Market"])
def symbols() -> List[str]:
    """
    List all known symbols.

    Returns a list of strings, e.g. ``["BTC/USDT", "ETH/USDT"]``.
    """
    return ENGINE.symbols

@app.get("/ticker", tags=["Market"])
def ticker(
    symbol: str = Query(
        ...,                     # “...”  = required
        description="Symbol to fetch, e.g. BTC/USDT"
    )
):
    return _try(lambda: ENGINE.fetch_ticker(symbol))

# Balance endpoints --------------------------------------------------- #
@app.get("/balance", tags=["Portfolio"])
def balance():
    return ENGINE.fetch_balance()

# Orders ---------------------------------------------------------------- #
@app.get("/orders", tags=["Orders"])
def list_orders(
    status: Literal["open", "closed", "canceled"] | None = Query(
        None, description="Filter by order status"
    ),
    symbol: str | None = Query(None, description="BTC/USDT etc."),
    tail: int | None = Query(
        None, description="Number of orders to return"
    ),
):
    return [o.__dict__ for o in ENGINE.order_book.list(status=status, symbol=symbol, tail=tail)]

@app.post("/orders", tags=["Orders"], dependencies=prod_depends)
async def new_order(req: OrderReq):
    try:
        return await ENGINE.create_order_async(**req.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))

@app.post("/orders/can_execute", tags=["Orders"])
def dry_run(req: OrderReq):
    # `type` is irrelevant for balance check
    return ENGINE.can_execute(
        symbol=req.symbol, side=req.side, amount=req.amount, price=req.price
    )

@app.post("/orders/cancel", tags=["Orders"], dependencies=prod_depends)
def cancel(
    oid: str = Query(..., description="Order ID to cancel")
):
    """Cancel an *open* order by its ID."""
    o = ENGINE.order_book.get(oid)

    if o.status != "open":
        raise HTTPException(400, "Only *open* orders can be canceled")

    base, quote = o.symbol.split("/")

    # -- Release reserved amounts based on order type and side
    if o.side == "buy":
        # release reserved quote (notion + fee)
        released_base = 0.0
        released_quote = o.amount * o.price + o.fee_cost
        ENGINE._release(quote, released_quote)

    else:  # sell
        # release reserved base (asset) and quote (fee)
        released_base = o.amount
        released_quote = o.fee_cost
        ENGINE._release(base, released_base)
        ENGINE._release(quote, released_quote)

    # -- Cancel order
    o.status = "canceled"
    o.ts_exec = int(time.time() * 1000)
    ENGINE.order_book.update(o)

    return {
        "canceled_order": o.__dict__,
        "freed": {
            base: released_base,
            quote: released_quote,
        }
    }

# Balance admin --------------------------------------------------------- #
@app.post("/admin/edit_balance", tags=["Admin"], dependencies=prod_depends)
def set_balance(req: BalanceReq):
    return _try(lambda: ENGINE.set_balance(req.asset, free=req.free, used=req.used))

@app.post("/admin/fund", tags=["Admin"], dependencies=prod_depends)
def fund(body: FundReq):
    return _try(lambda: ENGINE.fund_asset(body.asset, body.amount))

@app.post("/admin/reset", tags=["Admin"], dependencies=prod_depends)
def reset():
    ENGINE.reset()
    return {"status": "ok", "message": "All balances and orders have been reset."}

# ------------------------------------------------------------------------- #
async def tick_loop() -> None:
    """
    Background **coroutine** that keeps the order-book honest.

    Steps performed forever:

    1.  `SCAN` the Valkey/Redis instance for keys that start with
        ``sym_`` (that’s how prices are stored, e.g. ``sym_BTC/USDT``).
        The cursor-based iterator is very cheap and non-blocking.
    2.  For each symbol call :py:meth:`ExchangeEngine.process_price_tick`.
        The engine will:
            • Inspect every *OPEN* limit order for *that* symbol.  
            • If the last traded price crosses the limit, it settles the
              funds, marks the order *closed* and updates balances.
    3.  Sleep ``REFRESH_S`` seconds (default **10 s** – configurable via
        the ``TICK_LOOP_SEC`` env-var) and repeat.

    Notes
    -----
    * **Why not Pub/Sub?**  
      The loop is dead-simple and good enough for an emulator.  
      When you already publish real-time prices through Redis channels
      you can swap this loop for a subscriber that calls
      ``process_price_tick(symbol)`` **only when** a new price arrives.
    * **Error handling:**  
      If a key disappears between *scan* and *get*, Valkey raises
      ``ValueError``.  We swallow it because that race is harmless.
    """
    while True:
        # ❶ Iterate *once* over all known symbols in Valkey
        for key in ENGINE.redis.scan_iter("sym_*"):
            symbol = key[4:]                   # strip the 'sym_' prefix
            try:
                ENGINE.process_price_tick(symbol)
            except RuntimeError as exc:
                logger.warning("ticker-loop skipped %s: %s", symbol, exc)

        # ❷ Park the coroutine; lets FastAPI handle other requests
        await asyncio.sleep(REFRESH_S)


@app.on_event("startup")
async def _boot_loop() -> None:
    """
    FastAPI lifecycle hook.

    At application start-up we *detach* the price-tick coroutine.  The
    task lives as long as the Uvicorn worker lives and does **not**
    block the main event-loop.
    """
    asyncio.create_task(tick_loop())
