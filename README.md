# MockExchange API 📈  

A **zero-risk** spot-exchange emulator that speaks the exact JSON dialect your **ccxt-based** trading bots expect — yet writes every price-tick, balance and order to **Valkey (aka Redis)** instead of touching live markets.  

* **One binary, three faces**  
    * 🐍 Import as a normal Python package in your back-tests.  
    * 🐳 Run as a Docker container exposing a FastAPI server.  
    * 💻 Fire quick commands via the bundled CLI.  
* **Deterministic & Stateless** — wipe everything with one `POST /admin/reset`.  
* **Pluggable data-feed** — point the engine to any key-value store that writes `sym_<SYMBOL>` → *last price* and the tick-loop does the rest.  
* **Consistent commission model** — flat `COMMISSION` rate applied on every fill.  

---  

## Quick start (Docker)  

> **Prerequisite** : an accessible **Valkey** instance is mandatory.  
> The commands below launch one locally; set `REDIS_URL` if you already run Valkey elsewhere.  

```bash
# 1  Start Valkey (persist to ./data)
docker run -d --name valkey -p 6379:6379 \
    -v "$(pwd)"/data:/data valkey/valkey:latest

# 2  Boot the API in front of it (auth enabled)
docker run -d --name mockexchange-api --network host \
    -e API_KEY=my-secret \
    ghcr.io/your-org/mockexchange-api:latest

# 3  Open docs (only if TEST_ENV=true)
xdg-open http://localhost:8000/docs     # or "open" on macOS
```  

### Using docker-compose for Valkey Docker

```yaml
# docker-compose.yml
services:
  valkey-tradingbot-cache:
    image: valkey/valkey:latest
    container_name: valkey-cryptobot-cache
    network_mode: "host"
    restart: always
    volumes:
      - valkey_data:/data
    # Save a snapshot every 60 s if ≥ 1 write; keep an AOF as well
    command: ["valkey-server", "--save", "60", "1", "--appendonly", "yes"]

volumes:
  valkey_data:
```  

Bring Valkey up with: `docker compose up -d valkey-tradingbot-cache`  
then start **MockExchange** as shown above.  

---  

## Environment variables (complete)  

| Var                              | Default (dev)                   | Purpose / Notes                                                                                 |
| -------------------------------- | ------------------------------- | ------------------------------------------------------------------------------------------------ |
| `API_KEY`                        | `invalid-key`                  | Required header value for **every** request (`x-api-key`).                                      |
| `REDIS_URL`                      | `redis://127.0.0.1:6379/0`     | Where Valkey lives.                                                                             |
| `CASH_ASSET`                     | `USDT`                         | The “cash” currency used by the engine when computing PnL / fees.                               |
| `COMMISSION`                     | `0.00075`                      | Fee rate (0.075 %).                                                                             |
| `TEST_ENV`                       | `false`                        | `true` disables auth **and** re-enables `/docs`; tests set this to `True`.                      |
| `TICK_LOOP_SEC`                  | `10`                           | Scan interval for price-tick loop.                                                              |
| `MIN_TIME_ANSWER_ORDER_MARKET`   | `3`                            | Lower bound for the artificial latency (seconds) injected before a market-order fill.           |
| `MAX_TIME_ANSWER_ORDER_MARKET`   | `5`                            | Upper bound for that artificial latency.                                                        |
| `MIN_MARKET_ORDER_FILL_FACTOR`   | `0.95`                         | Minimum fraction of the requested amount that must be filled on a market order.                 |
| `URL_API`                        | *(unset)*                      | Base-URL used by integration tests. Example: `https://mockexchange.your-domain.com/`.           |

### `.env` template  

A ready-to-use template lives at **`.env.example`**. Copy it and tweak as needed:  

```bash
cp .env.example .env
```  

```dotenv
# .env
API_KEY="your-super-secret-key"
REDIS_URL=redis://127.0.0.1:6379/0
CASH_ASSET=USDT
COMMISSION=0.00075
TEST_ENV=True
TICK_LOOP_SEC=10
MIN_TIME_ANSWER_ORDER_MARKET=3
MAX_TIME_ANSWER_ORDER_MARKET=5
MIN_MARKET_ORDER_FILL_FACTOR=0.95
URL_API=https://mockexchange.your-domain.com/
```  

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

| Method   | Path                       | Description                                                 |
| -------- | -------------------------- | ----------------------------------------------------------- |
| **GET**  | `/tickers`                 | List all symbols currently cached.                         |
| **GET**  | `/tickers/{symbol}`        | Latest ticker for one symbol (`BTC/USDT`).                 |
| **GET**  | `/balance`                 | Full portfolio snapshot.                                   |
| **GET**  | `/balance/{asset}`         | Balance row for `BTC`, `USDT`, …                           |
| **GET**  | `/orders`                  | List orders. Filters: `status`, `symbol`, `tail`.          |
| **GET**  | `/orders/{oid}`            | Inspect single order.                                      |
| **POST** | `/orders`                  | Create *market* or *limit* order.                          |
| **POST** | `/orders/can_execute`      | Dry-run: is there enough balance to place the order?       |
| **POST** | `/orders/{oid}/cancel`     | Cancel an *open* order.                                    |
| **POST** | `/admin/edit_balance`      | Overwrite/add a balance row.                               |
| **POST** | `/admin/fund`              | Credit `free` column of an asset.                          |
| **POST** | `/admin/reset`             | Wipe **all** balances & orders (clean slate).              |

