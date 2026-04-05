"""
ib-quinn.py — Options Intelligence Layer

Spec: SPEC-QUINN.md (source of truth)

Provides instant option recommendations by pre-ranking all available contracts.
Subscribes to stock ticks (5555), fetches option chains from bridge (5556),
serves recommendations on port 5560.

Burst Mode (SPEC-QUINN §12):
  IDLE:   Quinn running, no chain fetching, only price context from 5555.
  BURST:  Quinn fetches and ranks every 1s for activated symbol.

Usage:
    python ib-quinn.py [--zmq-host 127.0.0.1] [--chain-port 5556]
                       [--tick-port 5555] [--rep-port 5560]
                       [--log-dir C:/hunter/algo/logs]
"""

import argparse
import asyncio
import json
import logging
import signal
import sys
import time

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
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

# ── Option Selection Criteria (SPEC-QUINN §3.1) ──────────────────────────────

DELTA_TARGET_CALL    = 0.60   # ideal call delta (center of 0.55–0.65 range)
DELTA_TARGET_PUT     = 0.40   # ideal put delta (center of 0.35–0.45 range)
DELTA_BAND           = 0.05   # ±0.05 from target
DTE_MAX              = 4      # 0–4 DTE
OI_MIN               = 2000
VOLUME_MIN           = 500
MAX_SPREAD_LOW       = 0.05   # ≤ $0.05 spread for stocks < $3
MAX_SPREAD_HIGH      = 0.10   # ≤ $0.10 spread for stocks ≥ $3

# ── Ranking weights (SPEC-QUINN §3.3) ─────────────────────────────────────

WEIGHT_DELTA   = 0.50
WEIGHT_DTE     = 0.30
WEIGHT_SPREAD  = 0.20

# ── Burst mode (SPEC-QUINN §12) ─────────────────────────────────────────────

BURST_INTERVAL_SEC  = 1      # fetch + rank every 1 second
BURST_TIMEOUT_SEC   = 60     # auto-exit BURST if no execute within 60s


# ============================================================================
# DATA MODELS  (SPEC-QUINN §5.2)
# ============================================================================

class QuinnState(Enum):
    IDLE  = "idle"
    BURST = "burst"


