"""
cli.py
~~~~~~

Ultra-simple *command-line* wrapper around :class:`mockexchange.engine.ExchangeEngine`.

Why keep it?
------------
* One-liner smoke tests **without** cURL / Postman
* Quick funding of balances during demos
* Works from **inside** the API container (`docker exec -it … bash`)
"""
from __future__ import annotations

import argparse
import json
import os
import pprint

from mockexchange.engine import ExchangeEngine

# ---------------------------------------------------------------------- #
def main() -> None:
    # 1) CLI definition ------------------------------------------------- #
    p = argparse.ArgumentParser(prog="mockx")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("balance")

    t = sub.add_parser("ticker")
    t.add_argument("symbol")

    o = sub.add_parser("order")
    o.add_argument("symbol")
    o.add_argument("side", choices=["buy", "sell"])
    o.add_argument("amount", type=float)
    o.add_argument("--type", choices=["market", "limit"], default="market")
    o.add_argument("--price", type=float)

    args = p.parse_args()

    # 2) Instantiate engine once --------------------------------------- #
    eng = ExchangeEngine(
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0")
    )

    # 3) Route commands ------------------------------------------------- #
    if args.cmd == "balance":
        pprint.pp(eng.fetch_balance())

    elif args.cmd == "ticker":
        pprint.pp(eng.fetch_ticker(args.symbol))

    elif args.cmd == "order":
        order = eng.create_order(
            symbol=args.symbol,
            side=args.side,
            type=args.type,
            amount=args.amount,
            price=args.price,
        )
        print(json.dumps(order, indent=2))

    else:  # shouldn’t happen thanks to `required=True`
        p.error("unknown command")


if __name__ == "__main__":
    main()
