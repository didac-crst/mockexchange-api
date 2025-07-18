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
    """
    monkeypatch: pytest fixture
    capsys:      pytest fixture
    fake_client: DummyClient instance
    argv:        list[str] e.g. ["balance"]
    """
    # import *after* patching so cli.py picks up the monkey‑patched symbols
    import importlib
    cli = importlib.import_module("mockexchange_api.cli")

    # replace the real client with the fake
    monkeypatch.setattr(cli, "client", fake_client, raising=True)

    # simulate `python -m mockexchange_api.cli ...`
    monkeypatch.setattr(sys, "argv", ["mockx", *argv])

    # capture SystemExit (argparse always calls it)
    with pytest.raises(SystemExit) as exc:
        cli.main()

    stdout = capsys.readouterr().out
    return exc.value.code, stdout, fake_client.calls


# ————————————————————————————————————————————————————————————————
#                           Tests
# ————————————————————————————————————————————————————————————————
def test_balance_ok(monkeypatch, capsys):
    data = {"USDT": {"free": 100}}
    resp = httpx.Response(200, json=data)
    client = DummyClient([resp])

    code, out, calls = run_cli(monkeypatch, capsys, client, ["balance"])

    assert code == 0
    assert json.loads(out) == data
    assert calls == [("GET", "/balance", {"params": {}})]


def test_ticker_routes_correctly(monkeypatch, capsys):
    resp = httpx.Response(200, json={"dummy": True})
    client = DummyClient([resp])

    _, _, calls = run_cli(monkeypatch, capsys, client, ["ticker", "BTC/USDT"])

    method, path, payload = calls[0]
    assert (method, path) == ("GET", "/tickers/BTC/USDT")
    # no empty query params were sent
    assert payload == {"params": {}}


def test_cancel_closed_order_shows_error(monkeypatch, capsys):
    # server replies 400 with FastAPI JSON body
    error_body = {"detail": "Only *open* orders can be canceled"}
    resp = httpx.Response(400, json=error_body)
    client = DummyClient([resp])

    code, out, _ = run_cli(
        monkeypatch, capsys, client, ["cancel", "abc123"]
    )

    # our CLI exits with the human‑readable message, not with a traceback
    assert code == "HTTP 400: Only *open* orders can be canceled"
    assert out == ""  # nothing printed to stdout