@dataclass
class OptionContract:
    symbol:    str
    strike:    float
    expiry:    str        # "YYYYMMDD"
    right:     str        # "CALL" or "PUT"
    conId:     str        # IBKR contract ID (from bridge chain response)
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

    Implements SPEC-QUINN §4 data flow + burst mode (SPEC-QUINN §12).
    """

    def __init__(
        self,
        ctx: zmq.asyncio.Context,
        zmq_host: str,
        tick_port: int,
        chain_port: int,
        rep_port: int,
        log_dir: Path,
        rep_host: str = "127.0.0.1",
    ):
        self.ctx        = ctx
        self.zmq_host   = zmq_host    # bridge connection host
        self.tick_port  = tick_port
        self.chain_port = chain_port
        self.rep_port   = rep_port
        self.rep_host   = rep_host    # local bind for algo connection
        self.log_dir    = log_dir

        self.tickers: List[str]              = []
        self.current_prices: Dict[str, float] = {}   # symbol → price

        # Rankings cache: key = f"{symbol}|{direction}"
        self.rankings: Dict[str, Rankings] = {}

        # ── ZMQ sockets ──────────────────────────────────────────────────
        self.tick_sub: Optional[zmq.Socket]          = None
        self.options_req: Optional[zmq.Socket]      = None
        self.recommendation_rep: Optional[zmq.Socket] = None

        # ── Burst mode state (SPEC-QUINN §12) ────────────────────────────
        self._state: QuinnState          = QuinnState.IDLE
        self._burst_symbol: str          = ""
        self._burst_direction: str       = ""   # "long" or "short"
        self._cached_best: Optional[OptionContract] = None
        self._burst_task: Optional[asyncio.Task]   = None
        self._burst_timeout_task: Optional[asyncio.Task] = None

        # Serialise REQ operations on port 5556 (ZMQ REQ is lockstep)
        self._req_lock = asyncio.Lock()

        self._running = False

    # ── Connections ───────────────────────────────────────────────────────

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
            tick_sub_ok = False
            chain_req_ok = False

            try:
                self.tick_sub = self.ctx.socket(zmq.SUB)
                self.tick_sub.setsockopt(zmq.RCVTIMEO, 5000)
                self.tick_sub.connect(
                    f"tcp://{self.zmq_host}:{self.tick_port}"
                )
                tick_sub_ok = True
            except Exception:
                logging.warning(
                    f"[bridge] Connection attempt {attempt}/{max_attempts} "
                    f"failed for tick SUB — retrying in {retry_delay}s"
                )

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
                        f"[bridge] Connection attempt {attempt}/{max_attempts} "
                        f"failed for chain REQ — retrying in {retry_delay}s"
                    )

            if tick_sub_ok and chain_req_ok:
                for sym in self.tickers:
                    self.tick_sub.setsockopt(zmq.SUBSCRIBE, sym.encode())
                logging.info(
                    f"[bridge] Connected — tick SUB on {self.tick_port}, "
                    f"chain REQ on {self.chain_port}"
                )
                return

            # Cleanup failed sockets
            if self.tick_sub:
                try:
                    self.tick_sub.close()
                except Exception:
                    pass
                self.tick_sub = None
            self.options_req = None

            if attempt >= max_attempts:
                logging.error(
                    f"[bridge] Could not connect after {max_attempts} attempts — exiting"
                )
                sys.exit(1)
            await asyncio.sleep(retry_delay)

    async def start_server(self) -> None:
        """Bind REP server on port 5560 for algo queries."""
        self.recommendation_rep = self.ctx.socket(zmq.REP)
        self.recommendation_rep.bind(f"tcp://{self.rep_host}:{self.rep_port}")
        logging.info(
            f"Quinn recommendation server bound on port {self.rep_port}"
        )

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
                        self.tickers = data.get(
                            "tickers", data.get("symbols", [])
                        )
                    if self.tickers:
                        logging.info(
                            f"Loaded {len(self.tickers)} tickers from {p}"
                        )
                        return
                except Exception as e:
                    logging.warning(f"Failed to read {p}: {e}")

        self.tickers = ["PLTR", "NVDA", "TSLA", "SMCI", "QQQ"]
        logging.warning(
            f"No ticker file found — using fallback: {self.tickers}"
        )

    # ── Option chain fetching ───────────────────────────────────────────────

    def _normalize_right(self, right: str) -> str:
        """
        Normalize bridge 'right' value to internal 'CALL'/'PUT' format.
        Handles: 'C', 'P', 'CALL', 'PUT' (case-insensitive).
        """
        r = right.upper()
        if r in ("C", "CALL"):
            return "CALL"
        if r in ("P", "PUT"):
            return "PUT"
        return right  # fallback

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

    async def fetch_option_chain(
        self, symbol: str, greeks: bool = True
    ) -> List[OptionContract]:
        """
        Fetch option chain for symbol from bridge via REQ on port 5556.

        Bridge 5556 enhanced response (SPEC-BRIDGE.md):
            Request:  {"action": "chains", "symbol": "AAPL", "greeks": true}
            Response: {
                "symbol": "AAPL",
                "expirations": [...],
                "strikes": [...],
                "contracts": [
                    {"strike": 248, "expiry": "20260330", "right": "C",
                     "delta": 0.52, "gamma": 0.04, "theta": -0.08,
                     "iv": 0.32, "bid": 2.15, "ask": 2.20,
                     "oi": 5000, "volume": 3000},
                    ...
                ]
            }
        """
        async with self._req_lock:
            try:
                await self.options_req.send_json({
                    "action": "chains",
                    "symbol": symbol,
                    "greeks": greeks,
                })
                response = await self.options_req.recv_json()
            except asyncio.CancelledError:
                logging.warning(
                    f"[bridge] REQ socket cancelled for {symbol}, recreating"
                )
                self._close_options_req()
                self._create_options_req()
                return []
            except zmq.ZMQError as e:
                if e.errno == zmq.EAGAIN:
                    # Bridge timed out (market closed or no data) — treat as empty
                    return []
                logging.error(f"Failed to fetch chain for {symbol}: {e}")
                return []
            except Exception as e:
                logging.error(f"Failed to fetch chain for {symbol}: {e}")
                return []

        if not isinstance(response, dict):
            logging.warning(f"Invalid chain response for {symbol}: {response}")
            return []

        if response.get("error"):
            logging.warning(
                f"Chain request failed for {symbol}: {response.get('error')}"
            )
            return []

        contracts = []
        for opt_data in response.get("contracts", []):
            try:
                right_raw = opt_data.get("right", "C")
                contract = OptionContract(
                    symbol=symbol,
                    strike=float(opt_data.get("strike", 0)),
                    expiry=str(opt_data.get("expiry", "")),
                    right=self._normalize_right(right_raw),
                    conId=str(opt_data.get("conId", "")),
                    delta=float(opt_data["delta"]) if opt_data.get("delta") is not None else None,
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
                logging.warning(
                    f"Failed to parse option for {symbol}: {e}"
                )

        return contracts

    def _close_options_req(self) -> None:
        if self.options_req:
            try:
                self.options_req.close()
            except Exception:
                pass
            self.options_req = None

    def _create_options_req(self) -> None:
        self.options_req = self.ctx.socket(zmq.REQ)
        self.options_req.setsockopt(zmq.RCVTIMEO, 5000)
        self.options_req.connect(
            f"tcp://{self.zmq_host}:{self.chain_port}"
        )

    # ── Ranking algorithm (SPEC-QUINN §3.3) ────────────────────────────────

    def _spread_cost_acceptable(
        self, contract: OptionContract, stock_price: float
    ) -> bool:
        """
        Spread rule from SPEC-QUINN §3.1 table:
          - stock < $3: bid-ask spread ≤ $0.05
          - stock ≥ $3: bid-ask spread ≤ $0.10
        """
        spread = contract.ask - contract.bid
        max_spread = MAX_SPREAD_LOW if stock_price < 3.0 else MAX_SPREAD_HIGH
        return spread <= max_spread + 1e-9

    def _score_contract(
        self,
        c: OptionContract,
        direction: str,
        stock_price: float,
    ) -> float:
        """
        Score a single contract (lower = better).

        Burst-mode scoring uses Greek-aware delta targets:
          - CALLS: ideal delta 0.60 (range 0.55–0.65)
          - PUTS:  ideal delta 0.40 (range 0.35–0.45)
        Positive gamma preferred for intraday gamma plays.
        """
        # Delta target depends on right and direction
        if c.right == "CALL":
            delta_target = DELTA_TARGET_CALL
        else:
            delta_target = DELTA_TARGET_PUT

        # delta_penalty: 0 at ideal, 1 at worst (edge of acceptable band)
        delta_error = abs(c.delta - delta_target)
        delta_penalty = min(delta_error / DELTA_BAND, 1.0)

        # DTE penalty: 0 for 0 DTE, 1 for DTE_MAX
        dte = self._calc_dte(c.expiry) or 0
        dte_penalty = dte / DTE_MAX

        # spread_penalty
        spread = c.ask - c.bid
        max_spread = (
            MAX_SPREAD_LOW if stock_price < 3.0 else MAX_SPREAD_HIGH
        )
        spread_penalty = min(spread / max_spread, 1.0) if max_spread > 0 else 0

        return (
            delta_penalty  * WEIGHT_DELTA
            + dte_penalty  * WEIGHT_DTE
            + spread_penalty * WEIGHT_SPREAD
        )

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

            # None delta — can't score, filter out
            if c.delta is None:
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
            c.score = self._score_contract(c, direction, stock_price)

        # Sort by score ascending (lower = better)
        filtered.sort(key=lambda x: x.score)

        # Assign ranks
        for i, c in enumerate(filtered):
            c.rank = i + 1

        return Rankings(
            symbol=(
                filtered[0].symbol
                if filtered else ""
            ),
            direction=direction,
            stock_price=stock_price,
            timestamp=time.time(),
            contracts=filtered,
        )

    # ── Burst mode (SPEC-QUINN §12) ────────────────────────────────────────

    async def _burst_loop(self) -> None:
        """
        Fetch + rank every BURST_INTERVAL_SEC for the activated symbol.
        Cache the best contract. Runs while self._state == BURST.
        Resilient to individual fetch failures — loop continues until deactivated.
        """
        symbol = self._burst_symbol
        direction = self._burst_direction

        while self._state == QuinnState.BURST:
            try:
                stock_price = self.current_prices.get(symbol, 0.0)

                chain = await self.fetch_option_chain(symbol, greeks=True)
                if chain:
                    rankings = self.rank_contracts(chain, direction, stock_price)
                    if rankings.contracts:
                        self._cached_best = rankings.contracts[0]
                        self.rankings[f"{symbol}|{direction}"] = rankings
                        logging.info(
                            f"[burst] {symbol} {direction} → "
                            f"${self._cached_best.strike:.2f} "
                            f"delta={self._cached_best.delta:.3f} "
                            f"gamma={self._cached_best.gamma:.4f} "
                            f"theta={self._cached_best.theta:.4f} "
                            f"score={self._cached_best.score:.3f}"
                        )
                    else:
                        logging.warning(
                            f"[burst] No qualifying contracts for {symbol}"
                        )
                        self._cached_best = None
                else:
                    logging.warning(
                        f"[burst] Empty chain response for {symbol}"
                    )
                    self._cached_best = None

            except asyncio.CancelledError:
                # Re-raise so _cancel_burst_tasks can handle it
                raise
            except Exception as e:
                logging.error(
                    f"[burst] Unhandled error in burst loop for {symbol}: {e}",
                    exc_info=True,
                )
                # Continue looping — stay in BURST, retry next interval
                self._cached_best = None

            await asyncio.sleep(BURST_INTERVAL_SEC)

    async def _burst_timeout(self) -> None:
        """
        Auto-exit BURST mode after BURST_TIMEOUT_SEC of no execute.
        """
        await asyncio.sleep(BURST_TIMEOUT_SEC)
        if self._state == QuinnState.BURST:
            logging.info(
                f"[burst] 60s timeout — returning to IDLE"
            )
            await self._exit_burst()

    async def _enter_burst(
        self, symbol: str, direction: str
    ) -> None:
        """Enter BURST mode for the given symbol and direction."""
        # Cancel any existing burst tasks
        await self._cancel_burst_tasks()

        self._state = QuinnState.BURST
        self._burst_symbol = symbol
        self._burst_direction = direction
        self._cached_best = None

        # Evict all rankings except the one we're about to refresh
        # to prevent unbounded dict growth over a trading session
        active_key = f"{symbol}|{direction}"
        self.rankings = {k: v for k, v in self.rankings.items() if k == active_key}

        # Start burst loop and timeout concurrently
        self._burst_task = asyncio.create_task(self._burst_loop())
        self._burst_timeout_task = asyncio.create_task(self._burst_timeout())

        logging.info(
            f"[burst] Activated for {symbol} {direction} "
            f"(interval={BURST_INTERVAL_SEC}s, timeout={BURST_TIMEOUT_SEC}s)"
        )

    async def _exit_burst(self) -> None:
        """Exit BURST mode and return to IDLE."""
        await self._cancel_burst_tasks()
        self._state = QuinnState.IDLE
        self._burst_symbol = ""
        self._burst_direction = ""
        self._cached_best = None
        logging.info("[burst] Deactivated — returned to IDLE")

    async def _cancel_burst_tasks(self) -> None:
        """Cancel burst loop and timeout tasks."""
        if self._burst_task and not self._burst_task.done():
            self._burst_task.cancel()
            try:
                await self._burst_task
            except asyncio.CancelledError:
                logging.debug("[burst] Burst loop cancelled")
            except Exception as e:
                logging.warning(f"[burst] Unexpected error cancelling burst loop: {e}")
            self._burst_task = None

        if self._burst_timeout_task and not self._burst_timeout_task.done():
            self._burst_timeout_task.cancel()
            try:
                await self._burst_timeout_task
            except asyncio.CancelledError:
                logging.debug("[burst] Timeout task cancelled")
            except Exception as e:
                logging.warning(f"[burst] Unexpected error cancelling timeout: {e}")
            self._burst_timeout_task = None

    # ── Background tasks ───────────────────────────────────────────────────

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
                break
            except zmq.ZMQError as e:
                if e.errno == zmq.EAGAIN:
                    continue  # timeout, no data yet
                logging.warning(f"recv_string error: {e}")
                continue
            except Exception as e:
                logging.warning(f"recv_string error: {e}")
                continue

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

            price = (
                tick.get("last")
                or tick.get("bid")
                or tick.get("ask")
            )
            if price:
                self.current_prices[sym] = price

    # ── REP request handler (SPEC-QUINN §12.5) ────────────────────────────

    async def handle_requests(self) -> None:
        """
        Handle algo queries on port 5560 (REP).

        Supported actions:
          activate   — enter BURST mode for symbol/direction
          execute    — return cached best contract (<10ms latency)
          deactivate — return to IDLE
          recommend  — backwards-compatible: serve from rankings cache

        Execute response (SPEC-QUINN §12.5):
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

        Error:
            {"status": "error", "message": "..."}
        """
        while self._running:
            try:
                msg = await self.recommendation_rep.recv_json()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"recv_json error: {e}")
                continue

            action    = msg.get("action", "")
            symbol    = msg.get("symbol", "")
            direction = msg.get("direction", "long")

            # ── activate ──────────────────────────────────────────────
            if action == "activate":
                await self._handle_activate(symbol, direction)
                continue

            # ── execute ───────────────────────────────────────────────
            if action == "execute":
                await self._handle_execute(symbol, direction)
                continue

            # ── deactivate ────────────────────────────────────────────
            if action == "deactivate":
                await self._handle_deactivate(symbol)
                continue

            # ── recommend (backwards-compatible) ───────────────────────
            if action == "recommend":
                await self._handle_recommend(symbol, direction)
                continue

            # ── unknown action ────────────────────────────────────────
            await self._send_error(
                f"unknown_action: '{action}'", symbol, direction
            )

    async def _handle_activate(self, symbol: str, direction: str) -> None:
        """Enter BURST mode for symbol/direction."""
        if not symbol:
            await self._send_error("symbol_required", symbol, direction)
            return

        # Restart timeout on every activate
        if self._state == QuinnState.BURST:
            if (
                self._burst_timeout_task
                and not self._burst_timeout_task.done()
            ):
                self._burst_timeout_task.cancel()
                try:
                    await self._burst_timeout_task
                except asyncio.CancelledError:
                    logging.debug(
                        f"[burst] Timeout task cancelled during activate restart"
                    )
                except Exception as e:
                    logging.warning(
                        f"[burst] Unexpected error cancelling timeout: {e}"
                    )

        await self._enter_burst(symbol, direction)
        norm_direction = "call" if direction == "long" else "put"

        try:
            await self.recommendation_rep.send_json({
                "status": "ok",
                "symbol": symbol,
                "direction": norm_direction,
                "state": self._state.value,
            })
        except Exception as e:
            logging.error(f"Failed to send activate response: {e}")

    async def _handle_execute(self, symbol: str, direction: str) -> None:
        """
        Return the cached best contract (<10ms latency).
        Resets the 60s BURST timeout on each execute.
        """
        if not symbol:
            await self._send_error("symbol_required", symbol, direction)
            return

        # Restart timeout
        if (
            self._state == QuinnState.BURST
            and self._burst_timeout_task
            and not self._burst_timeout_task.done()
        ):
            self._burst_timeout_task.cancel()
            try:
                await self._burst_timeout_task
            except asyncio.CancelledError:
                logging.debug(
                    f"[burst] Timeout task cancelled during restart for {symbol}"
                )
            except Exception as e:
                logging.warning(
                    f"[burst] Unexpected error cancelling timeout: {e}"
                )
            self._burst_timeout_task = asyncio.create_task(
                self._burst_timeout()
            )

        best = self._cached_best
        if not best:
            await self._send_error(
                f"no_{'call' if direction == 'long' else 'put'}_contracts_found",
                symbol, direction,
            )
            return

        underlying_price = (
            self.current_prices.get(symbol, 0.0)
        )
        norm_direction = "call" if direction == "long" else "put"

        response = {
            "status":           "ok",
            "symbol":           symbol,
            "direction":        norm_direction,
            "strike":           best.strike,
            "expiry":           best.expiry,
            "right":            best.right,
            "conId":            best.conId,
            "delta":            best.delta,
            "gamma":            best.gamma,
            "theta":            best.theta,
            "iv":               best.iv,
            "bid":              best.bid,
            "ask":              best.ask,
            "underlying_price": round(underlying_price, 4),
        }

        logging.info(
            f"Served execute: {symbol} {norm_direction} -> "
            f"${best.strike:.2f} {best.expiry} "
            f"delta={best.delta:.3f} gamma={best.gamma:.4f} "
            f"theta={best.theta:.4f} underlying=${underlying_price:.2f}"
        )

        try:
            await self.recommendation_rep.send_json(response)
        except Exception as e:
            logging.error(f"Failed to send execute response: {e}")

    async def _handle_deactivate(self, symbol: str) -> None:
        """Return to IDLE mode."""
        await self._exit_burst()

        try:
            await self.recommendation_rep.send_json({
                "status": "ok",
                "symbol": symbol,
                "state":  "idle",
            })
        except Exception as e:
            logging.error(f"Failed to send deactivate response: {e}")

    async def _handle_recommend(
        self, symbol: str, direction: str
    ) -> None:
        """
        Backwards-compatible recommendation from rankings cache.
        Works in both IDLE (stale) and BURST (fresh) modes.
        """
        key = f"{symbol}|{direction}"
        rankings = self.rankings.get(key)

        if not rankings or not rankings.contracts:
            await self._send_error(
                f"no_{'call' if direction == 'long' else 'put'}_contracts_found",
                symbol, direction,
            )
            return

        best = rankings.contracts[0]
        underlying_price = (
            rankings.stock_price
            or self.current_prices.get(symbol, 0.0)
        )
        norm_direction = "call" if direction == "long" else "put"

        response = {
            "status":           "ok",
            "symbol":           symbol,
            "direction":        norm_direction,
            "strike":           best.strike,
            "expiry":           best.expiry,
            "right":            best.right,
            "conId":            best.conId,
            "delta":            best.delta,
            "gamma":            best.gamma,
            "theta":            best.theta,
            "iv":               best.iv,
            "bid":              best.bid,
            "ask":              best.ask,
            "underlying_price": round(underlying_price, 4),
        }

        logging.info(
            f"Served recommend: {symbol} {norm_direction} -> "
            f"${best.strike:.2f} {best.expiry}"
        )

        try:
            await self.recommendation_rep.send_json(response)
        except Exception as e:
            logging.error(f"Failed to send recommend response: {e}")

    async def _send_error(
        self, message: str, symbol: str, direction: str
    ) -> None:
        """Send error response and log warning."""
        logging.warning(
            f"Recommendation error for {symbol} {direction}: {message}"
        )
        try:
            await self.recommendation_rep.send_json({
                "status":  "error",
                "message": message,
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
            self.process_ticks(),
            self.handle_requests(),
        )


# ============================================================================
# LOGGING  (SPEC-QUINN §7)
# ============================================================================

def setup_logging(log_dir: Path, process_name: str = "quinn") -> None:
    """
    Create per-process log file.
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

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    root.addHandler(console)

    logging.info(f"Logging to {log_path}")
    return log_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ib-quinn — Options Intelligence Layer"
    )
    parser.add_argument("--zmq-host",   default="127.0.0.1")
    parser.add_argument("--tick-port",  type=int, default=5555)
    parser.add_argument("--chain-port", type=int, default=5556)
    parser.add_argument("--rep-port",   type=int, default=5560)
    parser.add_argument(
        "--rep-host", default="127.0.0.1",
        help="Host to bind REP server for algo (default 127.0.0.1)"
    )
    parser.add_argument(
        "--log-dir", default="C:/hunter/algo/logs", dest="log_dir"
    )
    parser.add_argument(
        "--paper", action="store_true",
        help="Use paper trading: tick SUB shifts +1000, chain REQ stays 5556"
    )
    return parser.parse_args()


