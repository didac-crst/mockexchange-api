"""Verifies that the backend starts in a clean slate."""

from helpers import reset, reset_and_fund, edit_balance

def test_reset(client):
    """
    Aim of this test is to ensure that the backend starts with a clean state.
    It resets the database and checks that no orders or balances exist.
    """
    reset(client)
    assert client.get("/orders").json() == []
    assert client.get("/balance").json() == {}

def test_edit_balance(client):
    """
    After resetting, fund the USDT asset and check that the balance is correct.
    """
    asset = "USDT"
    # Fund the USDT asset
    expected_free = 8_000.0
    expected_used = 2_000.0
    edit_balance(client, asset, expected_free, expected_used)
    # Check that the balance is updated correctly
    assert client.get(f"/balance/{asset}").json() == {
        "asset": asset, "free": expected_free, "used": expected_used, "total": expected_free + expected_used
    }