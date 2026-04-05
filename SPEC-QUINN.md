# SPEC: ib-quinn — Options Intelligence Layer

> **Purpose:** Pre-compute and serve option contract recommendations to the algo. Subscribes to live stock data, fetches option chains from the bridge, ranks contracts by fit criteria, serves the best match instantly when the algo queries.
> This spec is the **source of truth** for Quinn. No code until spec is agreed.

---

## 1. Problem Statement

The 3-1-2 strategy needs to select the right option contract fast — within milliseconds of identifying an entry signal. Fetching an option chain from scratch takes 200-500ms, which is too slow for intraday entries. Quinn pre-computes option rankings so the algo gets an answer instantly when it needs one.

Quinn does NOT place orders. It recommends. The algo decides whether to trade.

---

## 2. Architecture

```
[IBKR TWS]
    │ option chain data (via bridge port 5556)
    ▼
[ib-quinn]
    │ subscribes to stock tick data (port 5555)
    │ fetches option chains (port 5556)
    │ evaluates all contracts against criteria
    ▼
[Option Rankings — in-memory]
    │ per symbol, per direction (bull/bear)
    ▼
[Algo Query — port 5560 REQ/REP]
    │ requests: {"symbol": "PLTR", "direction": "long"}
    ◄──────────────────────────────
    │ responds: best contract recommendation
```

---

## 3. Configuration

### 3.1 Option Selection Criteria

These are the parameters for ranking option contracts:

| Parameter | Target | Acceptable Range | Rationale |
|-----------|--------|------------------|-----------|
| Delta | 0.50 | 0.40 – 0.65 | Immediate correlation to stock move |
| DTE | 0-4 days | 0 – 4 | Maximum gamma for intraday |
| Open Interest | > 2,000 | > 2,000 | Liquid, fast fills |
| Volume | > 500 | > 500 | Active trading today |
| Bid-Ask Spread | ≤ $0.05 (< $3 stock) / ≤ $0.10 (≥ $3) | per table | Minimize spread cost |

### 3.2 Options Window

- **Look at:** Today's expiry (0 DTE) and next 3 expiries (1-4 DTE)
- **Skip:** Any expiry beyond 4 DTE
- **Right selection:** CALL for bullish signals, PUT for bearish signals

### 3.3 Ranking Algorithm

For each contract meeting the hard filters (DTE in range, OI > 2000, volume > 500, spread OK):

```
score = abs(delta - 0.50) * weight_delta
     + DTE_penalty * weight_DTE
     + spread_penalty * weight_spread

Where:
  delta_penalty = abs(delta - 0.50) / 0.50  (0 at ideal, 1 at worst)
  DTE_penalty = DTE / 4  (0 for 0 DTE, 1 for 4 DTE)
  spread_penalty = actual_spread / max_acceptable_spread
```

Lower score = better contract. Return the contract with the lowest score.

---

## 4. Data Flow

### 4.1 Startup Sequence

1. Load ticker list from `C:\hunter\algo\tickers\daily_tickers.json`
2. Subscribe to stock tick data from bridge (port 5555) — for live price context
3. Fetch option chains for all tickers from bridge (port 5556)
4. Rate limit: 1 chain request per second (IBKR limit)
5. Rank all contracts for each symbol/direction pair
6. Store top-N rankings in memory

### 4.2 Chain Refresh

- Refresh each chain every 60 seconds during market hours
- On chain refresh, re-rank all contracts
- Log any contracts that drop out of top-N

### 4.3 Real-Time Price Updates

- Quinn subscribes to 5555 (stock tick data)
- Updates the "current stock price" context for each symbol
- Used to calculate moneyness (strike vs spot) and spread cost estimates

---

## 5. Interface

### 5.1 Algo Query (REQ/REP on port 5560)

**Request:**
```json
{
  "action": "recommend",
  "symbol": "PLTR",
  "direction": "long"
}
```

