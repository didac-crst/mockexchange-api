"""
High-level black-box tests for the MockExchange FastAPI service.

* All HTTP calls go through `TestClient`; we never touch engine internals.
* `TEST_ENV=true` disables API-key auth and exposes `/docs`.
* Each test starts from a clean slate via **DELETE /admin/data**.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

# ───────────────────────── global TestClient ────────────────────────── #

os.environ.setdefault("TEST_ENV", "true")              # disable auth
BASE_URL = os.getenv("URL_API", "http://localhost:8000/")

from mockexchange_api.server import app  # root package changed!  noqa: E402

_client = TestClient(app, base_url=BASE_URL)


@pytest.fixture(scope="session")
def client() -> TestClient:
    """Session-wide client that starts in a pristine state."""
    _client.delete("/admin/data")          # wipe balances + orders
    yield _client


# ───────────────────────────── helpers ──────────────────────────────── #

def reset_and_fund(client: TestClient, amount: float = 100_000) -> None:
    """DELETE everything, then credit *amount* USDT to free balance."""
    assert client.delete("/admin/data").status_code == 200
    assert client.post("/admin/fund",
                       json={"asset": "USDT", "amount": amount}).status_code == 200


def patch_ticker_price(client: TestClient, symbol: str, price: float) -> None:
    """
    **PATCH /admin/tickers/{symbol}/price** – idempotent price mutation.
    """
    path = f"/admin/tickers/{symbol}/price"
    assert client.patch(path, json={"price": price}).status_code == 200


def place_orders(client: TestClient, payloads: List[Dict[str, Any]]) -> List[str]:
    """Submit each payload to **POST /orders**; return the order IDs."""
    ids: List[str] = []
    for body in payloads:
        r = client.post("/orders", json=body)
        assert r.status_code == 200
        ids.append(r.json()["id"])
    return ids


def assert_no_locked_funds(client: TestClient, eps: float = 0.5) -> None:
    """
    Fail if any balance row keeps more than *eps* in `used`.

    A tolerance of 0.5 USDT absorbs the fee-rounding residue (0.25 USDT)
    observed in the market-order round-trip.
    """
    for asset, row in client.get("/balance").json().items():
        assert row["used"] < eps, f"{asset} still locked: {row}"


# ────────────────────────────── tests ───────────────────────────────── #

# 1) Portfolio lifecycle ------------------------------------------------

def test_portfolio_lifecycle(client: TestClient) -> None:
    """reset → fund → patch balance → verify exact row."""
    free = 8_000.0
    used = 2_000.0
    total = free + used
    reset_and_fund(client, total)
    asset = "USDT"
    body = {"free": free, "used": used}
    assert client.patch(f"/admin/balance/{asset}", json=body).status_code == 200

    expected = {"asset": asset, "free": free, "used": used, "total": total}
    assert client.get(f"/balance/{asset}").json() == expected


# 2) Market-data surface -----------------------------------------------

def test_ticker_listing_and_mutation(client: TestClient) -> None:
    """List tickers, fetch one, then mutate its price."""
    syms = client.get("/tickers").json()
    assert syms, "exchange returned empty ticker list"

    sym = syms[0]
    tick0 = client.get(f"/tickers/{sym}").json()

    new_px = tick0["last"] * 1.10
    patch_ticker_price(client, sym, new_px)

    tick1 = client.get(f"/tickers/{sym}").json()
    assert abs(tick1["last"] - new_px) < 1e-8


# 3) Market-order round-trip -------------------------------------------

def test_market_order_flow(client: TestClient) -> None:
    """buy → sell → buy → sell with *market* orders; no residual locks."""
    reset_and_fund(client)

    sym, amt = "BTC/USDT", 0.01
    txs = [
        {"side": "buy",  "price": 50_000},
        {"side": "sell", "price": 80_000},
        {"side": "buy",  "price": 75_000},
        {"side": "sell", "price": 100_000},
    ]

    for tx in txs:
        tx.update(symbol=sym, type="market", amount=amt)
        place_orders(client, [tx])
        time.sleep(0.5)                    # engine latency
        patch_ticker_price(client, sym, tx["price"])
        time.sleep(0.5)

    orders = client.get("/orders").json()
    assert len(orders) == 4 and all(o["status"] == "closed" for o in orders)
    assert_no_locked_funds(client)        # eps = 0.5 USDT


# 4) Limit-order lifecycle ---------------------------------------------

def test_limit_order_lifecycle(client: TestClient) -> None:
    """
    Three limit-buys:

    • very-low  → cancel (remains *open* until we cancel)  
    • near-spot → stays *open* (price not crossed)  
    • above-spot→ fills instantly → *closed*
    """
    reset_and_fund(client, 10_000)

    sym, amt = "BTC/USDT", 0.02
    spot = client.get("/tickers/BTC/USDT").json()["last"]

    defs = [
        {"label": "low-cancel", "price": 1,           "expect": "canceled"},
        {"label": "near-open",  "price": spot * 0.99, "expect": "open"},
        {"label": "near-fill",  "price": spot * 1.01, "expect": "closed"},
    ]
    for d in defs:
        d.update(symbol=sym, side="buy", type="limit", amount=amt)

    ids = place_orders(client, defs)

    # Mutate price **only** for the "near-fill" order so it executes.
    near_fill_price = defs[2]["price"]
    patch_ticker_price(client, sym, near_fill_price)
    time.sleep(0.5)

    # Cancel the unrealistic low order (still open)
    low_id = ids[0]
    assert client.post(f"/orders/{low_id}/cancel").status_code == 200

    # Snapshot & assertions
    book = {o["id"]: o for o in client.get("/orders").json()}
    for d, oid in zip(defs, ids):
        assert book[oid]["status"] == d["expect"], f"{d['label']} wrong state"

    # ── balances ──────────────────────────────────────────────────────
    usdt = client.get("/balance/USDT").json()

    # • open order reserve = notion + fee (commission = 0.001)
    reserve = amt * defs[1]["price"] * (1 + 0.001)
    # allow 1e-4 USDT of fp drift
    assert abs(usdt["used"] - reserve) < 1e-4, "incorrect USDT locked"

    # • total USDT = initial – fill_cost (reserve doesn’t change total)
    fill_cost = amt * near_fill_price * (1 + 0.001)
    expected_total = 10_000 - fill_cost
    assert abs(usdt["total"] - expected_total) < 1e-6, "incorrect USDT total"
