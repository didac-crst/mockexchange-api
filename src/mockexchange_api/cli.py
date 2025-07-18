# cli.py  ── HTTP façade over the FastAPI service
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict

import httpx   # pip install httpx

# ────────────────────────────── Config ─────────────────────────────── #
API_URL = os.getenv("API_URL", "http://localhost:8000")
API_KEY = os.getenv("API_KEY", "invalid-key")
TIMEOUT = float(os.getenv("API_TIMEOUT_SEC", "10"))

HEADERS = {"x-api-key": API_KEY}  # added to every request

client = httpx.Client(base_url=API_URL, headers=HEADERS, timeout=TIMEOUT)


# ───────────────────────────── Helpers ─────────────────────────────── #
def _get(path: str, **params):
    # drop keys whose value is None to avoid sending e.g. status=&tail=
    r = client.get(path, params={k: v for k, v in params.items() if v is not None})
    _raise_for_status(r)
    return r.json()


def _post(path: str, payload: Dict[str, Any] | None = None):
    r = client.post(path, json=payload or {})
    _raise_for_status(r)
    return r.json()


def _patch(path: str, payload: Dict[str, Any]):
    r = client.patch(path, json=payload)
    _raise_for_status(r)
    return r.json()


def _delete(path: str):
    r = client.delete(path)
    _raise_for_status(r)
    return r.json()


def _raise_for_status(r: httpx.Response) -> None:
    if r.is_success:
        return
    # Bubble up FastAPI details if JSON, otherwise raw text
    try:
        detail = r.json().get("detail", r.text)
    except ValueError:           # body not JSON (or empty)
        detail = r.text or r.reason_phrase
    sys.exit(f"HTTP {r.status_code}: {detail}")


def pp(obj):
    print(json.dumps(obj, indent=2, sort_keys=True))


# ───────────────────────────── Commands ────────────────────────────── #
def main() -> None:
    p = argparse.ArgumentParser("mockx")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("balance")

    t = sub.add_parser("ticker")
    t.add_argument("symbol", help="BTC/USDT or comma‑separated list")

    o = sub.add_parser("order") # set_order
    o.add_argument("symbol")
    o.add_argument("side", choices=["buy", "sell"])
    o.add_argument("amount", type=float)
    o.add_argument("--type", choices=["market", "limit"], default="market")
    o.add_argument("--price", type=float, dest="limit_price")  # keep param names aligned

    c = sub.add_parser("cancel") # cancel_order
    c.add_argument("order_id", help="ID returned by /orders")

    d = sub.add_parser("orders") # get_orders - what about if no params provided?
    d.add_argument("--status", choices=["open", "closed", "canceled"])
    d.add_argument("--symbol")
    d.add_argument("--side", choices=["buy", "sell"])
    d.add_argument("--tail", type=int)

    # --- admin shortcuts (optional) ----------------------------------- #
    admin = sub.add_parser("fund")
    admin.add_argument("asset")
    admin.add_argument("amount", type=float)

    sub.add_parser("order-get").add_argument("order_id")
    # lean 'orders-simple' for the /list variant
    sub.add_parser("orders-simple")

    can = sub.add_parser("can-exec")
    for arg in ("symbol", "side", "amount"):
        can.add_argument(arg)
    can.add_argument("--price", type=float)

    sb = sub.add_parser("set-balance")
    sb.add_argument("asset")
    sb.add_argument("--free", type=float, required=True)
    sb.add_argument("--used", type=float, default=0.0)

    sp = sub.add_parser("set-price")
    sp.add_argument("symbol")
    sp.add_argument("price", type=float)
    sp.add_argument("--bid-volume", type=float)
    sp.add_argument("--ask-volume", type=float)

    sub.add_parser("reset-data")
    sub.add_parser("health")

    args = p.parse_args()

    match args.cmd:
        case "balance":
            pp(_get("/balance"))

        case "ticker":
            pp(_get(f"/tickers/{args.symbol}"))

        case "order":
            body = {
                "symbol": args.symbol,
                "side": args.side,
                "type": args.type,
                "amount": args.amount,
                "limit_price": args.limit_price,
            }
            pp(_post("/orders", body))

        case "cancel":
            pp(_post(f"/orders/{args.order_id}/cancel"))

        case "orders":
            pp(_get("/orders", status=args.status, symbol=args.symbol,
                    side=args.side, tail=args.tail))

        case "fund":                       # convenience wrapper - should also be admin_fund?
            pp(_post("/admin/fund", {"asset": args.asset, "amount": args.amount}))

        # ---- read one order -----------------------------------------
        case "order-get":
            pp(_get(f"/orders/{args.order_id}"))

        # ---- list simple --------------------------------------------
        case "orders-simple":
            pp(_get("/orders/list"))

        # ---- dry‑run -------------------------------------------------
        case "can-exec":
            body = {
                "symbol": args.symbol,
                "side": args.side,
                "amount": float(args.amount),
                "limit_price": args.price,
                "type": "limit" if args.price else "market",
            }
            pp(_post("/orders/can_execute", body))

        # ---- admin helpers ------------------------------------------
        case "set-balance":
            pp(_patch(f"/admin/balance/{args.asset}",
                      {"free": args.free, "used": args.used}))

        case "set-price":
            payload = {
                "price": args.price,
                "bid_volume": args.bid_volume,
                "ask_volume": args.ask_volume,
            }
            pp(_patch(f"/admin/tickers/{args.symbol}/price", payload))

        case "reset-data":
            pp(_delete("/admin/data"))

        case "health":
            pp(_get("/admin/health"))

        case _:
            p.error("unknown command")


if __name__ == "__main__":
    main()
