# How We Work — Bridge & Quinn

This is the working agreement between ByteMe (bridge) and Cisco (quinn). Read it, understand it, follow it.

---

## The Split

**ByteMe** owns the bridge.
- Connects to IBKR, ingests ticks, builds candles, publishes via ZMQ
- Owns: `ib-charon.py`, time bucketer, ZMQ publishers, Parquet persistence
- Repo: `hunter-connector`
- Workspace: `C:\hunter\algo\connector\`

**Cisco** owns Quinn.
- Options intelligence layer — fetches chains, ranks contracts, serves recommendations
- Owns: `ib-quinn.py`, ranking algorithm, REP server on port 5560
- Repo: `hunter-ib-quinn`
- Workspace: `C:\hunter\algo\quinn\`

**Hunter** coordinates. He moves between channels and decides what gets merged.

---

## The Interface Between Us

We meet at the ZMQ ports — not in the chat.

| Port | Meaning |
|------|---------|
| 5555 | Bridge publishes 1S bars → Quinn subscribes |
| 5556 | Quinn requests option chains from Bridge |
| 5560 | Algo queries Quinn for recommendations |

We don't edit each other's files. Ever. If Cisco needs something from the bridge, he requests it through the port or asks Hunter to relay the message. If I need something from Quinn, same thing.

---

## The Git Strategy

**ByteMe is the only one who commits.** Cisco can push branches, but I review and merge. No exceptions.

```
main branch = last stable working version
  └── Every commit to main = a tested, working milestone

Feature / fix work:
  └── Create a new branch from main
  └── Write the code, test it
  └── Push branch to origin
  └── ByteMe reviews, merges to main
  └── Tag the merge as v1, v2, etc.
```

**Branching rules:**
- Branch names: `feat/stage-1-bridge-connect`, `fix/tick-bucket-gap`, etc.
- Never commit broken code to main
- Never push directly to main — always through a branch
- If it's not tested, it doesn't get merged

---

## What We're Building This Phase

**Phase goal:** A working tick-to-candle pipeline.

```
IBKR tick-by-tick
    → Time bucketer (1S windows)
    → ZMQ pub (port 5555)
    → Quinn subscribes, fetches chains, ranks contracts
    → Algo queries Quinn (port 5560), places trades
```

**Build order (from SPEC-BRIDGE.md):**

1. **Stage 1** — Connect to IBKR, log raw ticks. Verify exchange timestamps.
2. **Stage 2** — Time bucketer: group ticks into 1S windows, emit OHLCV bars.
3. **Stage 3** — ZMQ PUB on port 5555. Simple subscriber test.
4. **Stage 4** — MTF builders (5M / 15M aggregation).
5. **Stage 5** — Parquet persistence.
6. **Stage 6** — REP ports (price query 5564, option chains 5556).
7. **Stage 7** — Quinn integration.

Each stage is a working, testable milestone. We don't skip stages. We test before moving on.

---

## The Specs

These are the contracts:

- `SPEC-BRIDGE.md` — Full bridge build specification
- `SPEC-BUCKETER.md` — Pure time-bucketing engine (vendor-agnostic, no IBKR dependency)
- `SPEC-QUINN.md` — Options intelligence layer specification

No code gets written until the spec is agreed. If the spec needs changing, we discuss it first.

---

## How We Communicate

We are in **different channels** — we cannot see each other's messages. This is intentional.

**Communication path:**
- Cisco → Hunter → ByteMe (and vice versa)
- Or: drop a message in the shared channel for Hunter to relay

**Real-time coordination happens at the ZMQ interface** — if Quinn is running and the bridge is running, they are already talking through the ports. That's the real communication channel.

If Cisco needs the bridge to expose something new, he asks through Hunter. If I need Quinn to handle something differently, I do the same.

---

## What Success Looks Like

At the end of this phase:

- Bridge connects to IBKR and produces clean, accurate 1S candles
- 5M and 15M candles are correctly aggregated from 1S bars
- ZMQ pubs are flowing on all ports
- Quinn can fetch option chains via port 5556
- Quinn serves contract recommendations on port 5560
- Parquet files are being written to `C:\hunter\algo\data\parquet\`
- All processes log to `C:\hunter\algo\logs\`
- Hunter can run the full stack: bridge → Quinn → algo, and watch it work

We build in public. No black boxes. Every piece is testable.

---

## A Note From ByteMe

I've been through the hard lessons on this project already. Here's what I know:

1. **Test each stage.** Don't build the whole thing and then hope it works. Build a little, test a lot.
2. **Logs are everything.** Write to `logs/` on every important event. When something breaks — and it will — the log is how we figure out why.
3. **The ZMQ interface is the boundary.** Don't cross it. Cisco doesn't touch bridge files. I don't touch Quinn files. The port numbers are the contract.
4. **Git history matters.** A clean linear history with meaningful commit messages means we can always roll back to a known good state.

I'm ready to build this properly. Let's do it.

— ByteMe
