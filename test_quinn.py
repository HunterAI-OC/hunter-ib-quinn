#!/usr/bin/env python3
"""
Manual test script for Quinn 5560 REP interface.

Tests the full activate → execute → deactivate sequence.
Requires Quinn running (with or without --paper).

Usage:
    python test_quinn.py
"""

import argparse
import json
import sys
import time

import zmq


def send_command(sock, action, symbol, direction=None):
    """Send a command to Quinn and return the response."""
    msg = {"action": action, "symbol": symbol}
    if direction is not None:
        msg["direction"] = direction

    print(f"\n>>> Sending: {json.dumps(msg)}")
    sock.send_json(msg)
    resp = sock.recv_json()
    print(f"<<< Received: {json.dumps(resp, indent=2)}")
    return resp


def main():
    parser = argparse.ArgumentParser(description="Test Quinn 5560 interface")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5560)
    args = parser.parse_args()

    ctx = zmq.Context()
    sock = ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.RCVTIMEO, 5000)
    sock.setsockopt(zmq.LINGER, 1000)
    sock.connect(f"tcp://{args.host}:{args.port}")

    print(f"Connected to Quinn at tcp://{args.host}:{args.port}")

    try:
        # Test 1: recommend (IDLE mode — no chain fetch)
        print("\n=== TEST 1: recommend (IDLE mode) ===")
        resp = send_command(sock, "recommend", "AAPL", "long")
        if resp.get("status") == "error":
            print(f"  → Expected error (no cached rankings): {resp.get('reason')}")

        # Test 2: activate (enter BURST)
        print("\n=== TEST 2: activate (enter BURST) ===")
        resp = send_command(sock, "activate", "AAPL", "long")
        if resp.get("status") == "ok":
            print(f"  → Quinn entered BURST mode")
        elif resp.get("status") == "error":
            print(f"  → Bridge may not have chain data: {resp.get('reason')}")

        # Brief pause — burst loop fetches once on activate
        time.sleep(2)

        # Test 3: execute (should return cached best contract)
        print("\n=== TEST 3: execute (cached contract) ===")
        resp = send_command(sock, "execute", "AAPL", "long")
        if resp.get("status") == "ok":
            print(f"  → Best contract returned:")
            print(f"     strike={resp.get('strike')} expiry={resp.get('expiry')} "
                  f"right={resp.get('right')} delta={resp.get('delta')}")
        elif resp.get("status") == "error":
            print(f"  → No cached contract: {resp.get('reason')}")

        # Test 4: deactivate (return to IDLE)
        print("\n=== TEST 4: deactivate (return to IDLE) ===")
        resp = send_command(sock, "deactivate", "AAPL")
        if resp.get("status") == "ok":
            print(f"  → Quinn returned to IDLE")
        elif resp.get("status") == "error":
            print(f"  → {resp.get('reason')}")

        # Test 5: short direction
        print("\n=== TEST 5: recommend short (IDLE) ===")
        resp = send_command(sock, "recommend", "SPY", "short")
        if resp.get("status") == "error":
            print(f"  → Expected error: {resp.get('reason')}")

    finally:
        sock.close()
        ctx.term()
        print("\nDone.")


if __name__ == "__main__":
    main()
