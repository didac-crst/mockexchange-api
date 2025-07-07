"""Adds funds and checks they persist for later tests."""

from helpers import reset_and_fund

def test_funding(client):
    """
    Test the funding of a specific asset.
    """
    asset = "USDT"
    # Fund the USDT asset
    funding_amount = 20_000.0
    reset_and_fund(client, asset, funding_amount)          # wipe + zero fund
    assert client.get(f"/balance/{asset}").json() == {
        "asset": asset, "free": funding_amount, "used": 0.0, "total": funding_amount
    }
