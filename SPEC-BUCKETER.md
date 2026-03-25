# SPEC: Tick Time Bucketer — Core Engine

> **Purpose:** Vendor-agnostic time-bucketing engine. Consumes raw ticks (with exchange timestamps), produces OHLCV candles in fixed time windows.
> This spec defines the pure bucketing logic. It has no IBKR dependency — it accepts ticks from any source.

---

## 1. Interface

```python
class TimeBucketer:
    """
    Parameters:
        window_seconds: int  — size of each time window (default: 1)
    """

    def add_tick(self, tick: RawTick) -> list[Bar]:
        """
        Add a raw tick and return any completed bars.

        Returns:
            List of Bar objects — one per finalized window.
            Empty list if tick belongs to current (unfinalized) window.
        """

    def get_current_bar(self, symbol: str) -> Bar | None:
        """Returns the in-progress bar for the current window, or None."""

    def flush_all(self) -> list[Bar]:
        """Finalize all open windows. Called on shutdown."""
```

---

## 2. Input: RawTick

```python
@dataclass
class RawTick:
    symbol:    str    # "PLTR"
    price:     float  # 23.45
    volume:    int    # trade size
    timestamp: float  # Unix epoch (exchange time, not local)
```

**Critical:** `timestamp` must be the **exchange timestamp**, not the local clock time of receipt. Using local receipt time destroys candle accuracy.

---

## 3. Output: Bar

```python
@dataclass
class Bar:
    symbol:     str
    window:     int   # Unix epoch of window start (integer seconds)
    open:       float
    high:       float
    low:        float
    close:      float
    volume:     int
    tick_count: int   # number of ticks aggregated
```

---

## 4. Windowing Logic

### 4.1 Window Identity

- Window number `W = int(tick.timestamp)` — integer seconds since epoch
- Window `W` covers `[W, W+1)` — includes ticks at exactly `W`, excludes ticks at `W+1`
- A tick at Unix timestamp `1742995212.345` belongs to window `1742995212`

### 4.2 Accumulation State (per symbol)

```python
@dataclass
class BucketState:
    window_start: int   # current window W
    open:         float
    high:         float
    low:          float
    close:        float
    volume:       int
    tick_count:   int
```

**Initialization:** When first tick for a symbol arrives, create its BucketState at that `window_start`.

### 4.3 Tick Addition Algorithm

```
function add_tick(tick):
    W = int(tick.timestamp)

    if symbol not in state:
        create new BucketState at window W
        initialize O=H=L=C=tick.price, V=tick.volume, count=1
        return []  (no completed bar yet)

    if W == state.window_start:
        # Same window — accumulate
        state.high = max(state.high, tick.price)
        state.low  = min(state.low,  tick.price)
        state.close = tick.price
        state.volume += tick.volume
        state.tick_count += 1
        return []  (still in progress)

    if W > state.window_start:
        # New window — finalize previous
        completed_bar = build Bar from state
        emit completed_bar

        # Handle gap: any missing windows between old W and new W?
        gap_seconds = W - state.window_start - 1
        for each missing second G in gap_seconds:
            zero_bar = Bar(symbol=symbol, window=state.window_start+1+G,
                           open=state.close, high=state.close,
                           low=state.close, close=state.close,
                           volume=0, tick_count=0)
            emit zero_bar

        # Start new window
        state.window_start = W
        state.open = state.high = state.low = state.close = tick.price
        state.volume = tick.volume
        state.tick_count = 1
        return [completed_bar, *zero_bars]
```

### 4.4 Gap Handling

If the gap between two ticks is 2 or more seconds, emit a **zero-filled bar** for each missing second. The zero bar's OHLC = the close of the last real bar. Volume = 0. This preserves candle continuity for the algo.

**Example:**
- Last tick of window 100: close = 23.47
- Next tick arrives at window 103
- Emit zero bars for windows 101 and 102, then start window 103

**Note:** A 1-second gap (consecutive windows with no ticks) is **not** a zero bar — the next tick simply starts the new window. Zero bars are only for gaps of 2+ seconds.