async def main_async(args: argparse.Namespace) -> None:
    setup_logging(Path(args.log_dir))

    # Paper mode: only tick SUB shifts +1000; chain REQ stays at default 5556
    tick_port  = args.tick_port  + 1000 if args.paper else args.tick_port
    chain_port = args.chain_port  # never shifts

    ctx = zmq.asyncio.Context()
    engine = QuinnEngine(
        ctx=ctx,
        zmq_host=args.zmq_host,
        tick_port=tick_port,
        chain_port=chain_port,
        rep_port=args.rep_port,
        rep_host=getattr(args, "rep_host", "127.0.0.1"),
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
            signal.signal(sig, lambda s, _: shutdown(s))

    def suppress_cancelled(loop_, context):
        exc = context.get("exception")
        if isinstance(exc, asyncio.CancelledError):
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
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    logging.info("ib-quinn starting — Options Intelligence Layer (burst mode)")

    # Paper mode: only tick SUB shifts +1000; chain REQ stays at default 5556
    tick_port  = args.tick_port  + 1000 if args.paper else args.tick_port
    chain_port = args.chain_port  # never shifts

    log, log_path = setup_logging(Path(args.log_dir), "quinn")

    # Launch banner
    mode = "PAPER" if args.paper else "LIVE"
    print("=" * 60, flush=True)
    print("IBKR Quinn Options Handler", flush=True)
    print("-" * 60, flush=True)
    print(f"ZMQ Host:  {args.zmq_host}", flush=True)
    print(f"Tick SUB:  {tick_port}  |  Chain REQ:  {chain_port}  |  REP:  {args.rep_port}", flush=True)
    print(f"Mode:      {mode}", flush=True)
    print(f"Log:       {log_path}", flush=True)
    print("=" * 60, flush=True)

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
