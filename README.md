# MockExchange API 📈

A **zero-risk** spot-exchange emulator that speaks the exact JSON dialect your ccxt-based trading bots expect — but stores every price tick, balance and order in **Valkey (aka Redis)** instead of touching live markets.

- **One binary, three faces**
- 🐍 Import as a normal Python package in your back-tests.
- 🐳 Run as a Docker container exposing a FastAPI server.
- 💻 Fire quick commands via the bundled CLI.
- **Deterministic & Stateless** — wipe everything with one `POST /admin/reset`.
- **Pluggable data-feed** — point the engine to any key-value store that writes `sym_<SYMBOL>` → *last price* and the tick-loop does the rest.
- **Consistent commission model** — flat `COMMISSION` rate applied on every fill.

---

## Quick start (Docker)

```bash
# 1  Start Valkey (persist to ./data)
$ docker run -d --name valkey -p 6379:6379 \
    -v $(pwd)/data:/data valkey/valkey:latest

# 2  Boot the API in front of it (auth enabled)
$ docker run -d --name mockexchange-api --network host \
    -e API_KEY=my-secret \
    ghcr.io/your-org/mockexchange-api:latest

# 3  Open docs (only if TEST_ENV=true)
$ open http://localhost:8000/docs
```

---

## Environment variables

| Var             | Default                    | Purpose                                                    |
| --------------- | -------------------------- | ---------------------------------------------------------- |
| `API_KEY`       | `invalid-key`              | Required header value for **every** request (`x-api-key`). |
| `REDIS_URL`     | `redis://localhost:6379/0` | Where Valkey lives.                                        |
| `COMMISSION`    | `0.001`                    | Fee rate (0.1 %=0.001).                                    |
| `TICK_LOOP_SEC` | `10`                       | Scan interval for price-tick loop.                         |
| `TEST_ENV`      | `false`                    | `true` disables auth **and** re-enables `/docs`.           |

> **Tip :** set `TEST_ENV=true` in CI so Postman or integration tests don’t need the header.

---

## Authentication

Production containers reject any request that doesn’t include the correct key:

```http
x-api-key: my-secret
```

Set the header once at *collection* level in Postman or use `curl -H "x-api-key:$API_KEY" …`.

---

## REST Endpoints

| Method   | Path                   | Description                                          |
| -------- | ---------------------- | ---------------------------------------------------- |
| **GET**  | `/tickers`             | List all symbols currently cached.                   |
| **GET**  | `/tickers/{symbol}`    | Latest ticker for one symbol (`BTC/USDT`).           |
| **GET**  | `/balance`             | Full portfolio snapshot.                             |
| **GET**  | `/balance/{asset}`     | Balance row for `BTC`, `USDT`, …                     |
| **GET**  | `/orders`              | List orders. Filters: `status`, `symbol`, `tail`.    |
| **GET**  | `/orders/{oid}`        | Inspect single order.                                |
| **POST** | `/orders`              | Create *market* or *limit* order.                    |
| **POST** | `/orders/can_execute`  | Dry-run: is there enough balance to place the order? |
| **POST** | `/orders/{oid}/cancel` | Cancel an *open* order.                              |
| **POST** | `/admin/edit_balance`  | Overwrite/add a balance row.                         |
| **POST** | `/admin/fund`          | Credit `free` column of an asset.                    |
| **POST** | `/admin/reset`         | Wipe **all** balances & orders (clean slate).        |

---

## Example workflow

```bash
# fund the account with 100 000 USDT
auth='-H "x-api-key:my-secret"'
curl -X POST $auth -H "Content-Type: application/json" \
    -d '{"asset":"USDT","amount":100000}' \
    http://localhost:8000/admin/fund

# get initial balance
curl $auth http://localhost:8000/balance

# dry-run a 0.05 BTC market buy
data='{"symbol":"BTC/USDT","side":"buy","amount":0.05}'
curl -X POST $auth -H "Content-Type: application/json" \
    -d "$data" http://localhost:8000/orders/can_execute

# execute the order for real
curl -X POST $auth -H "Content-Type: application/json" \
    -d "$data" http://localhost:8000/orders
```

---

## Tick-loop internals

A background coroutine scans Valkey for keys matching `sym_*`, feeds the latest price into `ExchangeEngine.process_price_tick(symbol)` and settles any limit orders that crossed. Interval is `TICK_LOOP_SEC` seconds (default **10 s**).

You can swap this loop for a Redis **Pub/Sub** subscriber if you already publish live prices – simply call `process_price_tick(symbol)` from the message handler.

---

## Local development (Poetry)

```bash
$ git clone https://github.com/your-org/mockexchange-api.git
$ cd mockexchange-api
$ poetry install
$ poetry run python -m mockexchange.cli balance  # quick smoke-test
```

Run the FastAPI app directly:

```bash
$ TEST_ENV=true poetry run uvicorn mockexchange.server:app --reload
```

---

## Repository layout

```text
mockexchange-api/
├── pyproject.toml            ← Poetry deps & build
├── Dockerfile                ← API image (uvicorn + poetry export)
├── docker-compose.yml        ← host-network by default
├── README.md                 ← this file
├── src/
│   └── mockexchange/
│       ├── __init__.py       ← re-exports Engine, version, …
│       ├── engine.py         ← business logic (order flow)
│       ├── market.py         ← ticker facade
│       ├── portfolio.py      ← balances
│       ├── orderbook.py      ← orders
│       └── _types.py         ← dataclasses / enums
└── scripts/
  ├── server.py             ← FastAPI wrapper (imports Engine)
  └── cli.py                ← thin command-line helper
```

---

##  Development notes
* Unit-tests use a **temporary Valkey** started with  
`valkey-server --save '' --appendonly no` on a random port.
* Market data is whatever you drop into hashes:  
`HSET sym_BTC/USDT price 56000 timestamp $(date +%s)`.
* Commission is read from `COMMISSION` env (default 0.00075 = 0.075 %).

---

## Contributing
Contributions are welcome! If you have suggestions for improvements or find a bug, please feel free to open an issue or submit a pull request.

---

##  License
This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

> **Don’t risk real money.** Spin up MockExchange, hammer it with tests, then hit the real markets only when your algos are solid.