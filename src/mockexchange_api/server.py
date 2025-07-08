"""
server.py
~~~~~~~~~

FastAPI façade over :class:`mockexchange.engine.ExchangeEngine`.

* **No business logic** lives here – we only translate *HTTP ⇄ Python*.
* Designed to run **inside a Docker container** (host-network is fine).
* All state (balances, orders, tick data) persists in Valkey/Redis.
* **Authentication**  
  Every request must include  
  ``x-api-key: $API_KEY`` unless the container is started with  
  ``TEST_ENV=true`` (integration tests).

Environment variables
---------------------
API_KEY required key for every request (default: "invalid-key")
REDIS_URL redis://host:port/db (default: localhost:6379/0)
COMMISSION trading fee, e.g. 0.001 (default: 0.001 = 0.1 %)
TICK_LOOP_SEC price-tick scan interval (default: 10 s)
TEST_ENV set to 1 / true to disable auth & expose /docs

HTTP Endpoints
--------------
Market data
~~~~~~~~~~~
GET  **/tickers**                      → list of all symbols  
GET  **/tickers/{symbol}**             → one ticker (e.g. ``BTC/USDT``)

Portfolio
~~~~~~~~~
GET  **/balance**                      → full account snapshot  
GET  **/balance/{asset}**              → asset row only (``free``, ``used``)

Orders
~~~~~~
GET  **/orders**                       → list orders, optional filters  
GET  **/orders/{oid}**                 → single order by id  
POST **/orders**                       → create *market* | *limit* order  
POST **/orders/can_execute**           → dry-run balance check  
POST **/orders/{oid}/cancel**          → cancel *open* order

Admin
~~~~~
POST **/admin/edit_balance**           → overwrite or add a balance row  
POST **/admin/fund**                   → credit an asset’s *free* column  
POST **/admin/reset**                  → wipe balances **and** orders

Implementation notes
--------------------
* The background *tick-loop* scans keys ``sym_*`` in Redis every
  ``TICK_LOOP_SEC`` seconds and settles limit orders whose prices have
  crossed.
* API docs (`/docs`) and the raw OpenAPI JSON are **disabled in
  production** for safety; they are exposed automatically when
  ``TEST_ENV=true``.

"""
from __future__ import annotations

import asyncio, os
from typing import List, Literal
import time
from datetime import timedelta           #  ← already needed later

from fastapi import FastAPI, HTTPException, Query, Depends, Header
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from contextlib import asynccontextmanager

from mockexchange.engine import ExchangeEngine
from mockexchange.logging_config import logger

# ─────────────────────────── Pydantic models ─────────────────────────── #

class OrderReq(BaseModel):
    symbol: str = "BTC/USDT"
    side:   Literal["buy", "sell"]
    type:   Literal["market", "limit"] = "market"
    amount: float
    limit_price:  float | None = None      # only for limit orders

class BalanceReq(BaseModel):
    free:  float = Field(1.0, ge=0)
    used:  float = Field(0.0, ge=0)

class FundReq(BaseModel):
    asset: str = "USDT"
    amount: float = Field(100000.0, gt=0)

class ModifyTickerReq(BaseModel):
    price:  float = Field(..., gt=0.0, description="New price for the ticker")
    bid_volume: float = Field(
        None,
        description="Optional volume at the bid price; if not provided"
    )
    ask_volume: float = Field(
        None,
        description="Optional volume at the ask price; if not provided"
    )

# ───────────────────── initialise singleton engine ───────────────────── #