**Response:**
```json
{
  "symbol": "PLTR",
  "direction": "long",
  "strike": 25.0,
  "expiry": "20260327",
  "right": "CALL",
  "delta": 0.52,
  "gamma": 0.14,
  "theta": -0.04,
  "iv": 48.2,
  "bid": 1.85,
  "ask": 1.90,
  "mid": 1.875,
  "oi": 5000,
  "volume": 3000,
  "score": 0.12,
  "stock_price": 23.47,
  "moneyness": "OTM",
  "rank": 1,
  "age_ms": 234
}
```

**Error response:**
```json
{
  "error": "no_call_contracts_found",
  "symbol": "PLTR",
  "direction": "long"
}
```

### 5.2 Internal State

```python
class OptionContract:
    symbol:      str
    strike:      float
    expiry:      str   # "YYYYMMDD"
    right:       str   # "CALL" or "PUT"
    conId:       int   # IBKR contract ID (required for order placement)
    delta:       float
    gamma:       float
    theta:       float
    iv:          float
    bid:         float
    ask:         float
    mid:         float
    oi:          int
    volume:      int
    score:       float  # lower = better
    rank:        int    # 1 = best

class Rankings:
    symbol:        str
    direction:     str   # "long" or "short"
    stock_price:   float
    timestamp:     float # Unix epoch of last update
    contracts:     list[OptionContract]  # sorted by score, best first
```

---

## 6. ZMQ Ports

| Port | Type | Direction | Purpose |
|------|------|-----------|---------|
| 5555 | SUB | Bridge → Quinn | Stock tick data |
| 5556 | REQ | Quinn → Bridge | Option chain requests |
| 5560 | REQ/REP | Algo → Quinn | Contract recommendations |
| 5571 | SUB | Bridge → Quinn | Live Greeks streaming (optional, during burst) |

---

## 7. Logging

All logs to `C:\hunter\algo\logs\quinn\quinn_YYYYMMDD_HHMMSS.log`.

**Log levels:**
- `INFO` — startup, chain fetch events, top recommendation served
- `WARNING` — chain fetch failed, no contracts found for symbol
- `ERROR` — ZMQ send failure, unhandled exceptions
- `DEBUG` — full ranking details, every tick processed

---

## 8. Error Handling

| Scenario | Behavior |
|----------|----------|
| Bridge not running | Log ERROR, retry connection every 5s |
| Chain request fails | Log WARNING, use stale data, retry next cycle |
| No contracts pass filter | Log WARNING, return error response to algo |
| Stock tick missing | Use last known price, log WARNING |
| Quinn unavailable | Algo falls back to bridge chain fetch (port 5556 directly) |

---

## 9. Configuration Arguments

| Argument | Env Var | Default | Description |
|----------|---------|---------|-------------|
| `--zmq-host` | `ZMQ_BRIDGE_HOST` | 127.0.0.1 | Bridge ZMQ address |
| `--chain-port` | `CHAIN_PORT` | 5556 | Bridge chain REP port |
| `--tick-port` | `TICK_PORT` | 5555 | Bridge tick SUB port |
| `--rep-port` | `REP_PORT` | 5560 | Quinn recommendation REP port |
| `--refresh-interval` | `CHAIN_REFRESH_SEC` | 60 | Chain refresh interval (seconds) |
| `--log-dir` | `LOG_DIR` | C:\hunter\algo\logs | Log output dir |

---

## 10. Build Stages

**Stage 1:** Connect to bridge, subscribe to 5555 ticks, log prices.
**Stage 2:** Fetch option chains from bridge (5556), parse response.
**Stage 3:** Implement ranking algorithm, store top-N in memory.
**Stage 4:** REP server on port 5560, serve recommendations.
**Stage 5:** Chain refresh loop (60s).
**Stage 6:** Full integration test with bridge and algo.

---

## 11. Out of Scope (for v1)

- Order placement (algo handles this)
- Position tracking
- Multiple symbol simultaneous queries
- Greeks computation (use IBKR values directly)
- Historical chain analysis

---

## 12. Burst Mode (Event-Driven)

> **IMPORTANT:** Quinn is event-driven, NOT continuously running. This is the core operational model.

### 12.1 Why Burst Mode?

The 60-second continuous chain refresh is wasteful compute. Option data only matters when the algo has a live setup. Quinn should only work when needed.

