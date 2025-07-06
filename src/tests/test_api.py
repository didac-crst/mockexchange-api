"""
High-level black-box tests for the MockExchange FastAPI service.

Every test talks to the HTTP surface only – no direct engine calls.
The suite is designed to run with ``TEST_ENV=true`` so that authentication
is disabled and the interactive docs are exposed.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------
# One global TestClient
# ---------------------------------------------------------------------

os.environ.setdefault("TEST_ENV", "true")          # disable x-api-key auth
BASE_URL = os.getenv("URL_API", "http://localhost:8000/")

from mockexchange_api.server import app  # noqa: E402 (import after env var)

_client = TestClient(app, base_url=BASE_URL)


@pytest.fixture(scope="session")
def client() -> TestClient:
    """
    Shared TestClient for the whole test session.

    Starts each run from a pristine engine via DELETE /admin/data.
    """
    _client.delete("/admin/data")
    yield _client


# ---------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------

def reset_and_fund(client: TestClient, amount: float = 100_000) -> None:
    """Hard reset + preload USDT balance."""
    assert client.delete("/admin/data").status_code == 200
    assert client.post("/admin/fund",
                       json={"asset": "USDT", "amount": amount}).status_code == 200


def patch_ticker_price(client: TestClient, symbol: str, price: float) -> None:
    """
    Hit **PATCH /admin/tickers/{symbol}/price** – idempotent price mutation.
    """
    path = f"/admin/tickers/{symbol}/price"
    assert client.patch(path, json={"price": price}).status_code == 200


def assert_no_locked_funds(client: TestClient, eps: float = 1e-8) -> None:
    """Fail if any asset keeps more than *eps* in `used`."""
    for asset, row in client.get("/balance").json().items():
        assert row["used"] < eps, f"{asset} still locked: {row}"


def place_orders(client: TestClient, payloads: List[Dict[str, Any]]) -> List[str]:
    """POST /orders for each payload; return their IDs."""
    ids: List[str] = []
    for body in payloads:
        r = client.post("/orders", json=body)
        assert r.status_code == 200
        ids.append(r.json()["id"])
    return ids


# ---------------------------------------------------------------------
# 1) Portfolio lifecycle
# ---------------------------------------------------------------------

def test_portfolio_lifecycle(client: TestClient) -> None:
    """reset → fund → patch balance → verify exact row."""
    reset_and_fund(client, 10_000)

    # Patch single row (idempotent)
    body = {"asset": "USDT", "free": 8_000, "used": 2_000}
    assert client.patch("/admin/balance", json=body).status_code == 200

    expected = {"free": 8_000, "used": 2_000}
    assert client.get("/balance/USDT").json() == expected


# ---------------------------------------------------------------------
# 2) Market-data surface
# ---------------------------------------------------------------------

def test_ticker_listing_and_mutation(client: TestClient) -> None:
    """
    * `/tickers` returns a non-empty list  
    * `/tickers/{sym}` echoes the symbol  
    * Price mutation via `PATCH /admin/tickers/{sym}/price` propagates
    """
    symbols = client.get("/tickers").json()
    assert symbols, "exchange returned empty ticker list"

    sym = symbols[0]
    tick0 = client.get(f"/tickers/{sym}").json()

    new_px = tick0["last"] * 1.10
    patch_ticker_price(client, sym, new_px)

    tick1 = client.get(f"/tickers/{sym}").json()
    assert abs(tick1["last"] - new_px) < 1e-8


# ---------------------------------------------------------------------
# 3) Market orders: buy → sell → buy → sell
# ---------------------------------------------------------------------

def test_market_order_flow(client: TestClient) -> None:
    """Round-trip of four *market* orders; no USDT left in `used`."""
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
        time.sleep(0.5)
        patch_ticker_price(client, sym, tx["price"])
        time.sleep(0.5)

    orders = client.get("/orders").json()
    assert len(orders) == 4 and all(o["status"] == "closed" for o in orders)
    assert_no_locked_funds(client)


# ---------------------------------------------------------------------
# 4) Limit orders: place → cancel → place-and-fill
# ---------------------------------------------------------------------

def test_limit_order_lifecycle(client: TestClient) -> None:
    """Ensure reserve/release mechanics for limit-buy orders."""
    reset_and_fund(client, 10_000)

    sym, amt = "BTC/USDT", 0.02
    spot = client.get("/tickers/BTC/USDT").json()["last"]

    # Three limit orders: extreme low (to cancel), near-spot, above-spot
    defs = [
        {"label": "low-cancel", "price": 1,           "expect": "canceled"},
        {"label": "near-open",  "price": spot * 0.99, "expect": "open"},
        {"label": "near-fill",  "price": spot * 1.01, "expect": "closed"},
    ]
    for d in defs:
        d.update(symbol=sym, side="buy", type="limit", amount=amt)

    ids = place_orders(client, defs)

    # Bump price after each placement – mirrors market-order test
    for d in defs:
        patch_ticker_price(client, sym, d["price"])
        time.sleep(0.5)

    # Cancel the unrealistic low order
    low_id = ids[0]
    assert client.post(f"/orders/{low_id}/cancel").status_code == 200

    # Final order-book snapshot & assertions
    book = {o["id"]: o for o in client.get("/orders").json()}
    for d, oid in zip(defs, ids):
        assert book[oid]["status"] == d["expect"], f"{d['label']} wrong state"

    assert_no_locked_funds(client)

    # Free balance = initial – fill_cost (tiny epsilon)
    fill_cost = amt * defs[2]["price"]
    usdt = client.get("/balance/USDT").json()
    assert abs(usdt["free"] - (10_000 - fill_cost)) < 1e-6
