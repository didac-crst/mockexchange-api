# Mock Exchange API 📈

A **self-contained** spot-exchange emulator that speaks the same JSON
dialect trading-bots expect from _real_ ccxt exchanges – but keeps every­thing
in **Valkey (Redis)** instead of touching live markets.

Run it as:

* a **local Python package** (venv, Poetry)  
* a **stand-alone Docker container** exposing a FastAPI server  
* a quick **CLI** for shell-level smoke tests

---

##  Repo layout
```
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

##  Fast tour 🔥
```
# 1️⃣ run Valkey first (outside the repo – or reuse an existing one)
docker run -d --name valkey -p 6379:6379 valkey/valkey:latest

# 2️⃣ boot the API (host network – talks to host’s :6379)
docker compose up -d               # produces container “mockexchange-api”

# 3️⃣ open Swagger
open http://localhost:8000/docs    # /ticker /orders /balance …

# 4️⃣ add 100 000 USDT
curl -X POST localhost:8000/balances/USDT/fund -d '{"amount":100000}'

# 5️⃣ dry-run a market buy
curl -X POST localhost:8000/orders/can_execute \
     -H 'Content-Type: application/json' \
     -d '{"symbol":"BTC/USDT","side":"buy","amount":0.05}'

# 6️⃣ execute it for real
curl -X POST localhost:8000/orders \
     -H 'Content-Type: application/json' \
     -d '{"symbol":"BTC/USDT","side":"buy","amount":0.05}'
```

---

##  Running inside Docker only 🐳

*Compose file (`docker-compose.yml`) – **host network** so the
container can connect to a Valkey already listening on `localhost:6379`.*
```yaml
services:
  api:
    build: .
    container_name: mockexchange-api
    network_mode: host          # ← single-node dev, zero NAT foofaraw
    environment:
      REDIS_URL: redis://127.0.0.1:6379/0
    restart: unless-stopped
```

**Start**
```
docker compose up -d
```

**Tail logs**
```
docker logs -f mockexchange-api
```

---

##  Running Valkey **and** API via Compose (bridge)
```yaml
version: "3.8"
services:
  valkey:
    image: valkey/valkey:latest
    networks: [ cryptonet ]
  api:
    build: .
    environment:
      REDIS_URL: redis://valkey:6379/0
    ports: [ "8000:8000" ]
    networks: [ cryptonet ]
networks: { cryptonet: {} }
```
```
docker compose -f docker-compose.bridge.yml up -d
```

---

##  CLI usage 🖥️

**Outside** Docker (needs Poetry env):
```
poetry run python -m mockexchange.cli balance
poetry run python -m mockexchange.cli ticker BTC/USDT
poetry run python -m mockexchange.cli buy BTC/USDT 0.02
```

**Inside** the running container:
```
docker exec -it mockexchange-api \
    python -m mockexchange.cli ticker ETH/USDT
```
(The `REDIS_URL` env-var is baked into the image.)

**One-shot** container just for CLI:
```
docker run --rm --network host \
   -e REDIS_URL=redis://127.0.0.1:6379/0 \
   mockexchange-api python -m mockexchange.cli balance
```

---

##  HTTP API cheat-sheet
| Verb | Path | Body / Query | Purpose |
|------|------|--------------|---------|
| `GET` | `/ticker/{symbol}` | – | latest price |
| `POST` | `/orders` | `{symbol,side,amount,price?}` | market/limit |
| `POST` | `/orders/can_execute` | same | dry-run margin check |
| `GET` | `/orders/recent?n=N` | – | last **N** orders |
| `GET` | `/balance` | – | all assets (`free/used/total`) |
| `POST` | `/balances` | `{asset,free,used}` | set/overwrite |
| `POST` | `/balances/{asset}/fund` | `{amount}` | credit `free` |
| `POST` | `/admin/reset` | – | wipe balances + orders |

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

##  License
This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
