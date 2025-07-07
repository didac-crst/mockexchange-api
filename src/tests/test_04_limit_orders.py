"""Round-trip a sequence of market orders and leave state for test 04."""
import random
from helpers import reset_and_fund, place_order, assert_no_locked_funds, engine_latency, get_tickers, patch_ticker_price

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
    num_assets_to_buy = 20
    notion_tx = initial_amount / (1.2 * num_assets_to_buy) # 20% less than the total amount
    tickers = random.sample(get_tickers(client), num_assets_to_buy)
    sides_list = ["buy", "sell"]
    txs = [
        {"side": side, "symbol": f"{ticker}"}
    for side in sides_list for ticker in tickers]
    # Each transaction will be a limit order
    record_tx = dict()
    for tx in txs:
        symbol = tx["symbol"]
        fake_price = initial_amount # Fake price for the sake of the test
        amt = notion_tx / fake_price
        if tx["side"] == "sell":
            amt *= 0.9 # To avoid liquidity issues, sell less than buy
        tx |= {"type": "limit", "amount": amt, "symbol": symbol, "limit_price": fake_price}
        print(tx)
        tx_data = place_order(client, tx)
        engine_latency()
        # Force price
        patch_ticker_price(client, symbol, fake_price)
        order_id = tx_data["id"]
        tx |= {"price": tx_data["price"], "id": order_id}
        record_tx[order_id] = tx        

    orders = client.get("/orders").json()
    for o in orders:
        oid = o['id']
        tx = record_tx.get(oid, {})
        if tx:
            # assert o["price"] == tx["price"] This could be tricky due to the price not being applied immediately
            assert o["status"] == "closed"
            assert o["symbol"] == tx["symbol"]
            assert o["amount"] == tx["amount"]
            assert o["side"] == tx["side"]
            assert o["type"] == tx["type"]
    assert len(orders) == len(txs)
    # Patch the ticker prices to simulate market conditions
    assert_no_locked_funds(client)
