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
GET  **/tickers/{ticker}**             → one ticker (e.g. ``BTC/USDT``)

Portfolio
~~~~~~~~~
GET  **/balance**                      → full account snapshot
GET  **/balance/list**                 → list of all assets with balances
GET  **/balance/{asset}**              → asset row only (``free``, ``used``)

Orders
~~~~~~
GET  **/orders**                       → display all orders, optional filters
GET  **/orders/list**                  → list orders, optional filters
GET  **/orders/{oid}**                 → single order by id
POST **/orders**                       → create *market* | *limit* order
POST **/orders/can_execute**           → dry-run balance check
POST **/orders/{oid}/cancel**          → cancel *open* order

Admin
~~~~~
PATCH **/admin/tickers/{ticker}/price** → set ticker price and volumes
PATCH **/admin/edit_balance**           → overwrite or add a balance row
PATCH **/admin/fund**                   → credit an asset’s *free* column
DELETE **/admin/data**                  → wipe balances **and** orders
GET **/admin/health**                  → check service health

Implementation notes
--------------------
* The background *tick-loop* scans keys ``sym_*`` in Redis every
  ``TICK_LOOP_SEC`` seconds and settles limit orders whose prices have
  crossed.
* API docs (`/docs`) and the raw OpenAPI JSON are **disabled in
  production** for safety; they are exposed automatically when
  ``TEST_ENV=true``.

