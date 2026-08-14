# Kite Momentum Rebalancer

Upload your weekly momentum scan CSV → review a complete rebalance plan built from the
Momentum Quality framework → confirm → CNC orders + GTT stops placed on Zerodha via Kite
Connect. Built to be developed further with Claude Code (CLAUDE.md + strategy skill included).

## Setup (one time)
1. **Kite Connect app**: create at https://developers.kite.trade (needs the Kite Connect
   subscription). Set Redirect URL to `http://127.0.0.1:8420/callback`.
2. `cp .env.example .env` and fill `KITE_API_KEY` / `KITE_API_SECRET`. Keep `DRY_RUN=true`.
3. `pip install -r requirements.txt`
4. (Optional) `data/sectors.csv` with `symbol,cluster` rows enables the 25% cluster cap.

## Daily use
```bash
./run.sh                     # http://127.0.0.1:8420
```
1. **Log in to Kite** (token lasts the trading day).
2. **Upload** the weekly scan CSV → Analyze. The plan table shows every order, weight,
   GTT stop, and pledged-share warnings. Nothing executes here.
3. **Execute plan…** opens the contract-note confirm. Sells run first, then buys, then
   GTT stops. A JSON execution log lands in `data/outputs/`.

## Safety model
- `DRY_RUN=true` simulates everything end-to-end. Flip to `false` only after the plan
  matches your manual expectations for a few sessions.
- Execution requires the plan_id from the same session + explicit confirm; plans expire
  in 30 minutes so you never fire on stale prices.
- CNC delivery only; pledged shares sell directly (collateral margin reduces — the UI
  flags affected symbols); excluded instruments (SGBs) are never touched.

## Working on it with Claude Code
```bash
cd kite-momentum-rebalancer && claude
```
`CLAUDE.md` gives Claude the architecture + non-negotiable rules; the strategy spec lives
in `.claude/skills/momentum-rebalance/SKILL.md`; the Kite MCP server is preconfigured for
read-only checks while developing (run `/mcp` in Claude Code to authenticate it).
Typical asks: "add a turnover-cost estimate column", "make cash bands VIX-aware",
"add a backtest command over data/uploads/*.csv".

## Performance & compliance notes (researched Aug 2026)
- Latency floor is Kite's API + your internet (~100-300ms round-trip), not your code.
  For the lowest RTT, run this on a VM in AWS Mumbai (ap-south-1) with a static IP
  (static IP is mandatory for API order placement under SEBI's 2025 framework).
- Rate caps enforced in code: 9 req/s, 9 orders/s (also keeps you under SEBI's
  10-OPS no-registration threshold), 380 orders/min, 2900/day.
- Ticker is ~1 tick/sec/instrument snapshots — not tick-by-tick; design strategies
  accordingly. 3 websocket connections × 3000 instruments max.
- API pricing: Kite Connect Personal is free (no market data); ₹500/month adds
  websocket market data. True HFT needs exchange co-location (~₹12-18L/yr) — out of
  scope by design.

**Disclaimer**: personal tooling, not investment advice; you are responsible for every
order your account places.
