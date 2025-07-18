# tests/test_cli.py
import json
import sys
from types import SimpleNamespace

import httpx
import pytest

#
# ——— minimal fake HTTP client ———
#
class DummyClient(SimpleNamespace):
    """
    Records every (method, path, json/params) call and returns the pre‑seeded
    response object in FIFO order.
    """
    def __init__(self, responses):
        super().__init__(responses=list(responses), calls=[])

    # GET / POST / PATCH / DELETE share the same signature in httpx.Client
    def get(self, path, params=None):
        return self._reply("GET", path, params=params)

    def post(self, path, json=None):
        return self._reply("POST", path, json=json)

    def patch(self, path, json=None):
        return self._reply("PATCH", path, json=json)

    def delete(self, path):
        return self._reply("DELETE", path)

    # internal --------------------------------------------------------
    def _reply(self, method, path, **payload):
        self.calls.append((method, path, payload))
        if not self.responses:
            raise RuntimeError("DummyClient: no more stub responses queued")
        return self.responses.pop(0)


#
# ——— helper: run cli.main() with fake argv ———
#
def run_cli(monkeypatch, capsys, fake_client, argv):
    import importlib, inspect, sys

    cli = importlib.import_module("mockexchange_api.cli")
    monkeypatch.setattr(cli, "client", fake_client, raising=True)
    monkeypatch.setattr(sys, "argv", ["mockx", *argv])

    try:
        cli.main()              # ← returns None on success
        code = 0
    except SystemExit as exc:    # ← only raised on error
        code = exc.code

    stdout = capsys.readouterr().out
    return code, stdout, fake_client.calls


# # ————————————————————————————————————————————————————————————————
# #                           Tests
# # ————————————————————————————————————————————————————————————————
# def test_balance_ok(monkeypatch, capsys):
#     data = {"USDT": {"free": 100}}
#     resp = httpx.Response(200, json=data)
#     client = DummyClient([resp])

#     code, out, calls = run_cli(monkeypatch, capsys, client, ["balance"])

#     assert code == 0
#     assert json.loads(out) == data
#     assert calls == [("GET", "/balance", {"params": {}})]


# def test_ticker_routes_correctly(monkeypatch, capsys):
#     resp = httpx.Response(200, json={"dummy": True})
#     client = DummyClient([resp])

#     _, _, calls = run_cli(monkeypatch, capsys, client, ["ticker", "BTC/USDT"])

#     method, path, payload = calls[0]
#     assert (method, path) == ("GET", "/tickers/BTC/USDT")
#     # no empty query params were sent
#     assert payload == {"params": {}}


# def test_cancel_closed_order_shows_error(monkeypatch, capsys):
#     # server replies 400 with FastAPI JSON body
#     error_body = {"detail": "Only *open* orders can be canceled"}
#     resp = httpx.Response(400, json=error_body)
#     client = DummyClient([resp])

#     code, out, _ = run_cli(
#         monkeypatch, capsys, client, ["cancel", "abc123"]
#     )

#     # our CLI exits with the human‑readable message, not with a traceback
#     assert code == "HTTP 400: Only *open* orders can be canceled"
#     assert out == ""  # nothing printed to stdout

cases = [
    # cmd            argv                       method  path                         body/query
    ("balance",      ["balance"],               "GET",  "/balance",                  {}),
    ("ticker",       ["ticker", "BTC/USDT"],    "GET",  "/tickers/BTC/USDT",         {}),
    ("order",        ["order", "BTC/USDT", "buy", "1"], "POST", "/orders",
                     {"symbol": "BTC/USDT", "side": "buy", "type": "market",
                      "amount": 1.0, "limit_price": None}),
    ("orders",       ["orders"],                "GET",  "/orders",                   {}),
    ("fund",         ["fund", "USDT", "100"],   "POST", "/admin/fund",
                     {"asset": "USDT", "amount": 100.0}),
    ("order-get",    ["order-get", "oid123"],   "GET",  "/orders/oid123",            {}),
    ("orders-simple",["orders-simple"],         "GET",  "/orders/list",              {}),
    ("can-exec",     ["can-exec", "BTC/USDT", "buy", "1"],
                                                "POST", "/orders/can_execute",
                     {"symbol": "BTC/USDT", "side": "buy", "amount": 1.0,
                      "limit_price": None, "type": "market"}),
    ("set-balance",  ["set-balance", "DOT", "--free", "10"],
                                                "PATCH","/admin/balance/DOT",
                     {"free": 10.0, "used": 0.0}),
    ("set-price",    ["set-price", "BTC/USDT", "30000"],
                                                "PATCH","/admin/tickers/BTC/USDT/price",
                     {"price": 30000.0, "bid_volume": None, "ask_volume": None}),
    ("reset-data",   ["reset-data"],            "DELETE","/admin/data",              {}),
    ("health",       ["health"],                "GET",  "/admin/health",             {}),
]

@pytest.mark.parametrize("name,argv,method,path,expected", cases)
def test_cli_routes(name, argv, method, path, expected,
                    monkeypatch, capsys):
    # stub HTTP 200 for every call
    fake = DummyClient([httpx.Response(200, json={"ok": True})])
    code, _, calls = run_cli(monkeypatch, capsys, fake, argv)

    assert code == 0

    if method in {"POST", "PATCH"}:
        want = {"json": expected}
    elif method == "GET":
        want = {"params": expected}
    elif method == "DELETE":
        want = {}

    assert calls == [(method, path, want)]