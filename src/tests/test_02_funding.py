"""
tests/test_02_funding.py
------------------------

Smoke-test for the **funding** helper.

Goal
~~~~
1.  Ensure that the convenience wrapper ``reset_and_fund`` really clears all
    state (orders *and* balances) before crediting an asset.
2.  Confirm that the credited amount lands in ``free`` (spendable funds) while
    ``used`` remains 0 — this establishes a predictable starting point for
    subsequent tests that will place orders and lock balances.

This is deliberately minimal: if it fails, later scenarios that rely on a clean
portfolio will be unreliable, so we surface the problem early.
"""

# --------------------------------------------------------------------------- #
# Imports & helpers
# --------------------------------------------------------------------------- #
from .helpers import reset_and_fund


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_funding(client):
    """
    Credit **USDT** and verify the resulting balance row.

    Parameters
    ----------
    client : starlette.testclient.TestClient
        Fixture that issues HTTP calls against the running FastAPI app.

    Procedure
    ---------
    1. Call :pyfunc:`helpers.reset_and_fund` – this helper does two things:
       a. ``DELETE /admin/data`` → wipes *every* order and balance.
       b. ``POST   /admin/fund`` → credits the requested amount to *free*.
    2. Fetch the balance row back and assert that:

       * ``free``  == *funding_amount*
       * ``used``  == 0  – no reservations yet
       * ``total`` == *free + used*
    """
    asset = "USDT"
    funding_amount = 20_000.0  # the amount we expect to end up in `free`

    # Helper ensures a clean slate, then credits the account.
    reset_and_fund(client, asset, funding_amount)

    # Verify that the backend returns the exact expected structure.
    expected_balance = {
        "asset": asset,
        "free": funding_amount,
        "used": 0.0,
        "total": funding_amount,
    }
    assert client.get(f"/balance/{asset}").json() == expected_balance
