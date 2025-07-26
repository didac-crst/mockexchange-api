# continuous_order_generator.py

from __future__ import annotations

from dotenv import load_dotenv
import os, time, random, requests
from math import floor, log10
from typing import Final
from .conftest import client
from .helpers import (
    reset_and_fund,
    place_order,
    get_tickers,
    get_last_price,
)

# --------------------------------------------------------------------------- #
# Scenario constants – tweak here, not in the test logic
# --------------------------------------------------------------------------- #

# Number of orders to send per batch (randomized between these)
MIN_ORDERS_PER_BATCH = 1
MAX_ORDERS_PER_BATCH = 5
# Sleep interval between batches (in seconds)
MIN_SLEEP = 10.0
MAX_SLEEP = 30.0

QUOTE: Final[str] = "USDT"
FUNDING_AMOUNT: Final[float] = 50_000.0  # initial USDT bankroll
ASSETS_TO_BUY: Final[list[str]] = [  # majors we accumulate
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


# Helper to fetch last price

def floor_to_first_sig(x: float) -> float:
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

def get_tickers_to_trade(client) -> list[str]:
    # Get the list of defined tickers + the extra ones we want to buy.
    tickers_list = get_tickers(client)
    tickers_to_trade = [f"{a}/{QUOTE}" for a in ASSETS_TO_BUY]
    extra_tickers = [t for t in tickers_list if t not in tickers_to_trade]
    extra_tickers = random.sample(extra_tickers, NUM_EXTRA_ASSETS)
    tickers_to_trade.extend(extra_tickers)


# Main loop: continuously generate orders
def main():
    mockX_client = client()

    reset_and_fund(mockX_client, QUOTE, FUNDING_AMOUNT)

    # headers = {}



    # print("Starting order fuzzer. Press Ctrl+C to stop.")
    # try:
    #     while True:
    #         n_orders = random.randint(MIN_ORDERS_PER_BATCH, MAX_ORDERS_PER_BATCH)
    #         print(f"Sending batch of {n_orders} orders...")
    #         for _ in range(n_orders):
    #             symbol = random.choice(ASSETS)
    #             side = random.choice(["buy", "sell"])
    #             price = get_last_price(symbol)
    #             # Allocate a random notion between 10 and 100 units
    #             notion = random.uniform(10, 100)
    #             qty = floor(notion / price) if price > 0 else 1
    #             if qty < 1:
    #                 qty = 1
    #             # Choose type
    #             order_type = random.choice(["market", "limit"])
    #             limit_price = None
    #             if order_type == "limit":
    #                 # set limit slightly favorable
    #                 if side == "buy":
    #                     limit_price = price * random.uniform(0.995, 0.999)
    #                 else:
    #                     limit_price = price * random.uniform(1.001, 1.005)
    #             payload = {
    #                 "symbol": symbol,
    #                 "side": side,
    #                 "type": order_type,
    #                 "amount": qty,
    #                 "limit_price": limit_price,
    #             }
    #             try:
    #                 r = requests.post(f"{API_BASE}/orders", json=payload, headers=headers, timeout=5)
    #                 r.raise_for_status()
    #                 order = r.json()
    #                 print(f"→ Created {order_type.upper()} {side} order: {order['id']} ({qty} @ {limit_price or 'MKT'})")
    #             except Exception as e:
    #                 print(f"! Error placing order: {e}")
    #         sleep_time = random.uniform(MIN_SLEEP, MAX_SLEEP)
    #         print(f"Sleeping {sleep_time:.2f}s before next batch...\n")
    #         time.sleep(sleep_time)
    # except KeyboardInterrupt:
    #     print("Order fuzzer stopped by user.")

if __name__ == "__main__":
    main()