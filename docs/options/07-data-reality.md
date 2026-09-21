# 07 — Data reality: where intraday option prices can and cannot come from

**Read before OP12.** Builds on `docs/condor/07`, which remains authoritative for the tier model
and the Tier 2 caveat text. This file extends it to weekly expiries and to the two sleeves that
*buy* options, and says what each sleeve's evidence can honestly be.

## 1. What Baskfy has, or will have after OP3

| Data | Source | Depth | Good for |
|---|---|---|---|
| NIFTY 50 one-minute bars | Kite `historical_data(interval="minute")` on the index token, chunked, through the limiter | ~2015→ (OP0 verifies the earliest date) | **Tier 1** for every sleeve: O1's gate, O2's range/trend/trigger, O3's range/ER/gap — all on real paths |
| India VIX daily close | Kite `historical_data(interval="day")` → `index_snapshot_daily` | ~2015→ (OP0) | O2's VIX filter; Tier 2's flat IV |
| NIFTY 50 daily closes | `index_snapshot_daily` (backfilled) | 2011→ | previous close, O2's EMA20 |
| Live option quotes with depth and OI | Kite `quote()` | now only | plans, scans, paper fills — and, via the collector, a series |
| The NFO master | `instruments("NFO")` nightly → `op_contract` | from OP2 onward only; **past masters are not retrievable** | resolving strikes and lot sizes; the first night fixes the start of our own history |
| The frozen lab's observations | `kite-momentum-rebalancer/data/outputs/strangle_*` (read-only, unrebuildable) and any `options_forward.jsonl` from the desk's research script | unknown until OP0 inventories it | at best a partial check on Tier 2's premium model around ATM, nothing more |

## 2. What Baskfy does not have, and cannot fetch on its own

**Historical intraday option prices.** Kite serves history only for instruments in the current
master; an expired contract leaves the master and its history goes with it (the Zerodha developer
forum says so; OP0 confirms against the live API with one call for last week's expired ATM call and
records the error). With weekly expiries this bites harder than for the condor: every contract O1-W,
O2 and O3 would have traded in the past is expired. **No intraday option backtest of any sleeve can
be built from Kite.**

**NSE's F&O bhavcopy** (UDiFF, free, archived) is **end-of-day** per contract — open, high, low,
close, settle, OI, volume. It cannot place a 09:45 entry, a 30 % stop or a 15:00 exit. It *can*
check one thing honestly: whether Tier 2's modelled premiums for the strikes Tier 2 chose are in the
right neighbourhood of the real **close/settle** on those days. OP12 runs that check if OP0 finds the
file reachable through the existing NSE provider (cookie/header discipline, Track C §9); the result
is a calibration table, never a P&L.

**Minute-level historical chains with bid/ask** exist only from paid vendors. Buying one is a new
provider and a licensing question (D10-adjacent) — **NEEDS-MAULIK** (QUESTIONS Q5), raised by OP0
with the loader's field list (`op_chain_snapshot` with `source = VENDOR`) so a purchase drops
straight into Tier 3.

## 3. The forward dataset — the collector

From the day OP3's collector is deployed, every trading minute 09:15–15:30 writes the two nearest
expiries' strikes within ±15 of ATM, both types, with depth and OI — one `quote()` call. It runs
whether or not any sleeve trades and on every trading day, not only expiries: O2 needs every day; O1
and O3 need the expiry days *and* the Monday chains that would let Tier 3 test PACK.7's rejected
"day-before" variant. A skipped day's chain is as valuable as a traded day's — it is the
counterfactual.

What a year of collection buys: ~250 O2 days; ~40 O1-W/O3 weekly expiries; 12 O1-M monthly
expiries. That is **why the paper periods differ by sleeve** (`02` §3.2) and why the O1-M sample
banner stays up the longest.

## 4. The tiers per sleeve, and what each may claim

| Tier | Prices | O1 (sell) | O2 (buy) | O3 (debit spread) | Must say |
|---|---|---|---|---|---|
| **1 — signals** | none | how often it trades; which filter works | how often the trend-aligned break fires; the index's move after it (MFE/MAE in points) before the hard exit | same, per setup | "No P&L. This measures selectivity, not profitability." |
| **2 — modelled** | Black-76 at the previous day's VIX, flat across strikes, forward = spot | the *shape*: ½C vs 1.5C vs 14:30 on real paths | the shape of stop vs target vs time stop — **least trustworthy**: a bought option's intraday IV changes dominate and a flat-IV model misses them | the shape; less IV-sensitive than O2 because both legs share the IV error | condor `07` §4's caveat verbatim; for O2 also `04` §13.2's sentence |
| **3 — observed** | `op_chain_snapshot` (collector and/or vendor), depth-ladder fills, full costs | a P&L, an expectancy in R, a drawdown | same | same | the sample size and start date on every card; the banner below `tier3_min_sessions` |

A lower tier's number never appears without its label and never in the same card as a higher tier's.
`op_backtest_run.caveats` stores the text; the page renders it from the row.

## 5. What the paper periods add that no backtest can

Fills against live depth at the moments the sleeves actually trade (09:45, 10:00, a 5-minute break
at 11:20, a 14:45 exit); the broker's real margin numbers; Kite's quote timing on busy expiry
afternoons; whether a 30-minute confirm window is workable for O2's intraday triggers when Maulik is
away from the desk (a lapsed plan is data — the journal counts lapses by sleeve); and whether the
desk's clock, token bridge and limiter hold with swing, TWT, VBT and the options monitor all running.
The paper record is evidence about the **machinery**; Tier 3 is evidence about the **strategy**.
`02` §3 needs both.

## 6. What is honestly unknowable in v1

* Whether any sleeve is profitable after costs — until Tier 3 has its sample.
* How O2's fills behave on a real breakout minute (spreads widen exactly when it buys) — the paper
  fills walk the snapshot's depth one minute old; `latency_ticks` is a guess until live fills exist.
* Whether weekly expiries will exist in their current form: SEBI has already cut them to one index
  per exchange (Nov 2024) and may act again. The calendar reads the master, so the code survives a
  change; the sleeves' samples may not.
