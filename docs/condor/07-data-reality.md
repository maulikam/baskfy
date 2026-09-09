# 07 — Data reality: where expiry-day option prices can and cannot come from

**Read before OC9.** The method's deployment plan begins "backtest NIFTY and BANKNIFTY
separately using actual bid/ask prices". This document says, honestly, which parts of that this
repository can do today, which parts need a purchase only Maulik can make, and what each tier of
evidence is allowed to claim on the page. The run does not pretend a modelled number is an
observed one.

## 1. What Baskfy has

| Data | Source | Depth | Good for |
|---|---|---|---|
| Index one-minute bars (NIFTY 50, NIFTY BANK) | Kite `historical_data(interval="minute")` on the permanent index tokens, through the provider's limiter and its chunk windows | Kite serves minute candles for indices back to roughly 2015 (OC0 verifies the earliest date it actually returns and records it) | **Tier 1** — the whole gate of `04` §2, every monthly expiry, for real |
| India VIX daily close | Kite `historical_data(interval="day")` on the VIX index token, into `index_snapshot_daily` | 2015→ | **Tier 2**'s volatility input |
| Daily index closes | `index_snapshot_daily` (already backfilled) | 2011→ | the previous close for the gap test |
| Live option quotes with depth | Kite `quote()` — bid/ask, five levels each side, volume, OI | now only | the live plan; the **collector** (§3) turns "now" into a series |
| The frozen lab's observation series | `kite-momentum-rebalancer/data/outputs/strangle_*` — straddle records, journals, lockouts written by the box's collectors since the lab ran | unknown until inventoried; **read-only, unrebuildable** (docs/02 §6) | OC0 inventories it; if it holds per-minute ATM straddle prices for NIFTY on past expiry days it is a partial check on Tier 2's premium model, nothing more |

## 2. What Baskfy does not have, and cannot fetch on its own

**Historical option prices.** Kite's `historical_data` serves instruments that are in the
current master; expired contracts leave the master and their history goes with them — a
contract that expired last month cannot be queried today. (OC0 confirms this against the live
API rather than trusting this sentence: one call for last month's expired NIFTY ATM call, the
error recorded.) NSE's daily F&O bhavcopy is free and archived but is **end-of-day** per
contract — open, high, low, close, settle, OI — which cannot place a 10:00 entry, a 12:40 stop
or a 14:30 exit. Minute-level historical option chains with bid/ask exist only from paid vendors
(the well-known ones sell NSE F&O tick or minute history back several years). Buying one is a
**new provider** — Track C §9 — and a licensing question (D10-adjacent), so it is a
**NEEDS-MAULIK** item, raised by OC0 with the exact fields the loader needs (`oc_chain_snapshot`
with `source = VENDOR`) so that a purchase drops straight into Tier 3.

## 3. The forward dataset — the collector

Because the paper period is six monthly expiries anyway, the cheapest real data is the data the
book makes for itself. On every `is_trading_day`, from 09:15 to 15:30, the collector writes
`oc_chain_snapshot` once a minute for the `snapshot_strikes` [12] strikes either side of the
spot for both option types, with depth — two `quote()` calls a minute, well under the limiter,
and OC3 measures its share against the swing book's morning reads before the flag flips. After
six expiries the table holds six observed expiry days; after a year, twelve. That is small, and
`04` §11.3 says so on the page until it is not.

The collector runs whether or not the strategy trades that day and whether or not a plan was
built: a skipped day's chain is as valuable as a traded day's, because it is the counterfactual.

## 4. The tiers, and what each may claim

| Tier | Prices | May claim | Must say |
|---|---|---|---|
| **1 — the gate** | none | how often the book trades; which filter does the work; the funnel by year; whether the gate's thresholds sit on a cliff (a sensitivity table ±25 % on each) | "No P&L. This measures selectivity, not profitability." |
| **2 — synthetic** | Black–Scholes at the previous day's VIX, flat across strikes | the *shape* of outcomes: the fraction of traded days that hit ½C vs 1.5C vs 14:30 on the real index path; how the stop distance relates to the realised range | verbatim: "Prices are modelled, not observed. Real expiry-day premiums, skew and slippage differ from a flat-VIX Black–Scholes; treat the P&L as a shape, not a number." |
| **3 — observed** | `oc_chain_snapshot` (collector and/or vendor) with real depth, fills through the depth-ladder simulator, full costs | a P&L number, an expectancy in R, a drawdown — the only tier the real-money gate reads | the sample size and its start date on every card; the "insufficient sample" banner below `tier3_min_expiries` [12] |

A number from a lower tier never appears without its tier label, and never in the same card as
a higher tier's. `oc_backtest_run.caveats` stores the text and the page renders it from the row,
so a later edit to this document does not silently change what an old run claimed.

## 5. What the six paper expiries add that no backtest can

Fills against the live depth, the broker's real margin numbers, the actual timing of Kite's
quotes at 09:59 and 14:30, whether the 15-minute confirm window is workable for a human, and
whether the desk's clock, token bridge and limiter hold on a day the swing monitor is also
running. The paper record is evidence about the *machinery*; Tier 3 is evidence about the
*strategy*. The gate in `02` §3 needs both.
