# RESEARCH — which overnight F&O edges survive 2022–2026 end-of-day data

**Run 23 Sep 2026, Tier 2E (`07` §4).** It covers every NSE trading day from 3 Jan 2022 to 22 Sep
2026: 1,162 F&O bhavcopy files read through `NSEProvider.fo_bhavcopy`, 67 exchange holidays (404),
and no other misses. The scripts and raw outputs are in `evidence/research/` and `evidence/`.
**This file decides which sleeves `01` builds.** It keeps every negative result, because the
negative results are most of the value.

## The verdict first

| Family | Best variant tested | Net expectancy (R/trade) | Robust across years? | Verdict |
|---|---|---|---|---|
| **B4 Index monthly iron condor, entered 15 sessions before expiry** (NIFTY + BANKNIFTY) | short ±1σ, wings +0.5σ, 50 % profit take, exit before expiry | **+0.033** (t = 1.36, n = 100, 86 % win, max DD −1.64R) | ≥ 0 in 2022, 2023, 2024; ~0 in 2025 and 2026 | **The one paper candidate** (F1), with the caveats in §B4 |
| A Stock-futures trend (20-day breakout with trend + NIFTY regime) | 3 ATR chandelier, long only | +0.033 (t = 1.35, n = 2,334) | **No.** +0.47 in 2023, negative in 2022, 2024, 2025 and 2026 | Not built. One year carries it |
| A Stock-futures trend, both sides | as above | −0.023 | shorts lose in 4 of 5 years | Not built |
| A Open-interest "buildup" filters | OI rising with the breakout; 1-day long/short buildup | −0.05 to −0.01 | — | **OI adds nothing**; the control without OI does better |
| A Cross-sectional momentum, futures long/short | top/bottom 10 weekly | −0.004 | — | Not built |
| A Index-futures trend | NIFTY, BANKNIFTY, MIDCPNIFTY | −0.03 to −0.06 | — | Not built |
| B1 Stock iron condors (top 30 by liquidity) | N = 10, ±1σ | −0.021 at 1.5 % slip, −0.032 at 3 % | negative every year | **Rejected: no gross edge** (+0.005 to +0.027R before costs) |
| B2 Stock credit spreads on the trend's side | N = 15, 0.5σ | −0.024 to −0.042 | — | Rejected (gross +0.006 to +0.008) |
| B3 Exit at E−4 instead of E−1 (stock condors) | — | −0.042 to −0.055 | — | Worse. The delivery-margin ramp is avoided at a cost in edge. Moot while B1 is rejected |
| C1 Debit spreads on the breakout signal | ATM / +0.5σ, 10 sessions | **−0.236** (t = −20.8) | — | Rejected |
| C2 Long ATM option on the breakout signal | 10 sessions | −0.105 | — | Rejected |
| E Cash-futures carry | front month, ≥ 5 days to expiry | median basis 5.6 % a.y. **before** ~0.45 %/cycle costs; 0.7 % of stock-days clear 7 % net | — | Rejected: below the cost of the capital |

**In plain words:** on four and three-quarter years of real closes, *selling stock-option premium,
buying stock options, trading stock futures on trend, and carry* all fail to beat their costs. The
one family with a defensible positive number is the slowest and cheapest to trade: a hedged,
monthly index condor. Even that number is small, from 100 trades, and the best of three entry
days, so it is a **hypothesis for paper**, not an edge.

## Method (applies to every family)

* **No look-ahead.** Signals use the close of day *t*. Futures enter at the **open of *t*+1**;
  options (whose opens are unreliable) at the **close of *t*+1**. Exits are at later closes, or
  at a stop inside the day's range.
* **The continuous futures series** holds, into each session, the nearest contract expiring
  *after* the previous session. Levels are built from that contract's own returns, so a roll is
  never a price jump (`evidence/research/cont.py`).
* **Corporate actions.** A session where the held future moves more than 30 % (a split or bonus)
  is dropped from the futures series. An option cycle containing one is excluded and counted
  (JUBLFOOD 1:5, Apr 2022 was the first found).
* **Option marks.** A leg is priced at its bhavcopy close only if it traded that day. Otherwise it
  is Black-76-modelled at the entry IV, and the number of modelled legs is reported (0.00–0.12 per
  exit across runs). Each vertical is clamped to `[0, width]`, because closes are not simultaneous
  and raw marks breach the no-arbitrage bound.
* **Costs, current regime applied to every year** (conservative for 2022–2025): STT 0.15 % of
  premium on option sales and 0.05 % on futures sales (Finance Act 2026, from 1 Apr 2026;
  `docs/options/DECISIONS-OP` OP0.1); exchange 0.0355 % of premium; ₹20 per order; GST 18 %.
  **Slippage per leg per crossing is `max(₹0.05, x % of premium)`**, with x = 3 % for stock
  options (1.5 % as a sensitivity), 0.5 % for NIFTY/BANKNIFTY options, and 0.03 % for futures.
