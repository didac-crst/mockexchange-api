from __future__ import annotations
import time
from typing import Any, Dict, List
from httpx import Client

# ───────────────────────── helpers ───────────────────────── #

def reset(client: Client) -> None:
    """Delete all data from the backend."""
    client.delete("/admin/data").raise_for_status()

def edit_balance(client: Client, asset: str, free: float, used: float) -> None:
    """Edit the balance of a specific asset."""
    body = {"free": free, "used": used}
    client.patch(f"/admin/balance/{asset}", json=body).raise_for_status()

def reset_and_fund(client: Client, asset:str, amount: float = 100_000) -> None:
    """Reset the backend and fund the asset with a specified amount."""
    reset(client)
    # Fund the asset with the specified amount
    client.post("/admin/fund", json={"asset": asset, "amount": amount}).raise_for_status()

def get_tickers(client: Client) -> List[Dict[str, Any]]:
    """Get the list of tickers."""
    resp = client.get("/tickers")
    resp.raise_for_status()
    return resp.json()

def get_last_price(client: Client, symbol: str) -> float:
    """Get the last price of a ticker."""
    resp = client.get(f"/tickers/{symbol}")
    resp.raise_for_status()
    return resp.json()["last"]

def patch_ticker_price(client: Client, symbol: str, price: float) -> None:
    client.patch(f"/admin/tickers/{symbol}/price",
                 json={"price": price}).raise_for_status()

def place_order(client: Client, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Place a single order and return the response."""
    resp = client.post("/orders", json=payload)
    resp.raise_for_status()
    return resp.json()

def assert_no_locked_funds(client: Client, eps: float = 10**-8) -> None:
    for asset, row in client.get("/balance").json().items():
        assert row["used"] < row["total"] * eps, f"{asset} still locked: {row}"

def engine_latency(t: float = 0.5):
    """Sleep helper to let background tick-loop catch up."""
    time.sleep(t)
