# SPEC: IBKR Time Bucketer — Bridge

> **Purpose:** Connect to IBKR, collect raw tick-by-tick data, bucket into time windows, publish completed candles via ZMQ.
> This spec is the **source of truth** for the bridge build. No code until spec is agreed.

---

## 1. Problem Statement

The 3-1-2 strategy requires accurate, real-time OHLCV candles built from exchange timestamps — not arrival timestamps. IBKR's API delivers individual ticks, not pre-built candles. The bridge's job is to:

1. Subscribe to tick-by-tick data from IBKR
2. Bucket ticks into time windows (1 second)
3. Build OHLCV candles from those buckets
4. Publish completed candles to ZMQ for consumption by the algo

---

## 2. Architecture

```
IBKR (TWS/API)
    │ tick-by-tick (reqTickByTickData or reqMktData)
    ▼
[Raw Tick Ingestion]
    │  captures: symbol, price, volume, exchange_timestamp
    ▼
[Time Bucketer]
    │  groups ticks by exchange timestamp into 1-second windows
    │  builds OHLCV for each completed window
    ▼
[Bar Publisher]
    │  publishes completed 1S bars via ZMQ PUB
    ▼
[Algo / Quinn]
    │  subscribes to bars, applies strategy logic
```

---

## 3. Tick Ingestion

### 3.1 IBKR Connection

- **Host:** `127.0.0.1`
- **Port:** `7497` (paper) / `7496` (live) — configurable
- **Client ID:** integer, configurable — must not conflict with other sessions
- **Connection:** Use `ib_async` in a background thread (pattern: `threading.Thread(target=ib.run)`)
- **Reconnection:** On disconnect, attempt reconnect with exponential backoff (max 3 attempts)

### 3.2 Tickers

- Loaded from `C:\hunter\algo\tickers\daily_tickers.json`
- Format: `["PLTR", "NVDA", "TSLA", ...]`
- Hardcoded fallback if file not found: `["AAPL", "MSFT", "SPY"]`

### 3.3 Tick Types

Use **tick-by-tick** data via `reqTickByTickData`:
- `tickType="Trade"` — trade ticks only
- Capture: `price`, `volume`, `time` (exchange timestamp as Unix epoch seconds)
- Ignore: bid/ask ticks for candle building (use for spread analysis only)

Fallback: `reqMktData` if tick-by-tick fails.

### 3.4 Raw Tick Schema

Every raw tick is captured as:

```python
@dataclass
class RawTick:
    symbol:      str      # "PLTR"
    price:       float    # 23.45
    volume:      int      # 100
    timestamp:   float    # Unix epoch (exchange time), e.g. 1742995212.0
    exchange_ts: int      # exchange timestamp as integer seconds: int(timestamp)
```

---

## 4. Time Bucketer

### 4.1 Window Definition

- **Window size:** 1 second
- **Window boundary:** Unix epoch integer seconds — window N = `[N, N+1)`
- **Critical:** Use `exchange_ts` (from IBKR tick), NOT arrival time (local clock)

### 4.2 Bucket Accumulation

State per symbol:

```python
class TickBucket:
    symbol:        str
    window_start:  int      # integer seconds — start of current window
    open:          float
    high:          float
    low:           float
    close:         float
    volume:        int
    tick_count:    int
```

**Accumulation rules:**
- First tick in a new window: `open = high = low = close = tick.price`, `volume = tick.volume`
- Subsequent ticks in same window: update `high = max(high, price)`, `low = min(low, price)`, `close = tick.price`, `volume += tick.volume`

### 4.3 Window Finalization

When a tick arrives with `exchange_ts > current_window_start`:

1. **Finalize previous window** — emit the completed OHLCV candle
2. **Start new window** — reset OHLCV to new tick's price
3. **Handle gaps** — if `exchange_ts - window_start > 1`, the gap seconds had no trades — emit zero-filled bars for each missing second

**Zero-filled bar:** `open = high = low = close = last_close`, `volume = 0`

### 4.4 Completed Bar Schema

```python
@dataclass
class Bar:
    symbol:      str
    window:      int      # integer seconds — window start
    open:        float
    high:        float
    low:         float
    close:       float
    volume:      int
    tick_count:  int     # number of ticks in this window
```

---

## 5. Candle Timeframes

The bridge produces:

| Timeframe | Source | How Built |
|-----------|--------|-----------|
| 1 second | `TickBucket` | Directly from 1S window finalization |
| 5 minute | `MTFBuilder` | Aggregates 1S bars |
| 15 minute | `MTFBuilder` | Aggregates 1S bars |

### 5.1 MTF Builders (5M / 15M)

