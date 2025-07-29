"""
tests/test_06A_fund_and_reject_insufficient_trade_when_buy.py
======================================

End-to-end check for **reservation roll-back** when the account balance is
manually corrupted *after* an order has been accepted but *before* it can be
filled.

Why this test exists
--------------------
* Every **BUY** order first *locks* the necessary quote currency
  ( notional + fee ) in the portfolio’s `used` column.
* If, in the meantime, something external reduces that balance
  (e.g. a bug, a manual intervention, or concurrent withdrawal),
  the engine **must** detect the shortfall *right before execution* and
  reject the order, releasing whatever was still reserved.

The test follows these steps:

1. **Reset & fund** the account with plenty of USDT.
2. **Create** a BUY-limit order on *BTC/USDT* – this books funds.
3. **Tamper** with the USDT balance so that *used &lt; originally reserved*.
4. **Move** the market price to the limit price – the engine tries to settle.
5. **Assert** that the order ends up *rejected / partially_rejected* and all
   previously-locked USDT is back in `free` while `used` is zero.
"""

# --------------------------------------------------------------------------- #
# Imports & helpers
# --------------------------------------------------------------------------- #
from helpers import reset_and_fund


# --------------------------------------------------------------------------- #
# Test case
# --------------------------------------------------------------------------- #
def test_funding(client):
    """
    Fund → Book → Tamper → Trigger → Cancel.

    Parameters
    ----------
    client : ``pytest`` *fixture*
        A FastAPI test-client pointed at the running mock-exchange.
    """
    # --------------------------------------------------------------------- #
    # 1️⃣  Preparation – fund the quote currency
    # --------------------------------------------------------------------- #
    quote = "USDT"
    asset = "BTC"
    limit_price = 2.0  # USD per BTC
    symbol = f"{asset}/{quote}"

    notional_to_buy = 10_000.0  # USD we intend to spend
    funding_amount = notional_to_buy * 2  # leave plenty of head-room

    # Helper wipes the database, then credits `funding_amount` free USDT
    reset_and_fund(client, quote, funding_amount)

    # Portfolio must now contain exactly that amount
    assert client.get(f"/balance/{quote}").json() == {
        "asset": quote,
        "free": funding_amount,
        "used": 0.0,
        "total": funding_amount,
    }

    # --------------------------------------------------------------------- #
    # 2️⃣  Place the BUY-limit order – this *books* funds
    # --------------------------------------------------------------------- #
    amount_btc = notional_to_buy / limit_price  # BTC @ 2 USD
    order_req = {
        "symbol": symbol,
        "side": "buy",
        "type": "limit",
        "amount": amount_btc,
        "limit_price": limit_price,
    }
    resp = client.post("/orders", json=order_req)
    assert resp.status_code == 200
    order = resp.json()

    # Sanity-check the response
    assert order["status"] == "new"
    assert order["initial_booked_notion"] == notional_to_buy
    fee = order["initial_booked_fee"]
    balance_locked = notional_to_buy + fee

    # The booking must now show up in the portfolio
    assert client.get(f"/balance/{quote}").json() == {
        "asset": quote,
        "free": funding_amount - balance_locked,
        "used": balance_locked,
        "total": funding_amount,
    }

    # --------------------------------------------------------------------- #
    # 3️⃣  Tamper with the balance – simulate disappearing funds
    # --------------------------------------------------------------------- #
    assets_free = 0.0
    # remove 10 % of the fee from what was locked
    assets_used = balance_locked - (fee * 0.1)

    tamper_resp = client.patch(
        f"/admin/balance/{quote}", json={"free": assets_free, "used": assets_used}
    )
    assert tamper_resp.status_code == 200
    tampered = tamper_resp.json()  # keep for final assertions

    # --------------------------------------------------------------------- #
    # 4️⃣  Move the market to the limit price → engine tries to fill
    # --------------------------------------------------------------------- #
    client.patch(f"/admin/tickers/{symbol}/price", json={"price": limit_price})

    # --------------------------------------------------------------------- #
    # 5️⃣  Order must be rejected 
    # --------------------------------------------------------------------- #
    order_after = client.get(f"/orders/{order['id']}").json()
    assert order_after["status"] in {"rejected", "partially_rejected"}

    balances = client.get("/balance").json()
    # Only USDT should exist in the portfolio
    assert list(balances.keys()) == [quote]

    # Locked USDT is back in `free`; nothing remains in `used`
    assert balances[quote]["free"] == tampered["used"]
    assert balances[quote]["used"] == 0.0
    assert balances[quote]["total"] == tampered["free"] + tampered["used"]
