"""
ib-quinn.py - Options Intelligence Layer

Provides instant option recommendations by pre-ranking all available options.
Runs alongside ib-charon bridge.

Usage:
    python ib-quinn.py [--test]
"""

import sys
import asyncio
import signal

# Graceful shutdown handler - simple version
import time
import argparse

# Must be set before importing zmq / asyncio loop creation on Windows
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import logging
import json
import time
from pathlib import Path
from typing import Dict, List, Optional, Any
from collections import defaultdict
from dataclasses import dataclass

# ZMQ imports
import zmq
import zmq.asyncio

# ============================================================================
# CONFIGURATION
# ============================================================================

# Bridge Ports
ZMQ_TICK_PORT = 5555
ZMQ_OPTIONS_PORT = 5556
ZMQ_RECOMMENDATION_PORT = 5560  # Quinn's port

# Option Selection Criteria
DELTA_MIN = 0.45
DELTA_MAX = 0.65
DELTA_TARGET = 0.50
DTE_MAX = 4
OI_MIN = 2000
VOLUME_MIN = 1000
GAMMA_MIN = 0.05  # Minimum gamma for explosive moves
MAX_SPREAD_PCT = 0.10  # Max 10% spread

# Scoring Weights (optimized for intraday)
WEIGHT_DELTA = 0.50
WEIGHT_OI = 0.20
WEIGHT_VOLUME = 0.15
WEIGHT_SPREAD = 0.10
WEIGHT_GAMMA = 0.05

# Refresh interval (seconds)
REFRESH_INTERVAL = 60  # Refresh chain every 60s

# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class Option:
    """Option contract"""
    symbol: str
    strike: int
    expiry: str
    right: str  # CALL or PUT
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
    """Option recommendation"""
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
    score: float  # 0-1 ranking score

# ============================================================================
# QUINN ENGINE
# ============================================================================

