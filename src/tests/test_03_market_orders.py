"""Round-trip a sequence of market orders and leave state for test 04."""
import random
from helpers import reset_and_fund, place_order, assert_no_locked_funds, get_last_price, get_tickers

def test_market_order_flow(client):
    """
    Test the round-trip of market orders.
    This test will:
    1. Reset the backend and fund the USDT asset.
    2. Place a series of market orders to buy and sell various assets.
    3. Verify that all orders are closed and the balances are updated correctly."""
    initial_asset = "USDT"
    initial_amount = 100_000.0
    # Reset the backend and fund the USDT asset
    reset_and_fund(client, initial_asset, initial_amount)
    # Check the initial balance
    assert client.get(f"/balance/{initial_asset}").json() == {
        "asset": initial_asset,
        "free": initial_amount,
        "used": 0.0,
        "total": initial_amount
    }
    # Prepare the market orders
    # and execute them
    num_assets_to_buy = 8
    notion_tx = initial_amount / (1.2 * num_assets_to_buy) # 20% less than the total amount
    tickers = random.sample(get_tickers(client), num_assets_to_buy)
    sides_list = ["buy", "sell"]
    txs = [
        {"side": side, "symbol": f"{ticker}"}
    for side in sides_list for ticker in tickers]
    # Each transaction will be a market order
    record_tx = dict()
    for tx in txs:
        symbol = tx["symbol"]
        price = get_last_price(client, symbol)
        amt = notion_tx / price
        if tx["side"] == "sell":
            amt *= 0.9 # To avoid liquidity issues, sell less than buy
        tx |= {"type": "market", "amount": amt, "symbol": symbol}
        tx_data = place_order(client, tx)
        order_id = tx_data["id"]
        tx |= {"price": tx_data["price"], "id": order_id}
        record_tx[order_id] = tx        

    orders = client.get("/orders").json()
    for o in orders:
        oid = o['id']
        tx = record_tx.get(oid, {})
        if tx:
            assert o["price"] == tx["price"]
            assert o["status"] == "closed"
            assert o["symbol"] == tx["symbol"]
            assert o["amount"] == tx["amount"]
            assert o["side"] == tx["side"]
            assert o["type"] == tx["type"]
    assert len(orders) == len(txs)
    # Patch the ticker prices to simulate market conditions
    assert_no_locked_funds(client)
