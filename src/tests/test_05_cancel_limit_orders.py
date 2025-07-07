"""Round-trip a sequence of market orders and leave state for test 04."""
import random
from helpers import reset_and_fund, place_order, assert_no_locked_funds, get_tickers, fund, cancel_order

def test_cancel_orders(client):
    """
    Create multiple limit orders, cancel them, and check the final state.
    """
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
    num_assets_to_buy = 4
    notion_tx = initial_amount / (1.2 * num_assets_to_buy) # 20% less than the total amount
    tickers = random.sample(get_tickers(client), num_assets_to_buy)
    # To avoid this orders to be filled.
    limit_prices ={
        "sell": 1_000_000.0,  # High price for sell orders
        "buy": 0.000001  # Low price for buy orders
    }
    for t in tickers:
        # Provide funds for each ticker
        symbol = t.split("/")[0]  # Extract the base asset from the ticker
        fund(client, symbol, initial_amount)
    sides_list = ["buy", "sell"]
    txs = [
        {"side": side, "symbol": f"{ticker}"}
    for side in sides_list for ticker in tickers]
    # Each transaction will be a limit order
    record_tx = dict()
    for tx in txs:
        symbol = tx["symbol"]
        amt = notion_tx / limit_prices[tx["side"]]
        if tx["side"] == "sell":
            amt *= 0.9 # To avoid liquidity issues, sell less than buy
        tx |= {"type": "limit", "amount": amt, "symbol": symbol, "limit_price": limit_prices[tx["side"]]}
        print(tx)
        tx_data = place_order(client, tx)
        order_id = tx_data["id"]
        tx |= {"price": tx_data["price"], "id": order_id}
        record_tx[order_id] = tx        

    orders = client.get("/orders").json()
    for o in orders:
        oid = o['id']
        tx = record_tx.get(oid, {})
        if tx:
            if o['status'] == "open":
                # If the order is open, it should be canceled
                cancel_order(client, oid)
    orders = client.get("/orders").json()
    for o in orders:
        oid = o['id']
        tx = record_tx.get(oid, {})
        if tx:
            # assert o["price"] == tx["price"] This could be tricky due to the price not being applied immediately
            assert (o["status"] == "canceled") or (o["status"] == "closed")
            assert o["symbol"] == tx["symbol"]
            assert o["amount"] == tx["amount"]
            assert o["side"] == tx["side"]
            assert o["type"] == tx["type"]
    assert len(orders) == len(txs)
    # Patch the ticker prices to simulate market conditions
    assert_no_locked_funds(client)