"""

# server.py
from __future__ import annotations

# Standard library imports
import asyncio, os, time, redis, socket
import contextlib
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import List, Literal
from pathlib import Path
from dotenv import load_dotenv

# load environment from your project’s .env
load_dotenv(Path(__file__).parent.parent / ".env")

from pykka import Future
from fastapi import FastAPI, HTTPException, Query, Depends, Header
from pydantic import BaseModel, Field
from mockexchange.engine_actors import start_engine  # NEW import
from mockexchange.logging_config import logger
from mockexchange.constants import ALL_STATUS, OPEN_STATUS, CLOSED_STATUS  # NEW import

_ALL_STATUS = Literal[*ALL_STATUS]  # type alias for all order statuses
_TRADING_SIDES = Literal["buy", "sell"]


# ─────────────────────────── Pydantic models ────────────────────────── #
class OrderReq(BaseModel):
    symbol: str = "BTC/USDT"
    side: _TRADING_SIDES
    type: Literal["market", "limit"] = "market"
    amount: float
    limit_price: float | None = None


class BalanceReq(BaseModel):
    free: float = Field(1.0, ge=0)
    used: float = Field(0.0, ge=0)


class FundReq(BaseModel):
    asset: str = "USDT"
    amount: float = Field(100000.0, gt=0)


class ModifyTickerReq(BaseModel):
    price: float = Field(..., gt=0)
    bid_volume: float | None = None
    ask_volume: float | None = None


# ───────────────────── initialise actor engine ──────────────────────── #
REFRESH_S = int(os.getenv("TICK_LOOP_SEC", "10"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
_r = redis.from_url(REDIS_URL, decode_responses=True)
MY_ID = f"{socket.gethostname()}:{os.getpid()}"
TEST_ENV = os.getenv("TEST_ENV", "FALSE").lower() in ("1", "true", "yes")
API_KEY = os.getenv("API_KEY", "invalid-key")
COMMISSION = float(os.getenv("COMMISSION", "0.0"))
PRUNE_EVERY_SEC = int(float(os.getenv("PRUNE_EVERY_MIN", "60")) * 60)
STALE_AFTER_SEC = int(float(os.getenv("STALE_AFTER_H", "24")) * 3600)
EXPIRE_AFTER_SEC = int(float(os.getenv("EXPIRE_AFTER_H", "24")) * 3600)
SANITY_CHECK_EVERY_SEC = int(float(os.getenv("SANITY_CHECK_EVERY_MIN", 5)) * 60)

ENGINE = start_engine(redis_url=REDIS_URL, commission=COMMISSION)

LOCK_KEY = "engine:leader"
LOCK_TTL = 30  # seconds


# auth dependency
async def verify_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
        raise HTTPException(403, "Invalid API Key")


prod_depends = [] if TEST_ENV else [Depends(verify_key)]


# ───────────────────────────── FastAPI app ──────────────────────────── #
@asynccontextmanager
async def lifespan(app):
    tick_task = asyncio.create_task(tick_loop())
    prune_task = asyncio.create_task(prune_and_expire_loop())
    sanity_task = asyncio.create_task(sanity_loop())
    yield
    for t in (tick_task, prune_task, sanity_task):
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t


app = FastAPI(
    title="MockExchange API",
    version="0.3",
    description="A mock exchange API for testing purposes",
    docs_url=None if not TEST_ENV else "/docs",
    lifespan=lifespan,
    swagger_ui_parameters={
        "tryItOutEnabled": True,  # enable "Try it out" button
    },
)


# helper to unwrap futures ------------------------------------------------ #
def _g(x):
    # return x.get() if hasattr(x, "get") else x
    # Only unwrap actual Pykka futures, leave plain dict/list untouched
    return x.get() if isinstance(x, Future) else x


# ─────────────────────────────── endpoints ───────────────────────────── #
@app.get("/", include_in_schema=False)
def root():
    return {"service": "mockexchange-api", "version": app.version}


# market ----------------------------------------------------------------- #
@app.get("/tickers", tags=["Market"])
def all_tickers():
    return ENGINE.tickers.get()


@app.get("/tickers/{symbols:path}", tags=["Market"])
def ticker(symbols: str = "BTC/USDT"):
    """Return one ticker (str) or many tickers (comma-separated list).

    Examples
    --------
    GET /tickers/BTC/USDT
    GET /tickers/BTC/USDT,ETH/USDT,XRP/USDT
    """
    # split on ',' and strip whitespace
    requested = [s.strip() for s in symbols.split(",") if s.strip()]
    out: dict[str, dict] = {}
    # many symbols → aggregate, but don’t blow up if one is unknown
    for sym in requested:
        try:
            out[sym] = _g(ENGINE.fetch_ticker(sym))
        except ValueError as e:  # unknown or inactive symbol
            out[sym] = {"error": str(e)}
    return out


# portfolio -------------------------------------------------------------- #
@app.get("/balance", tags=["Portfolio"])
def balance():
    return _g(ENGINE.fetch_balance())


@app.get("/balance/list", tags=["Portfolio"])
def balance_list():
    return _g(ENGINE.fetch_balance_list())


@app.get("/balance/{asset}", tags=["Portfolio"])
def asset_balance(asset: str):
    return _g(ENGINE.fetch_balance(asset))


# orders ----------------------------------------------------------------- #
@app.get("/orders", tags=["Orders"])
def list_orders(
    status: _ALL_STATUS | None = Query(None),
    symbol: str | None = None,
    side: _TRADING_SIDES | None = Query(None),
    tail: int | None = None,
    include_history: bool = Query(
        False, description="Include order history in response"
    ),
):
    orders = _g(
        ENGINE.order_book.get().list(
            status=status,
            symbol=symbol,
            side=side,
            tail=tail,
            include_history=include_history,
        )
    )
    return [o.to_dict(include_history=include_history) for o in orders]


@app.get("/orders/list", tags=["Orders"])
def list_orders_simple(
    status: _ALL_STATUS | None = Query(None),
    symbol: str | None = None,
    side: _TRADING_SIDES | None = Query(None),
    tail: int | None = None,
):
    orders = _g(
        ENGINE.order_book.get().list(status=status, symbol=symbol, side=side, tail=tail)
    )
    ids = [o.id for o in orders]
    return {"length": len(ids), "orders": ids}


@app.get("/orders/{oid}", tags=["Orders"])
def get_order(
    oid: str,
    include_history: bool = Query(
        False, description="Include order history in response"
    ),
):  
    try:
        o = _g(ENGINE.order_book.get().get(oid, include_history=include_history))
        return o.to_dict(include_history=include_history)
    except ValueError as e:
        return {"error": str(e)}


@app.post("/orders", tags=["Orders"], dependencies=prod_depends)
def new_order(req: OrderReq):
    """
    Non-blocking for FastAPI: the call runs in the default thread-pool,
    `.get()` blocks only that worker, not the event loop.
    """
    try:
        return _g(ENGINE.create_order_async(**req.model_dump()))
    except ValueError as e:
        return {"error": str(e)}


@app.post("/orders/can_execute", tags=["Orders"])
def dry_run(req: OrderReq):
    return _g(
        ENGINE.can_execute(
            symbol=req.symbol,
            side=req.side,
            amount=req.amount,
            price=req.limit_price,
        )
    )


@app.post("/orders/{oid}/cancel", tags=["Orders"], dependencies=prod_depends)
def cancel(oid: str):
    try:
        return _g(ENGINE.cancel_order(oid))
    except ValueError as e:
        # 400 = client made a bad request (nothing wrong with the server)
        return {"error": str(e)}


# overview --------------------------------------------------------------- #
@app.get("/overview/assets", tags=["Overview"])
def get_summary_assets():
    sum_assets_bal = _g(ENGINE.get_summary_assets())
    return sum_assets_bal


# admin ------------------------------------------------------------------ #
@app.patch(
    "/admin/tickers/{ticker:path}/price", tags=["Admin"], dependencies=prod_depends
)
def patch_ticker_price(ticker: str, body: ModifyTickerReq):
    ts = time.time()
    price = body.price
    bid = ask = price
    dummy_notion = 10**12  # just a large number to ensure liquid volumes
    body.bid_volume = body.bid_volume or dummy_notion / bid
    body.ask_volume = body.ask_volume or dummy_notion / ask
    data = _g(
        ENGINE.set_ticker(
            ticker,
            price,
            ts,
            bid,
            ask,
            body.bid_volume,
            body.ask_volume,
        )
    )
    _g(ENGINE.process_price_tick(ticker))
    return data


@app.patch("/admin/balance/{asset}", tags=["Admin"], dependencies=prod_depends)
def set_balance(asset: str, req: BalanceReq):
    return _g(ENGINE.set_balance(asset, free=req.free, used=req.used))


@app.post("/admin/fund", tags=["Admin"], dependencies=prod_depends)
def fund(req: FundReq):
    return _g(ENGINE.fund_asset(req.asset, req.amount))


@app.delete("/admin/data", tags=["Admin"], dependencies=prod_depends)
def purge_all():
    ENGINE.reset().get()
    return {"status": "ok"}


@app.get("/admin/health", tags=["Admin"])
def health():
    return {"status": "ok"}


# ─────────────────────── background tasks ────────────────────────────── #


def i_am_leader() -> bool:
    # atomic: SET key val NX EX ttl
    # returns True if lock acquired
    got = _r.set(LOCK_KEY, MY_ID, nx=True, ex=LOCK_TTL)
    if got:
        return True
    # already held? renew if it's me
    if _r.get(LOCK_KEY) == MY_ID:
        _r.expire(LOCK_KEY, LOCK_TTL)
        return True
    return False


async def tick_loop():
    while True:
        logger.debug(f"Tick loop started - REFRESH_S: {REFRESH_S} seconds")
        try:
            if i_am_leader():
                for t in ENGINE.tickers.get():
                    ENGINE.process_price_tick(t).get()
                # run every REFRESH_S seconds, so we don't hammer Redis
                logger.debug(f"Tick loop: {REFRESH_S} seconds")
        except Exception as e:
            logger.exception("Error in tick_loop: %s", e)
            # If an error occurs, we log it and continue the loop
        finally:
            # As it is a matter of seconds, we can afford to skip 1 tick
            await asyncio.sleep(REFRESH_S)


async def prune_and_expire_loop():
    prune_age = timedelta(seconds=STALE_AFTER_SEC)
    expire_age = timedelta(seconds=EXPIRE_AFTER_SEC)
    while True:
        logger.debug(f"Prune and expire loop started - PRUNE_EVERY_SEC: {PRUNE_EVERY_SEC} seconds")
        try:
            if i_am_leader():
                ENGINE.prune_orders_older_than(age=prune_age).get()
                ENGINE.expire_orders_older_than(age=expire_age).get()
        except Exception as e:
            logger.exception(f"Error in prune_and_expire_loop: {e}")
            # If an error occurs, we log it and continue the loop
        finally:
            # If an error occurs, we will miss one prune cycle.
            await asyncio.sleep(PRUNE_EVERY_SEC)

async def sanity_loop():
    while True:
        logger.debug(f"Sanity loop started - SANITY_CHECK_EVERY_SEC: {SANITY_CHECK_EVERY_SEC} seconds")
        try:
            if i_am_leader():
                ENGINE.check_consistency().get()
        except Exception as e:
            logger.exception(f"Error in sanity_loop: {e}")
            # If an error occurs, we log it and continue the loop
        finally:
            # If an error occurs, we will miss one prune cycle.
            await asyncio.sleep(SANITY_CHECK_EVERY_SEC)
