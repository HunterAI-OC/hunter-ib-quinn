"""
ib-quinn.py — Options Intelligence Layer

Spec: SPEC-QUINN.md (source of truth)

Provides instant option recommendations by pre-ranking all available contracts.
Subscribes to stock ticks (5555), fetches option chains from bridge (5556),
serves recommendations on port 5560.

Usage:
    python ib-quinn.py [--zmq-host 127.0.0.1] [--chain-port 5556]
                       [--tick-port 5555] [--rep-port 5560]
                       [--refresh-interval 60] [--log-dir C:/hunter/algo/logs]
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import zmq
import zmq.asyncio


# ============================================================================
# CONFIGURATION
# ============================================================================

ZMQ_TICK_PORT           = 5555   # Bridge → Quinn: stock tick data (SUB)
ZMQ_OPTIONS_PORT        = 5556   # Quinn → Bridge: option chain requests (REQ)
ZMQ_RECOMMENDATION_PORT = 5560   # Algo → Quinn: recommendation queries (REP)

# ── Option Selection Criteria (from SPEC-QUINN §3.1) ──────────────────────

DELTA_TARGET    = 0.50
DELTA_MIN       = 0.40
DELTA_MAX       = 0.65
DTE_MAX         = 4     # 0-4 DTE
OI_MIN          = 2000
VOLUME_MIN      = 500
MAX_SPREAD_LOW  = 0.05  # ≤ $0.05 spread for stocks < $3
MAX_SPREAD_HIGH = 0.10  # ≤ $0.10 spread for stocks ≥ $3

# ── Ranking weights (SPEC-QUINN §3.3) ─────────────────────────────────────

WEIGHT_DELTA  = 0.50
WEIGHT_DTE    = 0.30
WEIGHT_SPREAD = 0.20

REFRESH_INTERVAL = 60  # seconds between chain refreshes


# ============================================================================
# DATA MODELS  (SPEC-QUINN §5.2)
# ============================================================================

@dataclass
class OptionContract:
    symbol:    str
    strike:    float
    expiry:    str        # "YYYYMMDD"
    right:     str        # "CALL" or "PUT"
    delta:     float
    gamma:     float
    theta:     float
    iv:        float
    bid:       float
    ask:       float
    mid:       float
    oi:        int
    volume:    int
    score:     float = 0.0
    rank:      int    = 0


@dataclass
class Rankings:
    symbol:      str
    direction:   str          # "long" or "short"
    stock_price: float
    timestamp:   float        # Unix epoch of last update
    contracts:   List[OptionContract]  # sorted by score, best first


# ============================================================================
# QUINN ENGINE
# ============================================================================

class QuinnEngine:
    """
    Options intelligence engine.
    Implements SPEC-QUINN §4 data flow.
    """

    def __init__(
        self,
        ctx: zmq.asyncio.Context,
        zmq_host: str,
        tick_port: int,
        chain_port: int,
        rep_port: int,
        refresh_interval: int,
        log_dir: Path,
    ):
        self.ctx             = ctx
        self.zmq_host        = zmq_host
        self.tick_port        = tick_port
        self.chain_port       = chain_port
        self.rep_port         = rep_port
        self.refresh_interval = refresh_interval
        self.log_dir          = log_dir

        self.tickers: List[str]                 = []
        self.current_prices: Dict[str, float]    = {}   # symbol → price
        self.rankings: Dict[str, Rankings]       = {}   # key: f"{symbol}|{direction}"

        self.tick_sub: Optional[zmq.Socket]      = None
        self.options_req: Optional[zmq.Socket]   = None
        self.recommendation_rep: Optional[zmq.Socket] = None

        # Lock protects the ZMQ REQ socket from corruption if CancelledError
        # fires between send_json and recv_json (lockstep REQ socket constraint)
        self._req_lock: asyncio.Lock = asyncio.Lock()

        self._running = False

    # ── Connections ────────────────────────────────────────────────────────

    async def connect_to_bridge_with_retry(
        self, max_attempts: int = 5, retry_delay: float = 5.0
    ) -> None:
        """
        Connect to bridge with retry loop.

        Attempt to connect to both ZMQ sockets (tick SUB + chain REQ).
        On failure: WARNING log, wait retry_delay seconds, try again.
        After max_attempts consecutive failures: ERROR log, exit process.
        """
        attempt = 0

        while True:
            attempt += 1

            # ── Tick subscriber ──────────────────────────────────────
            tick_sub_ok = False
            chain_req_ok = False

            try:
                self.tick_sub = self.ctx.socket(zmq.SUB)
                self.tick_sub.setsockopt(zmq.RCVTIMEO, 5000)
                self.tick_sub.connect(f"tcp://{self.zmq_host}:{self.tick_port}")
                tick_sub_ok = True
            except Exception:
                logging.warning(
                    f"[bridge] Connection attempt {attempt}/5 failed — retrying in 5s"
                )

            # ── Chain request socket ───────────────────────────────────
            if tick_sub_ok:
                try:
                    self.options_req = self.ctx.socket(zmq.REQ)
                    self.options_req.setsockopt(zmq.RCVTIMEO, 5000)
                    self.options_req.connect(
                        f"tcp://{self.zmq_host}:{self.chain_port}"
                    )
                    chain_req_ok = True
                except Exception:
                    logging.warning(
                        f"[bridge] Connection attempt {attempt}/5 failed — retrying in 5s"
                    )

            # ── Both succeeded ────────────────────────────────────────
            if tick_sub_ok and chain_req_ok:
                for sym in self.tickers:
                    self.tick_sub.setsockopt(zmq.SUBSCRIBE, sym.encode())
                logging.info(
                    f"[bridge] Connected -- tick SUB on {self.tick_port}, "
                    f"chain REQ on {self.chain_port}"
                )
                return

            # ── Cleanup failed sockets before retry ───────────────────
            if self.tick_sub:
                try:
                    self.tick_sub.close()
                except Exception:
                    pass
                self.tick_sub = None
            self.options_req = None

            # ── Retry or give up ──────────────────────────────────────
            if attempt >= max_attempts:
                logging.error(
                    "[bridge] Could not connect to bridge after 5 attempts — exiting"
                )
                sys.exit(1)
            await asyncio.sleep(retry_delay)

    async def start_server(self) -> None:
        """Bind REP server on port 5560 for algo queries. RCVTIMEO=5s prevents
        a hung client from blocking the socket forever."""
        self.recommendation_rep = self.ctx.socket(zmq.REP)
        self.recommendation_rep.setsockopt(zmq.RCVTIMEO, 5000)  # 5s timeout
        self.recommendation_rep.bind(f"tcp://{self.zmq_host}:{self.rep_port}")
        logging.info(f"Quinn recommendation server bound on port {self.rep_port}")

    def _close_options_req(self) -> None:
        """Safely close the options REQ socket."""
        if self.options_req is not None:
            try:
                self.options_req.close()
            except Exception:
                pass
            self.options_req = None

    def _create_options_req(self) -> None:
        """Create a fresh options REQ socket."""
        self._close_options_req()
        self.options_req = self.ctx.socket(zmq.REQ)
        self.options_req.setsockopt(zmq.RCVTIMEO, 5000)
        self.options_req.connect(f"tcp://{self.zmq_host}:{self.chain_port}")

    # ── Ticker loading ─────────────────────────────────────────────────────

    def load_tickers(self) -> None:
        """Load ticker list from daily_tickers.json (SPEC-QUINN §4.1)."""
        paths = [
            Path("C:/hunter/algo/tickers/daily_tickers.json"),
            Path("C:/hunter/algo/quinn/tickers/daily_tickers.json"),
            Path(__file__).parent / "tickers" / "daily_tickers.json",
            Path(__file__).parent.parent / "tickers" / "daily_tickers.json",
            Path("tickers/daily_tickers.json"),
            Path("/tmp/hunter-algo/tickers/daily_tickers.json"),
        ]

        for p in paths:
            if p.exists():
                try:
                    with open(p) as f:
                        data = json.load(f)
                    if isinstance(data, list):
                        self.tickers = data
                    elif isinstance(data, dict):
                        self.tickers = data.get("tickers", data.get("symbols", []))
                    if self.tickers:
                        logging.info(f"Loaded {len(self.tickers)} tickers from {p}")
                        return
                except Exception as e:
                    logging.warning(f"Failed to read {p}: {e}")

        # Fallback
        self.tickers = ["PLTR", "NVDA", "TSLA", "SMCI", "QQQ"]
        logging.warning(f"No ticker file found — using fallback: {self.tickers}")

    # ── Option chain fetching ───────────────────────────────────────────────

    def _calc_dte(self, expiry_str: str) -> Optional[int]:
        """
        Calculate DTE from expiry string 'YYYYMMDD'.
        Returns None if expiry is invalid or in the past.
        """
        try:
            expiry_date = datetime.strptime(expiry_str, "%Y%m%d").date()
            today = date.today()
            dte = (expiry_date - today).days
            return dte if dte >= 0 else None
        except ValueError:
            return None

    async def fetch_option_chain(self, symbol: str) -> List[OptionContract]:
        """
        Fetch option chain for symbol from bridge via REQ on port 5556.
        Returns list of OptionContract objects.

        The _req_lock ensures only one REQ operation runs at a time (ZMQ REQ is
        lockstep — a second send before the prior recv reply would block forever).
        If CancelledError fires during the operation, the socket is closed and
        recreated so no corrupted state is left for the next request.
        """
        async with self._req_lock:
            try:
                await self.options_req.send_json(
                    {"action": "chains", "symbol": symbol}
                )
                response = await self.options_req.recv_json()
            except asyncio.CancelledError:
                # Socket left in unfulfilled-request state — must close and
                # recreate or every subsequent REQ call will hang forever.
                logging.warning(
                    f"[bridge] REQ socket cancelled for {symbol}, recreating socket"
                )
                self._close_options_req()
                self._create_options_req()
                return []
            except Exception as e:
                logging.error(f"Failed to fetch chain for {symbol}: {e}")
                return []

        if response.get("status") != "ok":
            logging.warning(f"Chain request failed for {symbol}: {response}")
            return []

        contracts = []
        for opt_data in response.get("chain", []):
            try:
                contract = OptionContract(
                    symbol=symbol,
                    strike=float(opt_data.get("strike", 0)),
                    expiry=str(opt_data.get("expiry", "")),
                    right=opt_data.get("right", "CALL"),
                    delta=float(opt_data.get("delta", 0.5)),
                    gamma=float(opt_data.get("gamma", 0)),
                    theta=float(opt_data.get("theta", 0)),
                    iv=float(opt_data.get("iv", 30)),
                    bid=float(opt_data.get("bid", 0)),
                    ask=float(opt_data.get("ask", 0)),
                    mid=float(opt_data.get("mid", 0)),
                    oi=int(opt_data.get("oi", 0)),
                    volume=int(opt_data.get("volume", 0)),
                )
                contracts.append(contract)
            except Exception as e:
                logging.warning(f"Failed to parse option for {symbol}: {e}")

        return contracts

    # ── Ranking algorithm (SPEC-QUINN §3.3) ────────────────────────────────

    def _spread_cost_acceptable(self, contract: OptionContract, stock_price: float) -> bool:
        """
        Spread rule from SPEC-QUINN §3.1 table:
          - stock < $3: bid-ask spread ≤ $0.05
          - stock ≥ $3: bid-ask spread ≤ $0.10
        Uses isclose() to handle floating-point precision (e.g. 1.85-1.80 ≠ exactly 0.05).
        """
        spread = contract.ask - contract.bid
        max_spread = MAX_SPREAD_LOW if stock_price < 3.0 else MAX_SPREAD_HIGH
        return spread <= max_spread + 1e-9

    def rank_contracts(
        self,
        contracts: List[OptionContract],
        direction: str,
        stock_price: float,
    ) -> Rankings:
        """
        Rank contracts per SPEC-QUINN §3.3.

        Hard filters:
          - right matches direction (CALL for long, PUT for short)
          - DTE in [0, DTE_MAX]
          - OI >= OI_MIN
          - volume >= VOLUME_MIN
          - spread within acceptable range

        Scoring (lower = better):
          score = delta_penalty * W_DELTA
                + DTE_penalty   * W_DTE
                + spread_penalty * W_SPREAD
        """
        filtered = []

        for c in contracts:
            # Direction filter
            if direction == "long" and c.right != "CALL":
                continue
            if direction == "short" and c.right != "PUT":
                continue

            # DTE filter
            dte = self._calc_dte(c.expiry)
            if dte is None or dte > DTE_MAX:
                continue

            # OI filter
            if c.oi < OI_MIN:
                continue

            # Volume filter
            if c.volume < VOLUME_MIN:
                continue

            # Spread filter
            if not self._spread_cost_acceptable(c, stock_price):
                continue

            filtered.append(c)

        # Score each contract
        for c in filtered:
            # delta_penalty: 0 at ideal (0.50), 1 at worst (0 or 1)
            delta_penalty = abs(c.delta - DELTA_TARGET) / 0.50

            # DTE_penalty: 0 for 0 DTE, 1 for DTE_MAX
            dte = self._calc_dte(c.expiry) or 0
            dte_penalty = dte / DTE_MAX

            # spread_penalty: actual_spread / max_acceptable_spread
            spread = c.ask - c.bid
            max_spread = MAX_SPREAD_LOW if stock_price < 3.0 else MAX_SPREAD_HIGH
            spread_penalty = min(spread / max_spread, 1.0) if max_spread > 0 else 0

            c.score = (
                delta_penalty  * WEIGHT_DELTA
                + dte_penalty  * WEIGHT_DTE
                + spread_penalty * WEIGHT_SPREAD
            )

        # Sort by score ascending (lower = better)
        filtered.sort(key=lambda x: x.score)

        # Assign ranks
        for i, c in enumerate(filtered):
            c.rank = i + 1

        rankings_symbol = filtered[0].symbol if filtered else ""
        return Rankings(
            symbol=rankings_symbol,
            direction=direction,
            stock_price=stock_price,
            timestamp=time.time(),
            contracts=filtered,
        )

    # ── Background tasks ───────────────────────────────────────────────────

    async def refresh_chains(self) -> None:
        """
        Periodically refresh all option chains and re-rank.
        SPEC-QUINN §4.2: Refresh every 60 seconds during market hours.
        Fetches all chains in parallel via asyncio.gather for performance.
        """
        while self._running:
            # Evict stale tickers — only keep tickers we still track
            stale = set(self.current_prices.keys()) - set(self.tickers)
            for s in stale:
                del self.current_prices[s]

            # Fetch all chains concurrently
            chains = await asyncio.gather(
                *[self.fetch_option_chain(sym) for sym in self.tickers],
                return_exceptions=True,
            )

            for symbol, chain in zip(self.tickers, chains):
                if isinstance(chain, Exception):
                    logging.warning(f"Chain fetch failed for {symbol}: {chain}")
                    continue

                stock_price = self.current_prices.get(symbol, 0.0)

                rankings_long  = self.rank_contracts(chain, "long",  stock_price)
                rankings_short = self.rank_contracts(chain, "short", stock_price)

                self.rankings[f"{symbol}|long"]  = rankings_long
                self.rankings[f"{symbol}|short"] = rankings_short

                top = rankings_long.contracts[0] if rankings_long.contracts else None
                if top:
                    logging.info(
                        f"Top call for {symbol} @ ${stock_price:.2f}: "
                        f"${top.strike:.2f} × {top.expiry} "
                        f"δ={top.delta:.2f} DTE={self._calc_dte(top.expiry)} "
                        f"S={top.score:.3f} rank={top.rank}"
                    )
                else:
                    logging.warning(f"No qualifying {symbol} calls found")

            logging.info(f"Chain refresh complete for {len(self.tickers)} tickers")
            await asyncio.sleep(self.refresh_interval)

    async def process_ticks(self) -> None:
        """
        Process incoming stock ticks from port 5555.
        Updates current_prices for moneyness calculation.
        SPEC-QUINN §4.3.
        """
        while self._running:
            try:
                msg = await self.tick_sub.recv_string()
            except asyncio.CancelledError:
                # Task is shutting down — exit cleanly. The gather in run() will
                # cancel sibling tasks; this is the intended shutdown path.
                break
            except Exception as e:
                logging.warning(f"recv_string error: {e}")
                continue

            # Message format: "SYMBOL {json}"
            parts = msg.split(" ", 1)
            if len(parts) != 2:
                continue
            sym = parts[0]

            if sym == "HEARTBEAT":
                continue

            try:
                tick = json.loads(parts[1])
            except json.JSONDecodeError:
                continue

            price = tick.get("last") or tick.get("bid") or tick.get("ask")
            if price:
                self.current_prices[sym] = price

    async def handle_requests(self) -> None:
        """
        Handle algo recommendation queries on port 5560 (REP).
        Request:  {"action": "recommend", "symbol": "PLTR", "direction": "long"}
        Response: SPEC-QUINN §5.1 full contract record.
        """
        while self._running:
            try:
                msg = await self.recommendation_rep.recv_json()
            except asyncio.CancelledError:
                # Task is shutting down — exit cleanly. The gather in run() will
                # cancel sibling tasks; this is the intended shutdown path.
                break
            except Exception as e:
                logging.error(f"recv_json error: {e}")
                continue

            t_request = time.time()
            action    = msg.get("action", "")
            symbol    = msg.get("symbol", "")
            direction = msg.get("direction", "long")

            if action != "recommend":
                await self._send_error(
                    f"unknown_action: '{action}'", symbol, direction, t_request
                )
                continue

            key = f"{symbol}|{direction}"
            rankings = self.rankings.get(key)

            if not rankings or not rankings.contracts:
                await self._send_error(
                    f"no_{'call' if direction == 'long' else 'put'}_contracts_found",
                    symbol, direction, t_request,
                )
                continue

            # Best contract
            best = rankings.contracts[0]
            stock_price = rankings.stock_price or self.current_prices.get(symbol, 0.0)

            # Moneyness
            if best.right == "CALL":
                moneyness = (
                    "ITM" if stock_price > best.strike
                    else "OTM" if stock_price < best.strike
                    else "ATM"
                )
            else:
                moneyness = (
                    "ITM" if stock_price < best.strike
                    else "OTM" if stock_price > best.strike
                    else "ATM"
                )

            age_ms = int((time.time() - rankings.timestamp) * 1000)

            response = {
                "symbol":       symbol,
                "direction":    direction,
                "strike":       best.strike,
                "expiry":       best.expiry,
                "right":        best.right,
                "delta":        best.delta,
                "gamma":        best.gamma,
                "theta":        best.theta,
                "iv":           best.iv,
                "bid":          best.bid,
                "ask":          best.ask,
                "mid":          best.mid,
                "oi":           best.oi,
                "volume":       best.volume,
                "score":        round(best.score, 4),
                "stock_price":  round(stock_price, 4),
                "moneyness":    moneyness,
                "rank":         best.rank,
                "age_ms":       age_ms,
            }

            logging.info(
                f"Served recommendation: {symbol} {direction} → "
                f"${best.strike:.2f} {best.expiry} rank={best.rank} age={age_ms}ms"
            )

            try:
                await self.recommendation_rep.send_json(response)
            except Exception as e:
                logging.error(f"Failed to send recommendation response: {e}")

    async def _send_error(
        self, error: str, symbol: str, direction: str, t_request: float
    ) -> None:
        """Send error response and log warning."""
        logging.warning(f"Recommendation error for {symbol} {direction}: {error}")
        try:
            await self.recommendation_rep.send_json({
                "error":      error,
                "symbol":     symbol,
                "direction":  direction,
            })
        except Exception as e:
            logging.error(f"Failed to send error response: {e}")

    # ── Run loop ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start all tasks. Blocks until shutdown."""
        self._running = True

        self.load_tickers()
        await self.connect_to_bridge_with_retry()
        await self.start_server()

        await asyncio.gather(
            self.refresh_chains(),
            self.process_ticks(),
            self.handle_requests(),
            return_exceptions=True,  # one task crashing shouldn't cancel the others
        )