REFRESH_S = int(os.getenv("TICK_LOOP_SEC", "10"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
TEST_ENV = os.getenv("TEST_ENV", "FALSE").lower() in ("1", "true", "yes")
API_KEY = os.getenv("API_KEY", "invalid-key")  # default is invalid key
COMMISSION = float(os.getenv("COMMISSION", "0.0"))  # 0.0% default
PRUNE_EVERY_SEC  = int(os.getenv("PRUNE_EVERY_SEC",  "3600"))     # run job every 1 hour
STALE_AFTER_SEC  = int(os.getenv("STALE_AFTER_SEC", "86400"))    # delete >24 h old

async def verify_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API Key")

prod_depends = [Depends(verify_key)] if not TEST_ENV else []

ENGINE = ExchangeEngine(
    redis_url=REDIS_URL,
    commission=COMMISSION,
)

# ───────────────────────────── FastAPI app ───────────────────────────── #

@asynccontextmanager
async def lifespan(app):
    # start background task
    tick_task = asyncio.create_task(tick_loop())
    prune_task = asyncio.create_task(prune_loop())
    yield                     # <-- application runs here
    for t in (tick_task, prune_task):
        t.cancel()

app = FastAPI(title="MockExchange API",
                version="0.2",
                description="A mock exchange API for testing purposes",
                swagger_ui_parameters={
                    "tryItOutEnabled": True,  # enable "Try it out" button
                },
                docs_url="/docs" if TEST_ENV else None,  # disable in production
                lifespan=lifespan,
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
def root() -> dict[str, str]:
    return {"service": "mockexchange-api", "version": app.version}

# Market endpoints ------------------------------------------------------ #
@app.get("/tickers", tags=["Market"])
def all_tickers() -> List[str]:
    """
    List all known tickers.

    Returns a list of strings, e.g. ``["BTC/USDT", "ETH/USDT"]``.
    """
    return ENGINE.tickers

@app.get("/tickers/{ticker:path}", tags=["Market"])
def ticker(ticker: str = "BTC/USDT"):
    """ Get a single ticker.

    :param ticker: Symbol, e.g. BTC/USDT, ETH/USDT.
    """
    return _try(lambda: ENGINE.fetch_ticker(ticker))

# Balance endpoints --------------------------------------------------- #
@app.get("/balance", tags=["Portfolio"])
def balance():
    return ENGINE.fetch_balance()

@app.get("/balance/{asset}", tags=["Portfolio"])
def asset_balance(asset: str):
    """
    Get the balance of a specific asset.

    :param asset: Asset symbol, e.g. BTC, USDT.
    """
    return _try(lambda: ENGINE.fetch_balance(asset))

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

@app.get("/orders/{oid}", tags=["Orders"])
def get_order(oid: str):
    return _try(lambda: ENGINE.order_book.get(oid))

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

@app.post("/orders/{oid}/cancel", tags=["Orders"], dependencies=prod_depends)
def cancel(oid: str):
    return _try(lambda: ENGINE.cancel_order(oid))

# Balance admin --------------------------------------------------------- #
@app.patch("/admin/tickers/{ticker:path}/price", tags=["Admin"], dependencies=prod_depends)
def patch_ticker_price(ticker: str, body: ModifyTickerReq):
    """
    Update the last-trade price (and optional volumes) for *ticker*.

    Idempotent: sending the same payload twice leaves the ticker unchanged.
    """
    ts = time.time() / 1000  # current time in seconds since epoch
    notion_for_volume = 100_000 # 100k USD is the default volume
    price = body.price
    bid = price
    ask = price
    if body.bid_volume is None or body.bid_volume <= 0:
        body.bid_volume = notion_for_volume / bid if bid > 0 else 0.0
    if body.ask_volume is None or body.ask_volume <= 0:
        body.ask_volume = notion_for_volume / ask if ask > 0 else 0.0
    data = _try(
        lambda: ENGINE.set_ticker(
            ticker, price, ts, bid, ask, body.bid_volume, body.ask_volume
        )
    )
    try:
        logger.info("Manually updating ticker %s", ticker)
        # Process the price tick to update orders and balances
        ENGINE.process_price_tick(ticker)
    except Exception as exc:
        logger.warning("Failed to process price tick for %s: %s", ticker, exc)
    return data

@app.patch("/admin/balance/{asset}", tags=["Admin"], dependencies=prod_depends)
def set_balance(asset: str, req: BalanceReq):
    """Overwrite or create a balance row (idempotent)."""
    return _try(lambda: ENGINE.set_balance(asset, free=req.free, used=req.used))

@app.post("/admin/fund", tags=["Admin"], dependencies=prod_depends)
def fund(body: FundReq):
    return _try(lambda: ENGINE.fund_asset(body.asset, body.amount))

@app.delete("/admin/data", tags=["Admin"], dependencies=prod_depends)
def purge_all():
    """Wipe **all** balances and orders."""
    ENGINE.reset()
    return {"status": "ok", "message": "All balances and orders have been reset."}

# --- Health check ------------------------------------------------------- #
@app.get("/admin/healthz", include_in_schema=False)
def health():
    return {"status": "ok"}

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
      ``process_price_tick(ticker)`` on every message.
    """
    while True:
        logger.debug(f"[tick_loop] scanning @ {time.strftime('%X')}")
        # ❶ Iterate *once* over all known symbols in Valkey
        for ticker in ENGINE.tickers:
            try:
                ENGINE.process_price_tick(ticker)
            except Exception as exc:
                logger.warning("ticker-loop skipped %s: %s", ticker, exc)

        # ❷ Park the coroutine; lets FastAPI handle other requests
        await asyncio.sleep(REFRESH_S)

async def prune_loop() -> None:
    """
    Periodically purge stale *closed / canceled* orders from Redis.

    * Age limit   = STALE_AFTER_SEC   (default 24 h)
    * Run period  = PRUNE_EVERY_SEC   (default 10 min)
    """
    age = timedelta(seconds=STALE_AFTER_SEC)
    while True:
        try:
            removed = ENGINE.prune_orders_older_than(age=age)
            if removed:
                logger.info("[prune_loop] removed %d stale orders", removed)
        except Exception as exc:
            logger.warning("[prune_loop] failed: %s", exc)
        await asyncio.sleep(PRUNE_EVERY_SEC)