* **R** = P&L ÷ the planned maximum loss: the structure's max loss for options, and the stop
  distance for futures.

## A — Stock and index futures (`res_futures.py`, `evidence/fut_full.txt`)

Universe: the top 60 % of F&O stocks by 20-session futures turnover, each day.

| Variant | n | exp R | t | win | Notes |
|---|---|---|---|---|---|
| A1 20-day breakout + trend + OI rising, 10 sessions, 2 ATR stop | 6,335 | −0.051 | −4.1 | 46 % | |
| A1r same, aligned with NIFTY's 50-day regime | 4,848 | −0.062 | −4.3 | 45 % | |
| A0 control: aligned, **no OI filter** | 6,841 | −0.047 | −3.9 | 45 % | better than with OI |
| A0 20 sessions, 3 ATR | 4,902 | −0.015 | −1.1 | 47 % | |
| A0 3 ATR chandelier, ≤ 40 sessions | 4,124 | −0.023 | −1.4 | 38 % | longs +0.040, shorts −0.099 |
| **A0 chandelier, long only** | 2,334 | **+0.033** | 1.35 | 39 % | by year: −0.013, **+0.474**, −0.116, −0.125, −0.126 |
| A3 one-day buildup (±2 % with OI +5 %), 5 sessions | 3,424 | −0.014 | −1.1 | 48 % | |
| A2 weekly XS momentum, top/bottom 10 | 2,705 | −0.004 | −0.4 | 51 % | |
| A4 index trend, chandelier | 119 | −0.033 | −0.3 | 37 % | |

**F2, the long-only chandelier, re-costed with its rolls** (`evidence/research/res_f2.py`). A
live future cannot be held through expiry, and a position held about 22 sessions rolls 0.96 times
on average, each roll another 0.12 % round trip: n = 2,334, **+0.017R** (t = 0.71). By year:
−0.026, +0.455, −0.129, −0.143, −0.141. Maulik chose to build it on paper anyway (M.1).

**Reading.** The 2022–mid-2023 slice, run first while the fetch was still going, showed +0.06 to
+0.09R. The full sample removes it. The long side's whole result is the 2023 mid/small-cap rally.
**This is the swing book's thesis in another wrapper.** A futures version adds leverage and a
short side, and the short side loses. Nothing here justifies a futures sleeve.

## B — Selling option premium (`res_options.py`)

**B1 Stock iron condor**, top 30 names by futures turnover, short strikes at ±kσ√T, wings a
further wσ, 50 % profit take checked at each close, exit at the close of E−x:

| N (sessions before expiry) | k | w | exit | slip | n | net R | gross R | cost R | win |
|---|---|---|---|---|---|---|---|---|---|
| 10 | 1.0 | 0.5 | E−1 | 3 % | 1,541 | −0.032 | +0.027 | 0.059 | 80 % |
| 10 | 1.0 | 0.5 | E−1 | 1.5 % | 1,541 | −0.021 | +0.027 | 0.048 | 80 % |
| 15 | 1.0 | 0.5 | E−1 | 3 % | 1,468 | −0.051 | +0.005 | 0.056 | 81 % |
| 15 | 1.0 | 0.5 | E−1 | 0 %* | 1,468 | −0.030 | +0.005 | 0.035 | 81 % |
| 15 | 1.0 | 0.5 | **E−4** | 3 % | 1,468 | −0.055 | +0.001 | 0.057 | 72 % |
| 20 | 0.75 | 0.75 | E−1 | 1.5 % | 1,133 | −0.042 | −0.008 | 0.034 | 78 % |

\* The ₹0.05 minimum per leg per crossing still applies at 0 %: that is the tick.

It is negative in every year (N=15, 0 % slip: −0.008, −0.051, −0.011, −0.025, −0.069). Bucketing by
IV ÷ RV20 at entry does not rescue it (N=10, 3 %: −0.036 / −0.032 / −0.025 for the buckets
IV < RV, 1–1.3 and above 1.3). **Stock options in this sample carry no variance premium worth selling at retail costs.** The 80 % win
rate is the classic trap: many small wins and a few max losses.

**B2 Trend-side credit spreads** (put spread when the stock and NIFTY are above their 50-day
averages, call spread when below; N=15, 0.5σ short, 0.5σ wing): top 60 at 3 % −0.042 (gross
+0.006); top 30 at 1.5 % −0.024 (gross +0.008). Rejected.

**B4 Index monthly iron condor** (NIFTY and BANKNIFTY monthlies, same structure, 0.5 % slip):