# ============================================================================
# LOGGING  (SPEC-QUINN §7)
# ============================================================================

def setup_logging(log_dir: Path, process_name: str = "quinn") -> None:
    """
    Create per-process rotating log file.
    File: logs/{process}/quinn_YYYYMMDD_HHMMSS.log
    Every line: UTC ISO 8601 timestamp + level + context.
    """
    log_dir = Path(log_dir) / process_name
    log_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"{process_name}_{timestamp}.log"

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)

    fmt = "%(asctime)sZ %(levelname)s %(message)s"
    formatter = logging.Formatter(fmt)
    file_handler.setFormatter(formatter)

    # Capture everything — handlers control what gets printed
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)

    # Console (optional in dev)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    root.addHandler(console)

    logging.info(f"Logging to {log_path}")


# ============================================================================
# MAIN
# ============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ib-quinn — Options Intelligence Layer")
    parser.add_argument("--zmq-host",           default="127.0.0.1")
    parser.add_argument("--tick-port",          type=int, default=5555)
    parser.add_argument("--chain-port",         type=int, default=5556)
    parser.add_argument("--rep-port",           type=int, default=5560)
    parser.add_argument("--refresh-interval",   type=int, default=60,
                        dest="refresh_interval")
    parser.add_argument("--log-dir",            default="C:/hunter/algo/logs",
                        dest="log_dir")
    return parser.parse_args()