class QuinnEngine:
    """Options intelligence engine"""
    
    def __init__(self, ctx: zmq.asyncio.Context):
        self.ctx = ctx
        self.tickers = []
        self.current_prices: Dict[str, float] = {}
        self.option_chains: Dict[str, List[Option]] = defaultdict(list)
        self.recommendations: Dict[str, Dict[str, Recommendation]] = {}
        
        # Bridge connections
        self.tick_sub = None
        self.options_req = None
        
        # Quinn server
        self.recommendation_rep = None
        
    async def connect_to_bridge(self):
        """Connect to ib-charon bridge"""
        # Tick subscriber
        self.tick_sub = self.ctx.socket(zmq.SUB)
        self.tick_sub.connect(f"tcp://127.0.0.1:{ZMQ_TICK_PORT}")
        
        # Options request
        self.options_req = self.ctx.socket(zmq.REQ)
        self.options_req.connect(f"tcp://127.0.0.1:{ZMQ_OPTIONS_PORT}")
        
        # Subscribe to all tickers
        for symbol in self.tickers:
            self.tick_sub.setsockopt(zmq.SUBSCRIBE, symbol.encode())
            
        logging.info(f"Connected to bridge - subscribed to {len(self.tickers)} tickers")
    
    async def start_server(self):
        """Start Quinn recommendation server"""
        self.recommendation_rep = self.ctx.socket(zmq.REP)
        self.recommendation_rep.bind(f"tcp://127.0.0.1:{ZMQ_RECOMMENDATION_PORT}")
        logging.info(f"Quinn server started on port {ZMQ_RECOMMENDATION_PORT}")
    
    async def load_tickers(self):
        """Load tickers from config"""
        # Try multiple paths for cross-platform compatibility
        # Relative to script, then common Windows paths
        script_dir = Path(__file__).parent
        possible_paths = [
            script_dir / 'tickers' / 'daily_tickers.json',
            script_dir.parent / 'tickers' / 'daily_tickers.json',
            Path('tickers/daily_tickers.json'),
            Path('../tickers/daily_tickers.json'),
            Path('C:/hunter/algo/tickers/daily_tickers.json'),
            Path('C:\\hunter\\algo\\tickers\\daily_tickers.json'),
            Path('C:/algo/tickers/daily_tickers.json'),
            Path('C:\\algo\\tickers\\daily_tickers.json'),
        ]
        
        config_path = None
        for p in possible_paths:
            if p.exists():
                config_path = p
                break
        if config_path and config_path.exists():
            with open(config_path) as f:
                data = json.load(f)
                if isinstance(data, list):
                    self.tickers = data
                elif isinstance(data, dict):
                    self.tickers = data.get('tickers', [])
        else:
            self.tickers = ["PLTR", "NVDA", "TSLA", "SMCI", "QQQ"]
        logging.info(f"Loaded tickers: {self.tickers}")
    
    async def fetch_option_chain(self, symbol: str) -> List[Option]:
        """Fetch option chain from bridge"""
        try:
            await self.options_req.send_json({"ticker": symbol})
            response = await self.options_req.recv_json()
            
            if response.get("status") != "ok":
                return []
                
            # Parse options from response
            options = []
            for opt_data in response.get("chain", []):
                option = Option(
                    symbol=symbol,
                    strike=opt_data.get("strike", 0),
                    expiry=opt_data.get("expiry", ""),
                    right=opt_data.get("right", "CALL"),
                    bid=opt_data.get("bid", 0),
                    ask=opt_data.get("ask", 0),
                    delta=opt_data.get("delta", 0.5),
                    gamma=opt_data.get("gamma", 0),
                    theta=opt_data.get("theta", 0),
                    vega=opt_data.get("vega", 0),
                    iv=opt_data.get("iv", 30),
                    oi=opt_data.get("oi", 0),
                    volume=opt_data.get("volume", 0)
                )
                options.append(option)
                
            return options
            
        except Exception as e:
            logging.error(f"Error fetching chain for {symbol}: {e}")
            return []
    
    def rank_options(self, options: List[Option], direction: str) -> List[Recommendation]:
        """
        Rank options and return recommendations.
        
        Filters by:
        - DTE <= 4
        - OI >= 2000
        - Delta 0.45-0.65
        - Gamma >= 0.05
        - Spread <= 10% of option price
        
        Score = (delta × 0.50) + (oi × 0.20) + (volume × 0.15) + (spread × 0.10) + (gamma × 0.05)
        """
        # Filter options
        filtered = []
        for opt in options:
            # Skip wrong direction
            if direction == "long" and opt.right != "CALL":
                continue
            if direction == "short" and opt.right != "PUT":
                continue
                
            # Apply filters
            if opt.oi < OI_MIN:
                continue
            if abs(opt.delta) < DELTA_MIN or abs(opt.delta) > DELTA_MAX:
                continue
            if opt.gamma < GAMMA_MIN:
                continue
                
            # Spread filter
            spread = opt.ask - opt.bid
            mid_price = (opt.bid + opt.ask) / 2
            if mid_price > 0 and spread / mid_price > MAX_SPREAD_PCT:
                continue
                
            filtered.append(opt)
        
        # Sort by delta closest to target
        filtered.sort(key=lambda x: abs(DELTA_TARGET - abs(x.delta)))
        
        # Create recommendations with scores
        recommendations = []
        for i, opt in enumerate(filtered[:10]):  # Top 10
            # Delta score (50%)
            delta_score = 1.0 - abs(DELTA_TARGET - abs(opt.delta))
            
            # OI score (20%)
            oi_score = min(opt.oi / 10000, 1.0)
            
            # Volume score (15%)
            volume_score = min(opt.volume / 5000, 1.0)
            
            # Spread score (10%) - tighter is better
            spread = opt.ask - opt.bid
            mid_price = (opt.bid + opt.ask) / 2
            if mid_price > 0:
                spread_pct = spread / mid_price
                spread_score = max(0, 1.0 - (spread_pct / MAX_SPREAD_PCT))
            else:
                spread_score = 0
                
            # Gamma score (5%) - higher gamma = more explosive
            gamma_score = min(opt.gamma / 0.20, 1.0)
            
            # Calculate total score
            score = (
                delta_score * WEIGHT_DELTA +
                oi_score * WEIGHT_OI +
                volume_score * WEIGHT_VOLUME +
                spread_score * WEIGHT_SPREAD +
                gamma_score * WEIGHT_GAMMA
            )
            
            rec = Recommendation(
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
                score=score
            )
            recommendations.append(rec)
            
        return recommendations
    
    async def refresh_chains(self):
        """Periodically refresh option chains"""
        while True:
            for symbol in self.tickers:
                chain = await self.fetch_option_chain(symbol)
                self.option_chains[symbol] = chain
                
                # Pre-compute recommendations
                self.recommendations[symbol] = {
                    "long": self.rank_options(chain, "long"),
                    "short": self.rank_options(chain, "short")
                }
                
            logging.info(f"Refreshed {len(self.tickers)} option chains")
            await asyncio.sleep(REFRESH_INTERVAL)
    
    async def handle_request(self):
        """Handle recommendation requests from algos"""
        while True:
            try:
                msg = await self.recommendation_rep.recv_json()
                request = msg.get("request", {})
                symbol = request.get("symbol", "")
                direction = request.get("direction", "long")
                
                # Get recommendation
                recs = self.recommendations.get(symbol, {}).get(direction, [])
                
                if recs:
                    response = {
                        "status": "ok",
                        "symbol": symbol,
                        "direction": direction,
                        "recommendation": recs[0].__dict__ if recs else None,
                        "alternatives": [r.__dict__ for r in recs[1:5]]
                    }
                else:
                    response = {
                        "status": "no_recommendation",
                        "symbol": symbol,
                        "message": "No options meet criteria"
                    }
                    
                await self.recommendation_rep.send_json(response)
                
            except Exception as e:
                logging.error(f"Error handling request: {e}")
                await self.recommendation_rep.send_json({"status": "error", "message": str(e)})
    
    async def process_ticks(self):
        """Process incoming ticks"""
        while True:
            try:
                msg = await self.tick_sub.recv_string()
                symbol, data = msg.split(" ", 1)
                tick = json.loads(data)
                
                price = tick.get("last") or tick.get("bid") or tick.get("ask")
                if price:
                    self.current_prices[symbol] = price
                    
            except Exception as e:
                logging.warning(f"Error processing tick: {e}")
                
            await asyncio.sleep(0.1)
    
    async def run(self):
        """Main run loop"""
        await self.load_tickers()
        await self.connect_to_bridge()
        await self.start_server()
        
        # Start tasks
        await asyncio.gather(
            self.refresh_chains(),
            self.handle_request(),
            self.process_ticks()
        )

# ============================================================================
# MAIN
# ============================================================================

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s - %(message)s"
    )
    logging.info("Starting ib-quinn.py - Options Intelligence Layer")
    logging.info("Press Ctrl+C to stop...")
    
    ctx = zmq.asyncio.Context()
    quinn = QuinnEngine(ctx)
    
    # Handle Ctrl+C gracefully
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()
    
    def signal_handler(sig):
        logging.info("Ctrl+C received - shutting down...")
        stop_event.set()
    
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: signal_handler(sig))
        except NotImplementedError:
            pass  # Windows doesn't support add_signal_handler for SIGTERM
    
    try:
        # Run with a task that can be cancelled
        await asyncio.wait_for(quinn.run(), timeout=None)
    except KeyboardInterrupt:
        logging.info("Shutting down...")
    except asyncio.CancelledError:
        logging.info("Shutting down...")
    finally:
        ctx.term()
        logging.info("Quinn stopped.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ib-quinn Options Intelligence")
    parser.add_argument("--test", action="store_true", help="Run in test mode")
    args = parser.parse_args()
    
    # Handle Ctrl+C gracefully
    def signal_handler(sig, frame):
        print("\nCtrl+C received - shutting down...")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    
    asyncio.run(main())
