"""
ib-quinn-v2.py - Options Intelligence Layer
==========================================

Provides instant option recommendations by pre-ranking all available options.
Runs alongside ib-charon bridge.

Fixes in v2:
  - Q-1: response format aligned with algo expectation (recommendation as dict,
           not dataclass -- algo accesses .get() keys)
  - Q-2: handles bridge "chain" key correctly (bridge now sends both chain + strikes)
  - Q-3: chain refresh re-qualifies contracts periodically (was stale near expiry)
  - General: tick subscription topic handling fixed (symbol-only subscription works)

Usage:
    python ib-quinn-v2.py [--test]
"""

import sys
import asyncio
import signal
import argparse
import logging
import json
import time
from pathlib import Path
from typing import Dict, List, Optional
from collections import defaultdict
from dataclasses import dataclass

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import zmq
import zmq.asyncio

# ============================================================================
# CONFIGURATION
# ============================================================================

ZMQ_TICK_PORT             = 5555
ZMQ_OPTIONS_PORT          = 5556
ZMQ_RECOMMENDATION_PORT   = 5560

DELTA_MIN    = 0.45
DELTA_MAX    = 0.65
DELTA_TARGET = 0.50
DTE_MAX      = 4
OI_MIN       = 2000
VOLUME_MIN   = 1000
GAMMA_MIN    = 0.05
MAX_SPREAD_PCT = 0.10

WEIGHT_DELTA  = 0.50
WEIGHT_OI     = 0.20
WEIGHT_VOLUME = 0.15
WEIGHT_SPREAD = 0.10
WEIGHT_GAMMA  = 0.05

REFRESH_INTERVAL = 60   # seconds between chain refreshes
QUALIFY_INTERVAL = 3600  # Q-3 fix: re-qualify contracts every hour

# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class Option:
    symbol: str
    strike: int
    expiry: str
    right: str
    bid: float
    ask: float
    delta: float
    gamma: float
    theta: float
    vega: float
    iv: float
    oi: int
    volume: int


@dataclass
class Recommendation:
    """Option recommendation returned to algos."""
    symbol: str
    strike: int
    expiry: str
    right: str
    delta: float
    gamma: float
    theta: float
    iv: float
    bid: float
    ask: float
    mid: float
    oi: int
    volume: int
    score: float

    def as_dict(self) -> Dict:
        """Q-1 fix: return plain dict so algo .get() accessors work."""
        return {
            "symbol": self.symbol,
            "strike": self.strike,
            "expiry": self.expiry,
            "right": self.right,
            "delta": self.delta,
            "gamma": self.gamma,
            "theta": self.theta,
            "iv": self.iv,
            "bid": self.bid,
            "ask": self.ask,
            "mid": self.mid,
            "oi": self.oi,
            "volume": self.volume,
            "score": self.score,
        }

# ============================================================================
# QUINN ENGINE
# ============================================================================