async def main_async(args: argparse.Namespace) -> None:
    setup_logging(Path(args.log_dir))

    ctx = zmq.asyncio.Context()
    engine = QuinnEngine(
        ctx=ctx,
        zmq_host=args.zmq_host,
        tick_port=args.tick_port,
        chain_port=args.chain_port,
        rep_port=args.rep_port,
        refresh_interval=args.refresh_interval,
        log_dir=Path(args.log_dir),
    )

    loop = asyncio.get_running_loop()

    def shutdown(sig):
        logging.info(f"Signal {sig} received — initiating shutdown")
        engine._running = False
        loop.call_soon_threadsafe(loop.stop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig: shutdown(s))
        except NotImplementedError:
            # Windows
            signal.signal(sig, lambda s, _: shutdown(s))

    # Suppress CancelledError floods on shutdown
    def suppress_cancelled(loop_, context):
        exc = context.get("exception")
        if isinstance(exc, asyncio.CancelledError):
            logging.debug("Suppressed CancelledError from event loop")
            return
        loop_.default_exception_handler(context)

    loop.set_exception_handler(suppress_cancelled)

    try:
        await engine.run()
    except asyncio.CancelledError:
        pass
    finally:
        ctx.term()
        logging.info("Quinn stopped.")


def main() -> None:
    args = parse_args()
    logging.info("ib-quinn starting — Options Intelligence Layer")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
