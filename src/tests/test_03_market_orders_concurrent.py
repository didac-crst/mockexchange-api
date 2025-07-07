"""Round-trip limit-order creation → cancel → state check."""
import random
import time
from helpers import (
    reset_and_fund, fund, get_tickers, place_orders_parallel, assert_no_locked_funds, get_last_price
)

def test_market_order_flow(client):
    """
    Test the round-trip of market orders.
    This test will:
    1. Reset the backend and fund the USDT asset.
    2. Place a series of market orders to buy and sell various assets.
    3. Verify that all orders are closed and the balances are updated correctly."""
    initial_asset   = "USDT"
    initial_amount  = 100_000.0
    reset_and_fund(client, initial_asset, initial_amount)

    assert client.get(f"/balance/{initial_asset}").json() == {
        "asset": initial_asset, "free": initial_amount,
        "used": 0.0, "total": initial_amount
    }

    # ── prepare tickers & prices ────────────────────────────────
    num_assets      = 30
    notion_tx       = initial_amount / (1.2 * num_assets)
    tickers         = random.sample(get_tickers(client), num_assets)
    # limit_prices    = {"sell": 1_000_000.0, "buy": 0.000001}

    # fund every base asset so SELLs always succeed
    for t in tickers:
        base = t.split("/")[0]
        fund(client, base, initial_amount)

    sides = ["buy", "sell"]
    payloads = []
    record_tx = {}

    # ── build two distinct batches ─────────────────────────────────
    buys, sells = [], []
    record_tx   = {}

    for sym in tickers:
        price = get_last_price(client, sym)
        amt   = notion_tx / price
        buys.append({
            "side":   "buy",  "symbol": sym,
            "type":   "market", "amount": amt,
        })
        sells.append({
            "side":   "sell", "symbol": sym,
            "type":   "market", "amount": amt * 0.9,
        })

    # ── wave 1 : BUYs in parallel ─────────────────────────────────
    buy_resps = place_orders_parallel(client, buys)
    for body, r in zip(buys, buy_resps):
        body |= {"price": r["price"], "id": r["id"]}
        record_tx[r["id"]] = body

    # give the engine time to move funds (latency in create_order_async)
    time.sleep(6)

    # ── wave 2 : SELLs in parallel ───────────────────────────────
    sell_resps = place_orders_parallel(client, sells)
    for body, r in zip(sells, sell_resps):
        body |= {"price": r["price"], "id": r["id"]}
        record_tx[r["id"]] = body

    # ── final assertions ────────────────────────────────────────
    orders = client.get("/orders").json()
    assert len(orders) == len(record_tx)

    for o in orders:
        tx = record_tx[o["id"]]
        assert o["symbol"] == tx["symbol"]
        assert o["amount"] == tx["amount"]
        assert o["side"]   == tx["side"]
        assert o["type"]   == tx["type"]
        assert o["status"] in {"canceled", "closed"}

    assert_no_locked_funds(client)
