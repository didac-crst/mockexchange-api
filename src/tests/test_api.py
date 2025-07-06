import os
import time
import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Test fixture: create a TestClient with TEST_ENV=true so docs & endpoints are
# exposed without API‑Key authentication.  Each test starts with a *clean*
# engine thanks to the POST /admin/reset call in the session‑scoped fixture.
# ---------------------------------------------------------------------------

os.environ.setdefault("TEST_ENV", "true")   # disable x-api-key
url_api = os.getenv("URL_API", "http://localhost:8000/")  # default to local dev server

from mockexchange.server import app  # noqa: E402 after env var is set

_client = TestClient(app, base_url=url_api)

@pytest.fixture(scope="session")
def client() -> TestClient:  # pylint: disable=redefined-outer-name
    """FastAPI TestClient shared by all tests."""
    # ensure a pristine state for the whole suite
    _client.post("/admin/reset")
    yield _client

# ---------------------------------------------------------------------------
# 1)  Portfolio lifecycle: reset → fund → edit → verify
# ---------------------------------------------------------------------------

def test_portfolio_lifecycle(client: TestClient):
    """end‑to‑end sanity check on balance admin endpoints."""
    # 1‑a  reset portfolio
    r = client.post("/admin/reset")
    assert r.status_code == 200

    # 1‑b  portfolio & order‑book must be empty
    assert client.get("/balance").json() == {}
    assert client.get("/orders").json() == []

    # 1‑c  add funds (USDT)
    body = {"asset": "USDT", "amount": 10_000}
    r = client.post("/admin/fund", json=body)
    assert r.status_code == 200

    # 1‑d  edit balance row (change free / used)
    body = {"asset": "USDT", "free": 8_000, "used": 2_000}
    r = client.post("/admin/edit_balance", json=body)
    assert r.status_code == 200

    # 1‑e  final balance must match exactly
    expected = {"asset": "USDT", "free": 8_000, "used": 2_000, 'total': 10000.0}
    r = client.get("/balance/USDT").json()
    assert r == expected

# ---------------------------------------------------------------------------
# 2)  Market data: list tickers & fetch single ticker
# ---------------------------------------------------------------------------

def test_tickers_endpoints(client: TestClient):
    ticker_list = client.get("/tickers").json()
    assert isinstance(ticker_list, list) and ticker_list, "empty tickers list"  # non‑empty

    # pick a random symbol from the list and fetch its ticker
    symbol = ticker_list[0]
    r = client.get(f"/tickers/{symbol}")
    assert r.status_code == 200 and r.json().get("symbol") == symbol

# ---------------------------------------------------------------------------
# 3)  Market orders: buy → sell → buy → sell
# ---------------------------------------------------------------------------

def test_market_order_flow(client: TestClient):
    client.post("/admin/reset")
    client.post("/admin/fund", json={"asset": "USDT", "amount": 100_000})

    def _mk(side: str):
        body = {"symbol": "BTC/USDT", "side": side, "type": "market", "amount": 0.01}
        return client.post("/orders", json=body)

    # buy, sell, buy, sell
    for side in ("buy", "sell", "buy", "sell"):
        r = _mk(side)
        assert r.status_code == 200

    orders = client.get("/orders").json()
    assert len(orders) == 4
    assert all(o["status"] == "closed" for o in orders)

# ---------------------------------------------------------------------------
# 4)  Limit orders: place → cancel → verify reserve / release mechanics
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="requires live price tick to cross limit; enable when tick feed is attached")

def test_limit_order_lifecycle(client: TestClient):
    client.post("/admin/reset")
    client.post("/admin/fund", json={"asset": "USDT", "amount": 10_000})

    # 4‑a  place a limit‑buy far below spot so it stays *open*
    order = {"symbol": "BTC/USDT", "side": "buy", "type": "limit", "amount": 0.02, "price": 1}  # unrealistic low
    r = client.post("/orders", json=order).json()
    oid = r["id"]

    # 4‑b  cancel it → funds should be released
    client.post(f"/orders/{oid}/cancel")
    o = client.get(f"/orders/{oid}").json()
    assert o["status"] == "canceled"

    # 4‑c  account free balance back to original (allow tiny fp tolerance)
    bal = client.get("/balance/USDT").json()
    assert abs(bal["free"] - 10_000) < 1e-6 and bal["used"] == 0

    # 4‑d  spray a few limit‑buys near current price so at least one fills
    near_price = client.get("/tickers/BTC/USDT").json()["last"]
    for delta in (-10, -5, -2):
        body = {"symbol": "BTC/USDT", "side": "buy", "type": "limit", "amount": 0.001, "price": near_price + delta}
        client.post("/orders", json=body)

    # wait a bit for tick‑loop to settle (max 3× refresh interval)
    timeout = time.time() + 3 * int(os.getenv("TICK_LOOP_SEC", "10"))
    while time.time() < timeout:
        closed = [o for o in client.get("/orders").json() if o["status"] == "closed"]
        if closed:
            break
        time.sleep(1)

    assert closed, "no limit order executed within timeout"

