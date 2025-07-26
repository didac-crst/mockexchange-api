# continuous_order_generator.py

import os, time, random
import httpx
from math import floor, log10
from typing import Final

from helpers import reset_and_fund, get_tickers, place_order, get_last_prices

BASE_URL = os.getenv("URL_API", "http://localhost:8000")
QUOTE = "USDT"
FUNDING_AMOUNT: Final[float] = 100_000
BASE_ASSETS_TO_BUY: Final[list[str]] = [  # majors we accumulate
    "BTC",
    "ETH",
    "SOL",
    "XRP",
    "BNB",
    "ADA",
    "DOGE",
    "DOT",
]
NUM_EXTRA_ASSETS = 12  # additional assets to buy, if available
TRADING_TYPES = ["market", "limit"]  # order types we can place

SIDES = ["buy", "sell"]

# Number of orders to send per batch (randomized between these)
MIN_ORDERS_PER_BATCH = 1
MAX_ORDERS_PER_BATCH = 5
# Sleep interval between batches (in seconds)
MIN_SLEEP = 10.0
MAX_SLEEP = 30.0
# Amount/Balance ratio to use for each order
MIN_AMOUNT_RATIO = 0.01
MAX_AMOUNT_RATIO = 0.05


# Helpers:
def _get_tickers_to_trade(client) -> list[str]:
    # Get the list of defined tickers + the extra ones we want to buy.
    tickers_list = get_tickers(client)
    tickers_to_trade = [f"{a}/{QUOTE}" for a in BASE_ASSETS_TO_BUY]
    extra_tickers = [t for t in tickers_list if t not in tickers_to_trade]
    extra_tickers = random.sample(extra_tickers, NUM_EXTRA_ASSETS)
    tickers_to_trade.extend(extra_tickers)
    return tickers_to_trade


def _floor_to_first_sig(x: float) -> float:
    """
    Round **down** to the most-significant digit, zeroing everything else.

    Examples
    --------
    >>> floor_to_first_sig(1234.5678)   # → 1000.0
    1000.0
    >>> floor_to_first_sig(0.0002543)   # → 0.0002
    0.0002
    >>> floor_to_first_sig(-87.9)       # → -80.0
    -80.0
    """
    if x == 0:
        return 0.0

    sign = -1.0 if x < 0 else 1.0
    ax = abs(x)

    # 10-exponent of the first significant digit: 1234 → 3, 0.000254 → -4
    d = floor(log10(ax))

    # Bring first digit into the units place, floor it, scale back.
    first_digit = floor(ax / 10**d)
    return sign * first_digit * 10**d


def main():
    # create your own client
    with httpx.Client(base_url=BASE_URL, timeout=20.0) as client:
        # seed the wallet once
        reset_and_fund(client, QUOTE, FUNDING_AMOUNT)

        tickers = _get_tickers_to_trade(client)
        while True:
            last_balances = client.get("/balance").json()
            trade_numbers = random.randint(MIN_ORDERS_PER_BATCH, MAX_ORDERS_PER_BATCH)
            tickers_batch = random.sample(tickers, trade_numbers)
            last_prices = get_last_prices(client, tickers_batch)
            for symbol in tickers_batch:
                # pick a random ticker and random payload
                asset = symbol.split("/")[0]
                side = random.choice(SIDES)
                order_type = random.choice(TRADING_TYPES)
                if side == "sell" and asset not in last_balances:
                    # No asset to sell, buy it instead
                    side = "buy"
                random_ratio = random.uniform(MIN_AMOUNT_RATIO, MAX_AMOUNT_RATIO)
                if side == "buy":
                    cash_balance = last_balances[QUOTE]["free"]
                    notional_to_use = cash_balance * random_ratio
                    amount = notional_to_use / last_prices[symbol]
                    amount = _floor_to_first_sig(amount)

                else:
                    asset_balance = last_balances[asset]["free"]
                    amount = asset_balance * random_ratio
                    amount = _floor_to_first_sig(amount)
                limit_price = last_prices[symbol] * random.gauss(1.0, 0.0005)
                order = place_order(
                    client,
                    {
                        "symbol": symbol,
                        "side": side,
                        "type": order_type,
                        "amount": amount,
                        "limit_price": limit_price,
                    },
                )
                print(
                    f"Status: {order['status']} / Asset: {order['symbol']} / Side: {order['side']} / Type: {order['type']} / Amount: {order['amount']} / Notional: {order['initial_booked_notion']} / Limit Price: {order['limit_price']}"
                )
            time.sleep(random.uniform(MIN_SLEEP, MAX_SLEEP))


if __name__ == "__main__":
    main()