### 12.2 Candle Classification Terminology

- **"Candle classification 3"** — the third pattern in the 3-1-2 strategy (energy expansion)
- **"Candle classification 1"** — the consolidation bar in the 3-1-2 strategy (inside bar)
- **"Candle classification 2"** — the trigger bar that breaks candle classification 1's range

These are NOT price bars — they are candle classifications per the strat.

### 12.3 Activation Sequence

| Event | Algo Action | Quinn Action |
|-------|-------------|--------------|
| Candle classification 3 complete | Pattern detected | IDLE — doing nothing |
| Candle classification 1 forming (last 10 seconds) | Sends `{"action": "activate", "symbol": "AAPL", "direction": "call"}` | **BURST MODE** — fetches chain + ranks every 1s |
| Candle classification 2 breaks C1 high/low | Sends `{"action": "execute", "symbol": "AAPL", "direction": "call"}` | Returns best contract instantly |
| Trade complete OR C1 closes without entry | Sends `{"action": "deactivate", "symbol": "AAPL"}` | IDLE — stops refreshing |

### 12.4 Quinn States

```
IDLE ←─────────────────────→ BURST
  ↑                            ↑
  │ (activate)                │ (execute OR deactivate OR 60s timeout)
  └────────────────────────────┘
```

- **IDLE:** Quinn running but NOT fetching chains. Only maintains price context from 5555 tick stream.
- **BURST:** Quinn fetching chain + ranking every 1 second for the activated symbol.

### 12.5 New Request Formats

**Activate (algo → Quinn):**
```json
{"action": "activate", "symbol": "AAPL", "direction": "call"}
```

**Execute (algo → Quinn):**
```json
{"action": "execute", "symbol": "AAPL", "direction": "call"}
```

**Deactivate (algo → Quinn):**
```json
{"action": "deactivate", "symbol": "AAPL"}
```

**Execute Response (Quinn → algo):**
```json
{
  "status": "ok",
  "symbol": "AAPL",
  "direction": "call",
  "strike": 248,
  "expiry": "20260330",
  "right": "CALL",
  "delta": 0.52,
  "gamma": 0.04,
  "theta": -0.08,
  "iv": 0.32,
  "bid": 2.15,
  "ask": 2.20,
  "underlying_price": 248.97
}
```

### 12.6 Burst Timing

- **Activation trigger:** 10 seconds before candle classification 1 closes
- **Burst interval:** Fetch + rank every 1 second
- **Execute response:** Return cached best (max 1 second old) — <10ms latency
- **Timeout:** Exit burst mode after 60 seconds of no activation

### 12.7 Benefits

| Aspect | Continuous (old) | Burst (new) |
|--------|------------------|-------------|
| Activation | Always running | Only when algo activates |
| Chain fetch | Every 60s always | Every 1s during setup only |
| Compute | Continuous waste | Only during setup window |
| Data freshness | 60s stale | Fresh at trade entry |

---

## 13. Bridge Greek Delivery

For Quinn to rank by Greek profile, the bridge must deliver Greeks on port 5556.

### 13.1 Bridge Response Format (5556)

```json
{
  "symbol": "AAPL",
  "expirations": ["20260330", "20260403"],
  "strikes": [245, 246, 247, 248, 249, 250],
  "contracts": [
    {
      "strike": 248,
      "expiry": "20260330",
      "right": "CALL",
      "conId": 123456789,
      "delta": 0.52,
      "gamma": 0.04,
      "theta": -0.08,
      "iv": 0.32,
      "bid": 2.15,
      "ask": 2.20,
      "underlying": 248.97,
      "oi": 5000,
      "volume": 3000
    }
  ]
}
```

### 13.2 Greeks PUB — Port 5571 (optional)

Bridge streams live Greek updates via 5571 PUB. Quinn can subscribe during burst mode instead of re-fetching chains every 1s. Message format matches the contract object above.

### 13.3 Quinn's Role

- Receive contracts array with Greek values from bridge
- Hook delta/gamma/theta into the scoring algorithm
- Rank by Greek profile (prefer higher gamma, lower theta for intraday)