- Maintain a deque of completed 1S bars
- On each new 1S bar, check if current 5M or 15M window is complete
- **Window alignment:** 5M windows start at `HH:00, HH:05, HH:10 ...`; 15M windows start at `HH:00, HH:15, HH:30, HH:45`
- When window closes, emit the aggregated bar and remove 1S bars from deque
- If window was skipped (no 1S bars), emit zero-filled aggregated bar

---

## 6. ZMQ Pub/Sub

All publishers use ZMQ `PUB` sockets. Subscribers use `SUB` with appropriate filters.

### 6.1 Ports

| Port | Type | Data |
|------|------|------|
| 5555 | PUB | Raw 1S bar — `"{symbol} {bar.to_json()}"` |
| 5556 | REP | Option chain requests (from Quinn) |
| 5557 | REQ/REP | Metrics/state queries |
| 5558 | REQ/REP | Order submissions |
| 5559 | PUB | Position updates |
| 5561 | PUB | Completed 5M bars |
| 5562 | PUB | Completed 15M bars |
| 5563 | PUB | 1S bars (same as 5555 — duplicate for convenience) |
| 5564 | REP | Price query: `{"symbol": "PLTR"}` → `{"price": 23.45}` |

### 6.2 Bar Message Format (PUB)

```
PLTR {"symbol":"PLTR","window":1742995212,"open":23.45,"high":23.48,"low":23.44,"close":23.47,"volume":1200,"tick_count":5}
```

One JSON message per bar, newline-separated over TCP.

### 6.3 Option Chain Request (REP port 5556)

**Request:** `{"action": "chains", "symbol": "PLTR"}`
**Response:** `{"symbol": "PLTR", "expirations": ["20260403", "20260410", ...], "strikes": [...]}`

(Chain data sourced from IBKR `reqSecDefOptParams` — rate-limited to 1 req/sec)

---

## 7. Logging

All logs written to `C:\hunter\algo\logs\bridge_YYYYMMDD_HHMMSS.log`.

**Log levels:**
- `INFO` — connection events, subscription events, bar publications
- `WARNING` — recoverable issues (rate limit hit, retry)
- `ERROR` — connection failure, unhandled exceptions
- `DEBUG` — tick receipt, bucket state changes

**Every log line must include:** timestamp (UTC ISO 8601), level, and context.

---

## 8. Data Persistence

- **Parquet:** Completed 1S bars written to `C:\hunter\algo\data\parquet\{symbol}_1s_{date}.parquet`
- **Write strategy:** Batch writes — accumulate up to 1000 rows or 60 seconds, whichever comes first
- **Format:** PyArrow, zstd compression
- **Date handling:** One file per symbol per trading day

---

## 9. Error Handling

| Scenario | Behavior |
|----------|----------|
| IBKR disconnection | Log ERROR, attempt reconnect 3x with backoff, then exit |
| Rate limit (market data) | Log WARNING, pause subscription 1 second, retry |
| Zero ticks in window | Emit zero-filled bar, do not skip |
| PyArrow write failure | Log ERROR, skip write, do not crash |
| ZMQ send failure | Log ERROR, skip send, do not crash |

---

## 10. Configuration

All configuration via command-line arguments or environment variables:

| Argument | Env Var | Default | Description |
|----------|---------|---------|-------------|
| `--port` | `IBKR_PORT` | 7497 | TWS port |
| `--client-id` | `IBKR_CLIENT_ID` | 1 | IBKR client ID |
| `--tickers-file` | `TICKERS_FILE` | tickers/daily_tickers.json | Ticker list |
| `--data-dir` | `DATA_DIR` | C:\hunter\algo\data | Parquet output dir |
| `--log-dir` | `LOG_DIR` | C:\hunter\algo\logs | Log output dir |

---

## 11. Build Stages

Build in this order. Each stage is a working, testable milestone:

**Stage 1:** Connect to IBKR, log all raw ticks to file. Verify tick timestamps are exchange timestamps.
**Stage 2:** Time bucketer — group ticks into 1S windows, emit completed bars. Verify OHLCV correctness.
**Stage 3:** ZMQ PUB — publish 1S bars on port 5555. Verify with a simple subscriber script.
**Stage 4:** MTF builders — 5M and 15M aggregation. Verify window alignment.
**Stage 5:** Parquet persistence. Verify files are readable.
**Stage 6:** REP ports — price query (5564) and option chains (5556).
**Stage 7:** Full integration test with Quinn and the algo.

---

## 12. Out of Scope (for v1)

- Order placement (handled by separate process)
- Position tracking
- Multiple account support
- Crypto data
- Non-US markets