| N | n | net R | t | gross R | win | worst |
|---|---|---|---|---|---|---|
| 20 | 85 | −0.020 | −0.6 | −0.005 | 81 % | −1.05 |
| **15** | **100** | **+0.033** | **1.36** | +0.048 | 86 % | −1.02 |
| 10 | 108 | −0.052 | −1.6 | −0.034 | 77 % | −1.03 |

N=15 by year: +0.054, +0.011, +0.093, −0.000, −0.014 (2026 is 12 trades). By underlying: BANKNIFTY
+0.068 (49 trades), NIFTY 0.000 (51). By IV ÷ RV20: < 1: +0.006 (36), 1–1.3: +0.003 (38), **> 1.3:
+0.117 (24 trades, 24 wins)**. Sequential max drawdown −1.64R.

**Why it is only a paper candidate:**

1. N=15 was chosen after seeing N=10 and N=20. Neighbouring choices are negative, so the result
   is not stable in the parameter. That is the mark of noise as often as of edge.
2. t = 1.36 on 100 trades is not significant.
3. The IV ÷ RV > 1.3 slice is 24 trades, found in-sample. It is a *hypothesis* to be tested
   forward, not a filter to be trusted.
4. The years are thinning: ~0 in 2025 and 2026. SEBI's F&O measures of Nov 2024 (larger lots,
   one weekly index per exchange) changed the index options market partway through the sample.

**A defensive exit was tested too** (close the whole structure at the next close after the
underlying closes beyond a short strike): N=15 falls to **+0.018R** (t = 0.84; worst −0.95R); N=20
−0.008; N=10 −0.045. It gives up edge and buys almost no tail protection, so F1 does not use it.

**Maulik's loss close** (M.1: close the structure when the loss reaches 1.5 × the credit),
tested after his answer (`evidence/idx_stop15.txt`): N=15 gives **+0.022R** (t = 1.01), win 83 %,
worst −0.73R, max drawdown −1.35R. By year: −0.010, −0.004, +0.078, +0.010, +0.047. At 1.0×:
+0.025R, worst −0.66R; at 2.0×: +0.023R, worst −0.95R. It costs about a third of the edge and cuts
the tail by about a quarter.

What makes it worth paper, and nothing else: it is hedged at every instant, it trades once a month
per index, its costs are a third of the stock versions' (0.015R), its worst trade is bounded at
about −1R, and it is the only family whose *gross* edge is clearly above zero.

## C — Buying options on a directional signal (`res_debit.py`)

On the A0 signal (aligned 20-day breakout; the top 60 % by futures turnover, intersected with the
~60 most liquid option underlyings), entry at the close of *t*+1, 10
sessions:

| Structure | n | net R | win | The future on the same trades |
|---|---|---|---|---|
| Debit spread: ATM long, short at +0.5σ | 3,874 | **−0.236** (t = −20.8) | 36 % | −0.33 % per trade |
| Long ATM option | 3,876 | −0.105 | 32 % | −0.32 % |

The underlying's move is roughly zero on these signals, so the options pay theta and spread for
nothing. Rejected. It would only be reconsidered on a signal that first shows a positive
*underlying* move after costs, which no signal here does.

## E — Cash-futures basis (`res_basis.py`, UDiFF days only: 8 Jul 2024 → 22 Sep 2026)

Front-month annualised basis across stock-days: 10th pct −5.1 %, quartiles 1.8 % / **5.6 %** /
8.8 %, 90th 11.7 %. Monthly median across the top 50 by turnover ran 4.0–7.6 %. After ~0.45 % per
cycle (delivery STT both sides, futures STT, charges, four fills' slippage), **0.7 %** of stock-days
offer more than 7 % annualised to expiry. That is less than a liquid fund, before counting the
capital locked in delivery. Rejected.

## D — Hedging the equity books (not tested)

Your holdings (`holdings-status/holdings.csv`, 20 names) contain **no F&O stock**. Covered calls
are therefore impossible, and any hedge would be an index proxy. It could not be tested here: the
bhavcopy has no small-cap index series, and hedging crosses the wall between books (`02` Track C
§7). It is QUESTIONS Q5, not a sleeve.

## What would change these verdicts

* **Measured slippage.** FO3's live collector records real bid-ask spreads on the stock options
  that the scans would have traded. If the real per-leg cost of the top 30 names is below the ₹0.05
  tick plus 1 %, B1 is re-run; its gross edge (+0.027R at N=10) is still too thin to survive
  anything more.
* **Another year of data.** Every rejection above is re-run automatically by FO9 each quarter on
  the growing `fo_contract_daily`, and reported on the page. A family that turns positive in
  three consecutive quarterly re-runs becomes a `DECISIONS-FO` entry for Maulik, never a silent
  switch-on.
