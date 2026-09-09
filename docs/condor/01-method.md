# 01 — The method: one range-filtered, fully hedged expiry-day iron condor

Settled by Maulik on 9 Sep 2026 from the attached research. This document is the strategy in
plain words; `04-business-rules.md` is the same strategy as numbers and functions, and the tests
assert `04`. Where the two disagree, `04` wins and this file is corrected.

## 1. The thesis, and its honest limit

On the day an index option expires, the premium left in out-of-the-money strikes decays to zero
by 15:30 unless the index moves through them. A seller who waits for the first forty-five
minutes to show a *contained* day, sells strikes outside that morning's range at a modest delta,
buys further-out wings so the worst case is known to the rupee before entry, and leaves well
before the last hour, is paid for taking a bounded, mostly time-driven risk. The filter is the
strategy: most of the edge (if there is any) is in the days *not* traded.

The honest limit: no part of this is demonstrated to be profitable after costs on NSE. SEBI's
FY25–FY26 study of individual traders in equity derivatives finds the segment overwhelmingly
loss-making for individuals. The attached research concluded the iron condor is the most
*defensible* candidate — bounded loss, no naked leg, no overnight — not that it wins. A no-trade
expiry is a valid, expected outcome, and the run must never make the desk feel that a day without
a trade was a day wasted.

## 2. Which index, and why only one

**NIFTY first.** It is the broader market; BANKNIFTY is a sector concentrated in a handful of
names and moves accordingly. This is a risk-design preference, not a claim that NIFTY earns more.

Both indices now expire monthly on the **last Tuesday**, so trading them together on the same
day is one bet on the same afternoon twice, not diversification. BANKNIFTY is therefore built
dark (Track B) and enabled only if its *own* backtest and paper record are better after every
cost — never because its premiums look larger.

Contract sizes at the time of writing: **NIFTY 65, BANKNIFTY 30**. The run never hard-codes
them; the instrument master is the source and the plan shows the value it used.

## 3. When to trade

* Monthly-expiry day only. NIFTY also has a weekly Tuesday expiry; those Tuesdays are **not**
  trading days for this book.
* Observe 09:15–09:59. Enter around 10:00 (a short window; a plan not confirmed by 10:15 lapses).
* One trade per expiry. No re-entry after any exit, for any reason, including a profit exit.
* Skip the day entirely on RBI policy days, the Union Budget, election-result days and any date
  Maulik has marked as an event day.
* Skip if any of these is true at 09:59:
  * the open gapped more than ~0.75 % from the previous close;
  * the 09:15–09:59 high–low range exceeds ~0.8 % of the index;
  * the 09:59 price is outside the 09:15–09:44 opening range;
  * the morning was a clean directional move — the efficiency ratio over 09:15–09:59 is above
    0.30, where ER = |P(09:59) − P(09:15)| ÷ Σ|one-minute changes|. A high ER means the path was
    efficient — trending — and a neutral short-premium structure is exactly what a trend hurts.

## 4. What to sell

A same-expiry iron condor, four legs:

* Short call at roughly 0.20–0.25 delta **and** above the opening-range high.
* Short put at roughly 0.20–0.25 delta **and** below the opening-range low.
* Long call and long put ("wings") beyond the shorts: NIFTY 150 points, BANKNIFTY 300, as the
  starting values to test.
* Net credit must be at least ~25–30 % of the wing width, or there is no trade.
* **Wings first.** The long legs are bought before the shorts are sold, so the account is never
  short an unhedged option even for a second — and so the margin the broker asks for is the
  hedged margin, not the naked one.
* Liquidity test: if bid–ask, depth or expected slippage put the round-trip cost above 20 % of
  the profit target, there is no trade.

## 5. When to leave

With C the opening net credit and D the current cost to close all four legs:

* Profit: exit when D ≤ 0.5 C.
* Stop: exit when D ≥ 1.5 C.
* Immediately exit if the index touches either short strike, whatever D says.
* Mandatory exit at **14:30**. Nothing of this book is open into the last hour of an expiry.
* Exit the shorts first, then the wings.
* No rolling, no averaging, no converting the position into something else.

## 6. How much

₹1 crore is the account. It sets the **loss limit**, not the margin to use.

| Item | Initial limit |
|---|---|
| Active collateral / margin pool | ₹20–25 lakh |
| Unpledged execution buffer | ₹10 lakh |
| Capital kept outside this strategy | ₹65–70 lakh |
| Maximum loss per expiry | ₹20,000–₹25,000 |
| Emergency daily limit | ₹30,000 |
| Monthly drawdown pause | ₹75,000 |
| Eventual absolute risk ceiling | ₹50,000 or 0.50 % of the account, whichever is lower |

Lots come from the risk budget, not from margin headroom:

    lots = floor( risk_budget / ((width − credit) × lot_size + cost_reserve) )

Illustratively — NIFTY, 150-point width, 60-point credit, lot 65: the worst case is
(150 − 60) × 65 = ₹5,850 a lot before costs; with ~₹1,000 a lot reserved for slippage, fees and
contingencies, a ₹25,000 budget buys **three lots**. BANKNIFTY at 300/120/30 is ₹5,400 a lot.
These are illustrations; the plan uses the live credit, the live lot size and the live charges.

Expiry-day short index options attract an additional 2 % extreme-loss margin, including on
positions created intraday; hedges do not remove it. The plan asks the broker's basket-margin
estimate before entry and refuses if it exceeds the pool.

## 7. The path to real money

1. Backtest NIFTY and BANKNIFTY **separately**, with real bid/ask where the data allows it,
   full charges, and explicit assumptions for rejected and partial fills. `07-data-reality.md`
   says which of these this repo can actually produce and what each tier may claim.
2. Paper-trade at least **six monthly expiries** on the live chain, through the real desk in
   `DRY_RUN`.
3. Start live with NIFTY only, ₹10,000–₹15,000 maximum risk, one or two lots.
4. Raise toward ₹25,000 only after positive results after every cost with no rule violations.
5. BANKNIFTY only if its independently tested results are better.

## Sources

* NSE NIFTY 50 derivatives contract specifications — https://www.nseindia.com/static/products-services/equity-derivatives-nifty50
* NSE BANKNIFTY derivatives contract specifications — https://www.nseindia.com/static/products-services/equity-derivatives-banknifty
* NSE lot-size circular (FAOP70616) — https://nsearchives.nseindia.com/content/circulars/FAOP70616.pdf
* NSE Clearing expiry-day margin circular (CMPT64639) — https://nsearchives.nseindia.com/content/circulars/CMPT64639.pdf
* SEBI, *Study — Profitability of individual traders in the equity derivatives segment, FY25–FY26* (Aug 2026) — https://www.sebi.gov.in/reports-and-statistics/research/aug-2026/study-profitability-of-individual-traders-in-the-equity-derivatives-segment-fy25-fy26-_103835.html