---

## 5. MTF Aggregation (5M / 15M)

The bucketer manages one `MTFBuilder` per timeframe per symbol.

### 5.1 MTFBuilder

```python
class MTFBuilder:
    """
    Parameters:
        window_seconds: int  — e.g. 300 for 5M, 900 for 15M
        window_offset:  int  — alignment anchor (0 = on the hour)
    """

    def add_bar(self, bar_1s: Bar) -> Bar | None:
        """
        Add a completed 1S bar.
        Returns completed MTF bar if window just closed, else None.
        """

    def flush(self) -> Bar | None:
        """Finalize current MTF window. Called on shutdown."""
```

### 5.2 Window Alignment

- **5M:** Anchor at 0 seconds. Windows: `[0-300)`, `[300-600)`, `[600-900)`, ...
- **15M:** Anchor at 0 seconds. Windows: `[0-900)`, `[900-1800)`, ...

A 1S bar at window `W` belongs to MTF window `M = floor((W - anchor) / window_seconds) * window_seconds + anchor`.

### 5.3 5M / 15M Bar Schema

```python
@dataclass
class Bar5M:
    window:     int     # window start as Unix epoch
    open:       float
    high:       float
    low:        float
    close:      float
    volume:     int
    bar_count:  int     # how many 1S bars were aggregated
```

Same structure for `Bar15M`.

---

## 6. Special Cases

### 6.1 Tick Arriving Late (Out of Order)

If `int(tick.timestamp) < state.window_start` — tick is for an already-closed window:
- Log WARNING with details
- Discard tick (do not accumulate, do not crash)

### 6.2 First Tick Ever

- Create BucketState at that window
- No bar emitted (nothing to complete)

### 6.3 Shutdown Flush

When `flush_all()` is called:
- Emit the current in-progress bar as completed
- For each MTFBuilder, call `flush()` and emit its final bar

---

## 7. Thread Safety

- The bucketer is used from the IBKR tick callback thread (not the main asyncio loop)
- All shared state access must be protected by `threading.Lock`
- The lock is held for the minimum time necessary — no I/O while holding the lock

---

## 8. Testing Checklist

Before integration, verify:

- [ ] Tick at `ts=100.0` creates window 100, no bar emitted
- [ ] Tick at `ts=100.5` accumulates correctly in window 100 (same window)
- [ ] Tick at `ts=101.0` finalizes window 100, returns 1 bar
- [ ] Gap of 3 seconds (window 100 → 104) emits 3 zero bars (101, 102, 103)
- [ ] Zero bar has OHLC = close of last real bar, volume = 0
- [ ] Late tick (ts < window_start) is discarded with WARNING
- [ ] Shutdown flush emits the in-progress bar
- [ ] MTF builder correctly aggregates 300 x 1S bars into one 5M bar
- [ ] MTF builder correctly handles missing 1S bars within a 5M window

---

## 9. Example Trace

```
Tick PLTR ts=100.0 price=23.40 → window 100 [O=23.40 H=23.40 L=23.40 C=23.40 V=100 ct=1]
Tick PLTR ts=100.2 price=23.45 → window 100 [O=23.40 H=23.45 L=23.40 C=23.45 V=200 ct=2]
Tick PLTR ts=100.9 price=23.47 → window 100 [O=23.40 H=23.45 L=23.40 C=23.47 V=300 ct=3]
Tick PLTR ts=101.1 price=23.50 → EMIT BAR window=100 [O=23.40 H=23.45 L=23.40 C=23.47 V=300 ct=3]
                                 → window 101 [O=23.50 H=23.50 L=23.50 C=23.50 V=50 ct=1]
Tick PLTR ts=103.8 price=23.55 → EMIT BAR window=101 [O=23.50 H=23.50 L=23.50 C=23.50 V=50 ct=1]
                                 → EMIT ZERO BAR window=102
                                 → window 103 [O=23.55 H=23.55 L=23.55 C=23.55 V=75 ct=1]
```
