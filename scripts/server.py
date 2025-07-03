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

import os
from typing import List, Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from mockexchange.engine import ExchangeEngine

# ─────────────────────────── Pydantic models ─────────────────────────── #

class OrderReq(BaseModel):
    symbol: str
    side:   Literal["buy", "sell"]
    type:   Literal["market", "limit"] = "market"
    amount: float
    price:  float | None = None      # only for limit orders

class BalanceReq(BaseModel):
    asset: str = Field(..., pattern=r"^[A-Z0-9_\-]+$")
    free:  float = Field(0.0, ge=0)
    used:  float = Field(0.0, ge=0)

class FundReq(BaseModel):
    amount: float = Field(..., gt=0)


# ───────────────────── initialise singleton engine ───────────────────── #

ENGINE = ExchangeEngine(
    redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0")
)

# ───────────────────────────── FastAPI app ───────────────────────────── #

app = FastAPI(title="MockExchange API", version="0.2")

# Helpers: wrap calls so every endpoint is ≤ 3 lines -------------------- #
def _try(fn):
    try:
        return fn()
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))

# Market endpoints ------------------------------------------------------ #
@app.get("/ticker/{symbol:path}")
def ticker(symbol: str):
    return _try(lambda: ENGINE.fetch_ticker(symbol))

@app.get("/balance")
def balance():
    return ENGINE.fetch_balance()

# Orders ---------------------------------------------------------------- #
@app.post("/orders")
def new_order(req: OrderReq):
    return _try(lambda: ENGINE.create_order(**req.model_dump()))

@app.post("/orders/can_execute")
def dry_run(req: OrderReq):
    # `type` is irrelevant for balance check
    return ENGINE.can_execute(
        symbol=req.symbol, side=req.side, amount=req.amount, price=req.price
    )

@app.get("/orders")
def list_orders(
    status: Literal["open", "closed", "canceled"] | None = Query(
        None, description="Filter by order status"
    ),
    symbol: str | None = Query(None, description="BTC/USDT etc."),
):
    return [o.__dict__ for o in ENGINE.order_book.list(status=status, symbol=symbol)]

@app.post("/orders/{oid}/cancel")
def cancel(oid: str):
    o = ENGINE.order_book.get(oid)
    if o.status != "open":
        raise HTTPException(400, "Only *open* orders can be canceled")
    o.status = "canceled"
    o.ts_exec = int(time.time() * 1000)
    ENGINE.order_book.update(o)
    return o.__dict__

# Balance admin --------------------------------------------------------- #
@app.post("/balances")
def set_balance(req: BalanceReq):
    return _try(lambda: ENGINE.set_balance(req.asset, free=req.free, used=req.used))

@app.post("/balances/{asset}/fund")
def fund(asset: str, body: FundReq):
    return _try(lambda: ENGINE.fund_asset(asset, body.amount))

# Destructive admin ----------------------------------------------------- #
@app.post("/admin/reset")
def reset():
    ENGINE.reset()
    return {"status": "ok"}
