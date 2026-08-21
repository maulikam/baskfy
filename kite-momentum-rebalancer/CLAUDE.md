# Kite Momentum Rebalancer

> **Part of [Baskfy](../CLAUDE.md).** The root `CLAUDE.md` governs — it carries the two laws,
> the desk's seven non-negotiables and the screener's nine house rules. This file remains
> authoritative for this tree's internals.

Personal momentum-portfolio system for Indian cash equities (NSE, delivery/CNC only).
Flow: upload weekly momentum scan CSV → score → build rebalance plan vs live Kite
holdings → review in webview → explicit confirm → orders via Kite Connect API →
GTT stop-losses placed for every position.

## Architecture (event-driven, latency-aware)
- `app/core/` — guards (SGB hard-block), rate limiter (Kite caps), risk manager +
  kill switch, async OrderGateway (sole order path), websocket TickBus.
- `app/strategies/` — plugin engines: momentum_weekly (LIVE), intraday_skeleton, and
  defined-risk option planners in options.py (PAPER-ONLY, gated OFF). New strategies
  subclass strategies/base.py.
- Perf: uvloop + orjson; hot path is in-memory; sync kiteconnect calls run in threads.
  Kite's own floor is ~100-300ms order round-trip, 10 req/s, ~1 tick/sec/instrument —
  design within it (host in AWS Mumbai for the lowest RTT). Zerodha's backend being Go
  does NOT change this client-side floor; a Rust/Go rewrite buys nothing on execution.
- Compliance: SEBI retail-algo framework (2025) — static IP required for order APIs;
  <10 orders/sec needs no algo registration. The rate limiter enforces ≤9 OPS.

## Legacy layout
- `app/main.py` — FastAPI server + webview (upload, plan review, execute with confirm)
- `app/scoring.py` — Momentum Quality Score /100 (the strategy brain; see skill below)
- `app/rebalance.py` — target weights, deltas vs holdings, cash reconciliation, stops
- `app/kite_client.py` — Kite Connect wrapper (auth, holdings incl. pledged qty, margins, LTP, orders, GTT)
- `app/config.py` — every strategy knob (filters, weights, caps). Change behaviour HERE, not in code.
- `app/analytics/` — SQLite persistence (data/portfolio.db), EOD snapshots, metrics,
  PRI/TRI benchmarks, regime adapters/store/view. Read-only w.r.t. the market.
- `app/core/regime*.py` — regime overlay: pure domain engine + sleeve allocation solver.
  `/regime` and `/regime/backtest` are read-only status pages.
- `.claude/skills/momentum-rebalance/SKILL.md` — full strategy spec. READ IT before touching scoring/rebalance logic.

## Non-negotiable rules (do not "improve" these away)
1. **Never auto-execute.** Orders fire only from POST /execute with `confirm=true` AND the
   plan_id issued by /analyze. DRY_RUN=true in .env must simulate.
2. **Holdings quantity = `quantity` + `t1_quantity` + `collateral_quantity`** (pledged shares
   count as held — missing this once caused a 103-share position error).
3. Pledged shares SELL DIRECTLY on Zerodha (instant-sale, Oct-2024 feature) — no unpledge
   gate. Plan flags them only as info: collateral margin reduces when they're sold.
4. Every buy gets a GTT stop the same session (vol-scaled 8–12%, `stop_from_vol()`).
5. Product gates: rebalancer is CNC-only. MIS requires config.INTRADAY_ENABLED, NFO/BFO
   requires config.OPTIONS_ENABLED — both enforced inside core/gateway.py, default OFF.
6a. ALL order flow goes through core/gateway.py (guards → risk → rate-limits → journal).
   Never call kc.place_order from a strategy. guards.py blocks SGB*/G-sec at the lowest
   layer — an untouchable instrument raises before any network call.
   POST /execute routes through the gateway (client_id = plan_id:symbol, so re-posting a
   plan cannot double-send). GTT stops still use kite_client.place_gtt_stop, which carries
   its own guard; the gateway has no GTT method yet.
6. Filter-rejected stocks (circuits, BE series) are never bought, even if user holds them —
   held rejects become capped "runner" positions or exits per config.
7. SGB / instruments in `config.EXCLUDED_SYMBOLS` are untouchable.

## Daily routine
`python -m scripts.daily` collects everything for the day: index history, benchmark PRI,
the EOD snapshot, breadth (with `--scan`), and an observe-mode regime preview. Every step
is idempotent, so re-running changes nothing, and one failure never stops the rest.
`--check` reports state without touching anything.

A missed session is NOT recoverable: kc.margins() has no history, so a backfilled snapshot
cannot know that day's cash, and breadth cannot be rebuilt from a scan you no longer have.
Kite tokens expire daily with no refresh, so a scheduled run needs you to have logged in
first — see scripts/com.momentum.daily.plist.example.

## Commands
- Run dev server: `uvicorn app.main:app --reload --port 8420`
- Test scoring on a CSV: `python -m app.scoring data/uploads/scan.csv`
- Kite MCP (read-only checks while developing) is configured in `.claude/settings.json`;
  order placement in the app goes through kiteconnect (API), not MCP.

## Position sizing (changed 2026-08-14)
TARGET_POSITIONS (12, 15), MAX_SINGLE_WEIGHT 15%, MIN_POSITION_WEIGHT 6%, cluster cap 25%.
Under the regime overlay these are percentages of the ACTIVE EQUITY SLEEVE, not of NAV:
in R3 the sleeve is 40% of NAV, so a 10% sleeve position is 4% of NAV. With the overlay
disabled the sleeve is 100% and they behave as plain NAV weights.

## When asked to modify strategy logic
Update `config.py` constants first; only touch `scoring.py` formulas if the skill file's
spec changed. After any scoring change, run it against `data/uploads/` sample and diff
ranks before and after — print the top-25 delta table in your response.