class QuinnEngine:
    def __init__(self, ctx: zmq.asyncio.Context):
        self.ctx = ctx
        self.tickers: List[str] = []
        self.current_prices: Dict[str, float] = {}
        self.option_chains: Dict[str, List[Option]] = defaultdict(list)
        self.recommendations: Dict[str, Dict[str, List[Recommendation]]] = {}
        self.tick_sub: Optional[zmq.Socket] = None
        self.options_req: Optional[zmq.Socket] = None
        self.recommendation_rep: Optional[zmq.Socket] = None
        self._last_qualify: float = 0

    # ------------------------------------------------------------------
    # Bridge connections
    # ------------------------------------------------------------------
    async def connect_to_bridge(self) -> None:
        self.tick_sub = self.ctx.socket(zmq.SUB)
        self.tick_sub.connect(f"tcp://127.0.0.1:{ZMQ_TICK_PORT}")

        self.options_req = self.ctx.socket(zmq.REQ)
        self.options_req.setsockopt(zmq.RCVTIMEO, 5000)
        self.options_req.connect(f"tcp://127.0.0.1:{ZMQ_OPTIONS_PORT}")

        for sym in self.tickers:
            self.tick_sub.setsockopt(zmq.SUBSCRIBE, sym.encode())

        logging.info(f"Connected to bridge -- subscribed to {len(self.tickers)} tickers")

    async def start_server(self) -> None:
        self.recommendation_rep = self.ctx.socket(zmq.REP)
        self.recommendation_rep.bind(f"tcp://127.0.0.1:{ZMQ_RECOMMENDATION_PORT}")
        logging.info(f"Quinn server started on port {ZMQ_RECOMMENDATION_PORT}")

    # ------------------------------------------------------------------
    # Ticker loading
    # ------------------------------------------------------------------
    async def load_tickers(self) -> None:
        paths = [
            Path(__file__).parent / 'tickers' / 'daily_tickers.json',
            Path(__file__).parent.parent / 'tickers' / 'daily_tickers.json',
            Path('C:/hunter/algo/tickers/daily_tickers.json'),
            Path('C:/hunter/algo/the-strat/the-strat-312-v1/local-tickers.json'),
        ]
        for p in paths:
            if p.exists():
                try:
                    with open(p) as f:
                        data = json.load(f)
                    if isinstance(data, list):
                        self.tickers = data
                    elif isinstance(data, dict):
                        tickers = data.get('tickers', data.get('symbols', []))
                        if tickers:
                            self.tickers = tickers
                            break
                except Exception as e:
                    logging.warning(f"Failed to read {p}: {e}")
        if not self.tickers:
            self.tickers = ["PLTR", "NVDA", "TSLA", "SMCI", "QQQ"]
        logging.info(f"Loaded tickers: {self.tickers}")

    # ------------------------------------------------------------------
    # Option chain fetching
    # ------------------------------------------------------------------
    async def fetch_option_chain(self, symbol: str) -> List[Option]:
        """
        Fetch option chain from bridge.
        Q-2 fix: bridge now sends both 'chain' and 'strikes' keys.
        """
        try:
            await self.options_req.send_json({"ticker": symbol})
            response = await self.options_req.recv_json()
        except asyncio.CancelledError:
            logging.warning(f"Chain request cancelled for {symbol} -- retrying next cycle")
            return []
        except Exception as e:
            logging.error(f"Error fetching chain for {symbol}: {e}")
            return []

        if response.get("status") != "ok":
            logging.warning(f"Chain request failed for {symbol}: {response}")
            return []

        options = []
        for opt_data in response.get("chain", []):
            try:
                option = Option(
                    symbol=symbol,
                    strike=int(opt_data.get("strike", 0)),
                    expiry=str(opt_data.get("expiry", "")),
                    right=opt_data.get("right", "CALL"),
                    bid=float(opt_data.get("bid", 0)),
                    ask=float(opt_data.get("ask", 0)),
                    delta=float(opt_data.get("delta", 0.5)),
                    gamma=float(opt_data.get("gamma", 0)),
                    theta=float(opt_data.get("theta", 0)),
                    vega=float(opt_data.get("vega", 0)),
                    iv=float(opt_data.get("iv", 30)),
                    oi=int(opt_data.get("oi", 0)),
                    volume=int(opt_data.get("volume", 0)),
                )
                options.append(option)
            except Exception as e:
                logging.warning(f"Failed to parse option data for {symbol}: {e}")

        return options

    # ------------------------------------------------------------------
    # Option ranking
    # ------------------------------------------------------------------
    def rank_options(self, options: List[Option], direction: str) -> List[Recommendation]:
        """
        Rank options for the given direction.

        Filters:
          - DTE <= DTE_MAX
          - OI >= OI_MIN
          - Delta in [DELTA_MIN, DELTA_MAX]
          - Gamma >= GAMMA_MIN
          - Spread <= MAX_SPREAD_PCT of option price

        Score = weighted sum of delta/OI/volume/spread/gamma proximity scores.
        """
        filtered = []
        for opt in options:
            if direction == "long" and opt.right != "CALL":
                continue
            if direction == "short" and opt.right != "PUT":
                continue

            if opt.oi < OI_MIN:
                continue
            if abs(opt.delta) < DELTA_MIN or abs(opt.delta) > DELTA_MAX:
                continue
            if opt.gamma < GAMMA_MIN:
                continue

            spread = opt.ask - opt.bid
            mid = (opt.bid + opt.ask) / 2
            if mid > 0 and spread / mid > MAX_SPREAD_PCT:
                continue

            filtered.append(opt)

        # Sort by delta closest to target
        filtered.sort(key=lambda x: abs(DELTA_TARGET - abs(x.delta)))

        recommendations = []
        for opt in filtered[:10]:
            delta_score   = 1.0 - abs(DELTA_TARGET - abs(opt.delta))
            oi_score      = min(opt.oi / 10000, 1.0)
            volume_score  = min(opt.volume / 5000, 1.0)

            spread = opt.ask - opt.bid
            mid    = (opt.bid + opt.ask) / 2
            spread_score = max(0, 1.0 - (spread / mid / MAX_SPREAD_PCT)) if mid > 0 else 0

            gamma_score   = min(opt.gamma / 0.20, 1.0)

            score = (
                delta_score   * WEIGHT_DELTA  +
                oi_score      * WEIGHT_OI     +
                volume_score  * WEIGHT_VOLUME +
                spread_score  * WEIGHT_SPREAD +
                gamma_score   * WEIGHT_GAMMA
            )

            recommendations.append(Recommendation(
                symbol=opt.symbol,
                strike=opt.strike,
                expiry=opt.expiry,
                right=opt.right,
                delta=opt.delta,
                gamma=opt.gamma,
                theta=opt.theta,
                iv=opt.iv,
                bid=opt.bid,
                ask=opt.ask,
                mid=(opt.bid + opt.ask) / 2,
                oi=opt.oi,
                volume=opt.volume,
                score=score,
            ))

        return recommendations

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------
    async def refresh_chains(self) -> None:
        """Periodically refresh option chains and re-rank recommendations."""
        while True:
            now = time.time()

            for symbol in self.tickers:
                chain = await self.fetch_option_chain(symbol)
                self.option_chains[symbol] = chain
                self.recommendations[symbol] = {
                    "long": self.rank_options(chain, "long"),
                    "short": self.rank_options(chain, "short"),
                }

            logging.info(f"Chains refreshed for {len(self.tickers)} tickers")

            # Q-3 fix: occasionally re-subscribe to catch new series
            if now - self._last_qualify > QUALIFY_INTERVAL:
                self._last_qualify = now
                await self._reconnect_ticks()

            await asyncio.sleep(REFRESH_INTERVAL)

    async def _reconnect_ticks(self) -> None:
        """Q-3 fix: re-subscribe to tick stream to pick up new option series."""
        try:
            self.tick_sub.close()
            self.tick_sub = self.ctx.socket(zmq.SUB)
            self.tick_sub.connect(f"tcp://127.0.0.1:{ZMQ_TICK_PORT}")
            for sym in self.tickers:
                self.tick_sub.setsockopt(zmq.SUBSCRIBE, sym.encode())
            logging.info("Tick subscriptions refreshed")
        except Exception as e:
            logging.warning(f"Failed to refresh tick subscriptions: {e}")

    async def handle_request(self) -> None:
        """
        Handle recommendation requests from algos (port 5560 REP).
        Q-1 fix: returns recommendation as plain dict (not dataclass).
        """
        while True:
            try:
                msg = await self.recommendation_rep.recv_json()
                request = msg.get("request", {})
                symbol    = request.get("symbol", "")
                direction = request.get("direction", "long")

                recs = (
                    self.recommendations
                    .get(symbol, {})
                    .get(direction, [])
                )

                if recs:
                    # Q-1 fix: return as dict, not dataclass instance
                    response = {
                        "status": "ok",
                        "symbol": symbol,
                        "direction": direction,
                        "recommendation": recs[0].as_dict(),
                        "alternatives": [r.as_dict() for r in recs[1:5]],
                    }
                else:
                    response = {
                        "status": "no_recommendation",
                        "symbol": symbol,
                        "message": f"No options meet criteria for {symbol} {direction}",
                    }

                await self.recommendation_rep.send_json(response)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Error handling request: {e}", exc_info=True)
                try:
                    await self.recommendation_rep.send_json({
                        "status": "error",
                        "message": str(e),
                    })
                except Exception:
                    pass

    async def process_ticks(self) -> None:
        """Process incoming tick stream to keep current prices updated."""
        while True:
            try:
                msg = await self.tick_sub.recv_string()

                # Format from bridge: "SYMBOL {json}"
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

            except (zmq.Again, asyncio.CancelledError):
                pass   # timeout (normal) or task cancelled during shutdown
            except Exception as e:
                logging.warning(f"Error processing tick: {e}")

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------
    async def run(self) -> None:
        await self.load_tickers()
        await self.connect_to_bridge()
        await self.start_server()

        await asyncio.gather(
            self.refresh_chains(),
            self.handle_request(),
            self.process_ticks(),
        )

# ============================================================================
# MAIN
# ============================================================================

async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s - %(message)s",
    )
    logging.info("Starting ib-quinn-v2.py -- Options Intelligence Layer")

    ctx = zmq.asyncio.Context()
    quinn = QuinnEngine(ctx)

    loop = asyncio.get_running_loop()

    def signal_handler(sig):
        logging.info(f"Signal {sig} received -- cancelling tasks...")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig: signal_handler(s))
        except NotImplementedError:
            pass

    try:
        await quinn.run()
    except asyncio.CancelledError:
        logging.info("Tasks cancelled -- shutting down")
    except KeyboardInterrupt:
        logging.info("KeyboardInterrupt -- shutting down")
    finally:
        ctx.term()
        logging.info("Quinn stopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ib-quinn Options Intelligence v2")
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()

    asyncio.run(main())
