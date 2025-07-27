"""
Continuously places random dry-run orders against a MockExchange API.

All tunables are taken from environment variables (see .env.example).
"""

from __future__ import annotations

import os, time, random, httpx
from math import floor, log10
from typing import Final

# ---------- helpers from your own codebase ----------
from helpers import reset_and_fund, get_tickers, place_order, get_last_prices

# ────────────────────────────────────────────────────
# Configuration via ENV (with sensible defaults)
# ────────────────────────────────────────────────────
BASE_URL          = os.getenv("URL_API", "http://localhost:8000")
API_KEY           = os.getenv("API_KEY") or None   # optional
TEST_ENV         = os.getenv("TEST_ENV", "true").lower() in ("true", "1", "yes")

QUOTE             = os.getenv("QUOTE_ASSET", "USDT")
FUNDING_AMOUNT    = float(os.getenv("FUNDING_AMOUNT", 100_000))

BASE_ASSETS_TO_BUY = os.getenv(
    "BASE_ASSETS_TO_BUY",
    "BTC,ETH,SOL,XRP,BNB,ADA,DOGE,DOT",
).split(",")

NUM_EXTRA_ASSETS  = int(os.getenv("NUM_EXTRA_ASSETS", 8))
TRADING_TYPES     = os.getenv("TRADING_TYPES", "market,limit").split(",")
SIDES             = os.getenv("SIDES", "buy,sell").split(",")

MIN_ORDERS_PER_BATCH = int(os.getenv("MIN_ORDERS_PER_BATCH", 1))
MAX_ORDERS_PER_BATCH = int(os.getenv("MAX_ORDERS_PER_BATCH", 3))

MIN_SLEEP = float(os.getenv("MIN_SLEEP", 30))
MAX_SLEEP = float(os.getenv("MAX_SLEEP", 60))

MIN_AMOUNT_RATIO = float(os.getenv("MIN_AMOUNT_RATIO", 0.01))
MAX_AMOUNT_RATIO = float(os.getenv("MAX_AMOUNT_RATIO", 0.05))

# HTTP headers (add API key only if provided)
HEADERS = {"x-api-key": API_KEY} if TEST_ENV else None


# ────────────────────────────────────────────────────
# Utility functions
# ────────────────────────────────────────────────────
def _get_tickers_to_trade(client: httpx.Client) -> list[str]:
    """Return the fixed majors plus *NUM_EXTRA_ASSETS* random tickers."""
    tickers_list = get_tickers(client)
    majors = [f"{a}/{QUOTE}" for a in BASE_ASSETS_TO_BUY]
    extra_pool = [t for t in tickers_list if t not in majors]
    extras = random.sample(extra_pool, min(NUM_EXTRA_ASSETS, len(extra_pool)))
    return majors + extras


def _floor_to_first_sig(x: float) -> float:
    """Round **down** to the most-significant digit only."""
    if x == 0:
        return 0.0
    sign = -1.0 if x < 0 else 1.0
    ax = abs(x)
    d = floor(log10(ax))               # exponent of first sig-digit
    first_digit = floor(ax / 10 ** d)
    return sign * first_digit * 10 ** d


# ────────────────────────────────────────────────────
# Main loop
# ────────────────────────────────────────────────────
def main() -> None:
    with httpx.Client(base_url=BASE_URL, timeout=20.0, headers=HEADERS) as client:
        # Seed the wallet once per container start
        reset_and_fund(client, QUOTE, FUNDING_AMOUNT)

        tickers = _get_tickers_to_trade(client)

        while True:
            last_balances = client.get("/balance").json()

            n_orders = random.randint(MIN_ORDERS_PER_BATCH, MAX_ORDERS_PER_BATCH)
            batch = random.sample(tickers, n_orders)
            last_prices = get_last_prices(client, batch)

            for symbol in batch:
                asset = symbol.split("/")[0]
                side = random.choice(SIDES)

                # Sell only if we actually hold the asset
                if side == "sell" and asset not in last_balances:
                    side = "buy"

                order_type = random.choice(TRADING_TYPES)
                ratio = random.uniform(MIN_AMOUNT_RATIO, MAX_AMOUNT_RATIO)

                if side == "buy":
                    cash_free = last_balances[QUOTE]["free"]
                    notional = cash_free * ratio
                    amount = _floor_to_first_sig(notional / last_prices[symbol])
                else:
                    asset_free = last_balances[asset]["free"]
                    amount = _floor_to_first_sig(asset_free * ratio)

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
                    f"Status: {order['status']} | {order['symbol']} | "
                    f"{order['side']} {order['type']} {order['amount']} "
                    f"@ {order['limit_price']}"
                )

            time.sleep(random.uniform(MIN_SLEEP, MAX_SLEEP))


if __name__ == "__main__":
    main()