---  

## Example workflow  

```bash
# Fund the account with 100 000 USDT
auth='-H "x-api-key:my-secret"'
curl -X POST $auth -H "Content-Type: application/json" \
    -d '{"asset":"USDT","amount":100000}' \
    http://localhost:8000/admin/fund

# Get initial balance
curl $auth http://localhost:8000/balance

# Dry-run a 0.05 BTC market buy
data='{"symbol":"BTC/USDT","side":"buy","amount":0.05}'
curl -X POST $auth -H "Content-Type: application/json" \
    -d "$data" http://localhost:8000/orders/can_execute

# Execute the order for real
curl -X POST $auth -H "Content-Type: application/json" \
    -d "$data" http://localhost:8000/orders
```  

---  

## Tick-loop internals  

A background coroutine scans Valkey for keys matching `sym_*`, feeds the latest price into `ExchangeEngine.process_price_tick(symbol)` and settles any limit orders that crossed. Interval is `TICK_LOOP_SEC` seconds (default **10 s**).  

### Feeding live prices  

MockExchange is agnostic about **where** prices come from; it simply expects a hash per symbol with fields `price` and `timestamp`, e.g.  
`HSET sym_BTC/USDT price 58640.13 timestamp 1751893262`.  

The reference feeder we use in production is a 40-line script that:  

1. Pulls fresh tickers from **Binance** via **CCXT** every 10 s.  
2. Writes each one to Valkey under `sym_{ticker}`.  

Any mechanism that follows the same convention works (Kafka consumer, WebSocket stream, another exchange, etc.).  Check `examples/binance_feeder.py` for a concrete implementation.  

---  

## Installation (from source)  

1. **Install Poetry** (one-liner below, or follow the [official docs](https://python-poetry.org/docs/#installation)):  

    ```bash
    curl -sSL https://install.python-poetry.org | python3 -
    # or with pipx:
    pipx install poetry
    ```  

2. Clone & install dev-deps:  

    ```bash
    git clone https://github.com/your-org/mockexchange-api.git
    cd mockexchange-api
    poetry install --with dev      # core + tests + linters
    ```  

3. Smoke-test the CLI:  

    ```bash
    poetry run mockexchange-cli balance
    ```  

---  

## Running the test-suite 🧪  

We ship a full integration suite that spins up a **temporary Valkey** (no persistence, no AOF) and hammers the API in-process and over HTTP.  

*Run everything:*  

```bash
poetry run pytest -q
```  

*Run one high-traffic test (200 buy + 200 sell concurrent market orders):*  

```bash
poetry run pytest src/tests/test_03_market_orders_concurrent.py -vv
```  

Useful flags:  

* `-n auto` with *pytest-xdist* to parallelise the suite.  
* `--lf` to re-run only the last failures.  

> **Note :** tests assume `URL_API=http://localhost:8000` — override if you point to a remote instance.  

---  

## Repo layout (updated 2025-07)  

```text
mockexchange-api/
├── Dockerfile                   ← Uvicorn + Poetry export
├── docker-compose.yml           ← Convenience wrapper (host-network)
├── README.md                    ← You’re here
├── pyproject.toml               ← Poetry deps & build meta
├── start_mockexchange.sh        ← Quick dev helpers
├── stop_mockexchange.sh
├── logs_mockexchange.sh
├── src/
│   ├── mockexchange/            ← Core engine (stateless library)
│   │   ├── __init__.py          ← Re-exports Engine, version, …
│   │   ├── engine.py            ← Order flow & matching
│   │   ├── market.py            ← Ticker facade
│   │   ├── portfolio.py         ← Balances
│   │   ├── orderbook.py         ← Orders & fills
│   │   ├── _types.py            ← Enums & dataclasses
│   │   └── logging_config.py    ← Centralised logging setup
│   ├── mockexchange_api/        ← API layer & CLI
│   │   ├── __init__.py
│   │   ├── server.py            ← FastAPI app (`mockexchange_api.server:app`)
│   │   └── cli.py               ← Thin command-line helper
│   └── tests/                   ← Pytest suite (unit + integration)
│       ├── conftest.py
│       ├── helpers.py
│       └── test_*               ← 01-05 cover reset → cancel flow
└── LICENSE
```  

---  

## Development notes  

* Unit-tests boot a throw-away Valkey with  
  `valkey-server --save '' --appendonly no --port 0` (**random port**).  
* Market data is whatever you drop into hashes:  
  `HSET sym_BTC/USDT price 56000 timestamp $(date +%s)`.  
* Commission is read from `COMMISSION` env (default `0.00075` = 0.075 %).  
* Code style: **Black** & **Ruff** (`poetry run ruff check .`) — run `ruff format .` to auto-fix.  
* Static typing: **MyPy** (`poetry run mypy src/mockexchange`).  

---  

## Contributing  

Pull-requests, feature ideas and bug reports are welcome!  
Please run `ruff check .`, `ruff format .` and `pytest` before opening a PR.  

---  

## License 🪪  

This project is released under the MIT License — see [`LICENSE`](LICENSE) for details.  

> **Don’t risk real money.**  Spin up MockExchange, hammer it with tests, then hit the real markets only when your algos are solid.