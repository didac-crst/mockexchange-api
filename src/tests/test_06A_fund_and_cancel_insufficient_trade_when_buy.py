"""
tests/test_06A_fund_and_reject_insufficient_trade_when_buy.py
======================================

End-to-end check for **reservation roll-back** when the account balance is
manually corrupted *after* an order has been accepted but *before* it can be
filled.

It covers two cases:

* full_reject        – 0 BTC filled → status == rejected
* partial_reject     – some BTC filled → status == partially_rejected

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
# --------------------------------------------------------------------- #
# Imports & helpers
# --------------------------------------------------------------------- #
import time
import pytest

from helpers import reset_and_fund, get_last_price

# ─── test parameters ──────────────────────────────────────────────────
QUOTE          = "USDT"
ASSET          = "BTC"
SYMBOL         = f"{ASSET}/{QUOTE}"
LIMIT_PX       = 2.0                 # 1 BTC ≙ 2 USDT
NOTIONAL       = 10_000.0            # USDT we plan to spend
FUNDING        = NOTIONAL * 2        # plenty of head-room


def _place_buy_limit(client, qty_btc: float):
    """Helper → place BUY-limit, return (order_json, fee, booked_notional)."""
    resp = client.post(
        "/orders",
        json={
            "symbol": SYMBOL,
            "side":   "buy",
            "type":   "limit",
            "amount": qty_btc,
            "limit_price": LIMIT_PX,
        },
    )
    order = resp.json()
    assert resp.status_code == 200 and order["status"] == "new"
    fee     = order["initial_booked_fee"]
    booked  = order["initial_booked_notion"] + fee
    return order, fee, booked


# --------------------------------------------------------------------- #
# single integrated test ------------------------------------------------
# --------------------------------------------------------------------- #
def test_full_and_partial_reject(client):
    # ── 1) ONE reset + funding for the whole function ──────────────────
    reset_and_fund(client, QUOTE, FUNDING)
    assert client.get(f"/balance/{QUOTE}").json()["free"] == FUNDING

    # ===================================================================
    # A) FULL-REJECT   (0 fills → rejected)
    # ===================================================================
    qty_full = NOTIONAL / LIMIT_PX
    ord_full, fee_full, booked_full = _place_buy_limit(client, qty_full)

    # shrink *used* so reservation < required
    new_used = booked_full - fee_full * 0.1          # knock 10 % off the fee
    client.patch(f"/admin/balance/{QUOTE}", json={"free": 0.0, "used": new_used})

    # move price → engine attempts fill, detects shortfall
    client.patch(f"/admin/tickers/{SYMBOL}/price", json={"price": LIMIT_PX})
    time.sleep(0.1)                                  # async settle

    final_full = client.get(f"/orders/{ord_full['id']}").json()
    assert final_full["status"] == "rejected"

    # wallet is back to pristine after reject
    bal = client.get(f"/balance/{QUOTE}").json()
    assert bal["used"] == 0.0 and pytest.approx(bal["free"]) == FUNDING

    # ===================================================================
    # B) PARTIAL-REJECT   (some fills → partially_rejected)
    # ===================================================================
    qty_part = NOTIONAL / LIMIT_PX                    # same size is fine
    ord_part, fee_part, booked_part = _place_buy_limit(client, qty_part)

    # let ~5 % fill first
    client.patch(
        f"/admin/tickers/{SYMBOL}/price",
        json={
            "price": LIMIT_PX,
            "ask_volume": qty_part * 0.05,
            "bid_volume": qty_part * 0.05,
        },
    )
    time.sleep(0.1)                                  # allow partial fill

    # fetch updated order to get new reservations after partial fill
    ord_part_now = client.get(f"/orders/{ord_part['id']}").json()
    residual_fee = ord_part_now["reserved_fee_left"]
    residual_not = ord_part_now["reserved_notion_left"]
    still_locked = residual_fee + residual_not

    # tamper again → make balance insufficient for the *next* fill
    tampered_used = still_locked - residual_fee * 0.1
    client.patch(
        f"/admin/balance/{QUOTE}",
        json={"free": 0.0, "used": tampered_used},
    )

    # trigger another settle attempt
    client.patch(f"/admin/tickers/{SYMBOL}/price", json={"price": LIMIT_PX})
    time.sleep(0.1)

    final_part = client.get(f"/orders/{ord_part['id']}").json()
    assert final_part["status"] == "partially_rejected"

    # all locked funds released again
    bal = client.get(f"/balance/{QUOTE}").json()
    assert bal["used"] == 0.0
    assert pytest.approx(bal["free"]) == FUNDING