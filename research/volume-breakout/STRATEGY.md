# VBT-1 — the "Ankur's Volume Scan" turned into a tradable strategy

*Research note, 10 Sep 2026. Data: Baskfy `ohlcv_daily` on the AWS Phase-A box (3.75 M bars,
4,186 traded instruments, 2017-01-02 → 2026-09-09), exported read-only via
`research/volume-breakout/export_bars_aws.sh` to `s3://baskfy-archive/backtests/volume-breakout/20260910T112857Z/`.
Code and every table quoted here live beside this file. Nothing here has traded real money.*

## 0. The one-paragraph answer

The Chartink scan **as given is not a strategy** — it is a candidate generator with negative
short-term expectancy. Bought at the next open and held 1–20 sessions, its 32,929 signals since
2017 lose money on average (median 20-session return −2.1 %, 42 % of signals positive), because a
6.5 %+ day on 3× volume is, more often than not, a spike that gets sold into. What *does* carry an
edge is the subset of those signals that occur **in an established uptrend, in a liquid name, with
a controlled bar** — and only when **the broad market is healthy**. With those filters, a
**pullback entry** (bid the signal-day close for three sessions instead of chasing the open), a
12 % disaster stop and a **close-below-21-EMA exit**, a 10-slot equal-weight book returns
**18.2 % CAGR (2017-10 → 2026-09) with a −28 % maximum drawdown**, against NIFTY Midcap 150's
15.4 % CAGR at −44 % drawdown over the same window, while being invested only 63 % of the time.
The number is honest about its costs (25 bps a side) and its regime dependence (2018: −19 %,
2025: +1 %), and the rules are simple enough to express in Chartink and in `baskfy_core`.

## 1. What the scan is, and how faithfully it was reproduced

Chartink's five lines, read literally on the closed daily bar:

| # | Rule | Reading used here |
|---|---|---|
| 1 | Volume > SMA(Volume, 50) × 3 | adjusted volume; the SMA includes the signal day |
| 2 | Close > 30 | `close_raw` — an exchange price, not the adjusted series |
| 3 | % Change ≥ 6.5 | close / previous close − 1 |
| 4 | SMA(Volume, 50) ≥ 25,000 | |
| 5 | Volume > 50,000 | |

Universe: NSE cash, `EQ`/`BE`/`BZ` series (SME `SM`/`ST` out), ETFs out (the 350 members of the
`etf` universe plus a narrow name regex). BE names stay in because every trade here is delivery
anyway, and dropping today's BE list would drop the history of names that were *later* demoted —
a survivorship bias in reverse.

**Verification against the Chartink backtest CSV (20 Jan → 10 Sep 2026, 3,407 non-ETF rows):
recall 89.7 %, precision 99.2 %.** The 352 Chartink signals the panel does not produce are almost
all a data-plant gap, not a logic gap: 262 are days on which Baskfy has **no bar** for the
instrument, 81 have too few bars in the 50-session window, 9 fail a rule at the margin. The 26
signals Baskfy produces and Chartink does not are in 20 micro-cap names (ANNAPURNA, AHCL, GNRL,
AEROPLANE …) that Chartink's universe or data evidently treats differently; none is a rule
disagreement on a liquid name.

Two data findings for the plant (recorded here because they change the backtest, not as blockers):
six **thin sessions** (`2017-10-19`, `2018-11-07`, `2024-01-20`, `2024-03-02`, `2024-05-18`,
`2025-02-01` — muhurat / special Saturday sessions where only ~200 names printed) were dropped
from the calendar; a single such column poisons every 50- and 200-session window that spans it,
which is why the 200-DMA filter below "vanished" for most of 2024–25 on the first run. Rolling
statistics tolerate up to 10 % missing bars inside their window, the way a screener that only sees
traded bars computes them. Corporate-action adjustment before 2024 is sparse (403 rows, 2024→);
148 suspicious overnight gaps (>60 % or >+60 %) exist across 66 instruments, and removing every
signal within 60 sessions of one changes no number below by more than 0.03 pt.

## 2. Event study — what happens after the raw signal

Buy at the next session's open, mark at the close *k* sessions later, all 32,882 signals with a
next bar (no costs, no portfolio constraints):

| horizon | mean % | median % | % positive |
|---|---|---|---|
| open gap vs signal close | +0.99 | +0.69 | 69 |
| +1 | −0.61 | −1.06 | 38 |
| +5 | −0.91 | −1.87 | 39 |
| +10 | −0.62 | −2.04 | 41 |
| +20 | −0.03 | −2.07 | 42 |
| +40 | +1.24 | −2.16 | 43 |
| +60 | +2.88 | −1.74 | 44 |
| MAE / MFE over 20 sessions | −11.4 / +13.5 | −10.1 / +9.2 | |

The signal gaps up at the next open (+1 %) and then, on the typical name, gives it all back and
more. The mean only turns positive past 40 sessions and stays below the median-negative line
throughout — the distribution is a long right tail sitting on a mass of small losers. Slicing the
same 20-session return by what the bar looked like (in-sample = signals to 2022-12; out-of-sample
= 2023-01 onward, so a slice that only works in one half is visible as such):

| slice | IS n | IS +20 (open entry) | IS +20 (limit entry) | OOS n | OOS +20 (open) | OOS +20 (limit) |
|---|---|---|---|---|---|---|
| all signals | 17,075 | −0.54 | −0.18 | 15,854 | +0.65 | +0.88 |
| close above 200-DMA | 11,536 | −0.40 | +0.08 | 10,984 | +1.10 | +1.29 |
| close below 200-DMA | 5,539 | −0.84 | −0.71 | 4,870 | −0.39 | −0.06 |
| 20-day avg turnover ≥ ₹2 cr | 11,537 | +0.10 | +0.24 | 13,052 | +1.23 | +1.38 |
| 20-day avg turnover < ₹1 cr | 3,302 | −2.27 | −1.34 | 1,628 | −3.12 | −2.35 |
| day change 6.5–12 % | 11,983 | −0.08 | +0.19 | 11,487 | +0.84 | +1.01 |
| day change > 15 % | 2,897 | −2.40 | −1.55 | 2,398 | −0.67 | −0.09 |
| 20-day ADR > 8 % | 1,655 | −5.13 | −4.08 | 534 | −4.15 | −3.29 |
| relative volume 3–6× | 8,514 | +0.01 | +0.32 | 6,590 | +0.93 | +1.11 |
| relative volume > 10× | 3,540 | −2.17 | −1.70 | 5,000 | −0.11 | +0.17 |
| **trend combo (§3 A–D, F)** | **3,058** | **+0.82** | **+0.93** | **3,991** | **+2.23** | **+2.48** |

Every row that looks good in one half looks good in the other, and every bad one is bad in both.
The pattern is the textbook one: the edge is in **continuation of an existing trend in a liquid
name**, not in the size of the spike. The wildest bars (biggest change, biggest relative volume,
widest ADR, thinnest turnover) are the ones to avoid, not to chase. `out/slices.csv` has the full
table; `out/event_by_*.csv` the raw-scan cuts.

## 3. The strategy — VBT-1

### Universe and signal (evaluated at the close of session *t*)

Chartink's five rules, plus the trend context that carries the edge:

| | Rule | Why (from §2) |
|---|---|---|
| A | Close > SMA(Close, 200) | the trend filter; below the 200-DMA the same bar loses money in both halves of the history |
| B | Close > highest High of the prior 20 sessions | a breakout, not a bounce — the filter the book can least do without |
| C | 20-session return < 25 % | not already extended into the spike |
| D | Close in the top 40 % of the day's range (`(C−L)/(H−L) ≥ 0.6`) | the bar closed strong |
| E | Day change ≤ 15 % | excludes circuit plays and the parabolic prints |
| F | 20-session average turnover ≥ ₹2 crore | the thin tail is where the losses live, and it is the tail a ₹10-lakh book cannot exit |

This cuts 32,929 raw signals to **6,293** (about 13 a week in a normal market). Two more filters
were tested and dropped as redundant once the gate is on — Close > 50-DMA (implied by A + B) and
20-day ADR ≤ 8 % (its damage is already caught by E and F); neither changes the result.

What each filter is worth, removed one at a time from the finished book (`out/final_ablation.csv`):

| removed | signals | CAGR | max DD | profit factor |
|---|---|---|---|---|
| nothing (VBT-1) | 6,293 | **18.2 %** | −27.9 % | 1.55 |
| A  200-DMA | 8,266 | 12.8 % | −32.4 % | 1.32 |
| B  20-day high | 8,544 | **9.0 %** | −37.2 % | 1.24 |
| C  not extended | 10,262 | 12.7 % | −40.0 % | 1.38 |
| D  strong close | 7,070 | 15.4 % | −32.7 % | 1.46 |
| E  change ≤ 15 % | 7,058 | 12.0 % | −32.5 % | 1.36 |
| F  turnover ≥ ₹2 cr | 7,674 | 14.9 % | −34.7 % | 1.48 |
| all six (raw scan) | 32,929 | 0.8 % | −48.1 % | 1.02 |

Chartink form, for whoever runs the scan by hand (append to the existing five lines):
`Daily Close > Daily Sma(Daily Close, 200)`, `Daily Close > 1 day ago Max(20, Daily High)`,
`Daily Close / 20 days ago Close < 1.25`, `(Daily Close − Daily Low) / (Daily High − Daily Low) ≥ 0.6`,
`Daily % Change ≤ 15`, `Daily Sma(Daily Close × Daily Volume, 20) ≥ 20000000`.

### Regime gate

New entries only when **more than 40 % of the tradable universe closed above its own 200-DMA**
on the signal day (`out/breadth200.csv`). Open positions are managed regardless. This is the rule
that turns 2018–19 from a −45 % hole into a −28 % one and keeps the book in cash for most of
2018-06 → 2020-06 and most of 2025. It is a breadth reading of the *universe the book trades*,
which is the same lesson `CLAUDE.md` records for the swing gate: ask the tape you actually trade.
Index-based gates (Midcap 150 above its 50-DMA, 10-DMA over 20-DMA) were tested and **do not
help** here.

### Entry

Do not chase the open. Place a **limit order at the signal-day close**, working for **three
sessions**; it fills when the low trades through the limit (fill assumed at the better of the
open and the limit; requiring the low to trade 0.25 % *through* the limit changes CAGR by 0.02 pt).
91 % of signals trade through their limit within the three sessions. This alone is worth ~9 CAGR
points against a next-open entry (18.2 % vs 9.6 %), because the open gap (+1 %) is exactly the part of the move that reverts.

### Position sizing and book limits

Equal weight, **10 slots, 10 % of equity each** (12.5 % cap), at most **3 new entries a session**,
ranked by signal-day turnover when there are more fills than slots, and never more than **1 % of
the name's 20-day average turnover** (the liquidity cap binds nowhere at ₹10 lakh; it will at
₹1 crore). One position per name. Cash sits idle when the gate is shut.

### Exits, in the order they are checked

1. **Disaster stop: 12 % below the fill**, as a GTT the same session (non-negotiable #4's
   discipline; stops go 10–15 % without changing the result: 16.4–18.2 % CAGR). A gap through
   the stop fills at the open.
2. **Close below the 21-day EMA → sell at the next open.** This is the working exit: 688 of the
   761 trades left this way, 62 hit the stop. A 10-EMA is far too tight (6.3 % CAGR); a 50-SMA
   exit holds longer for the same CAGR at a deeper drawdown.
3. No target, no partials, no time stop — each was tested and each lowers the result, because the
   book's profit is a right tail (average winner +16.5 %, average loser −6.1 %, best trade
   +242 %, the ten best trades are 24 % of gross profit — a fat tail but not a lottery).

## 4. Results

₹10 lakh, 2017-10-16 → 2026-09-09 (the first 200 sessions are warm-up), 25 bps a side for
charges and slippage, every fill on the exchange tick.

| | VBT-1 | NIFTY Midcap 150 | NIFTY 50 | NIFTY Smallcap 250 |
|---|---|---|---|---|
| **CAGR** | **18.2 %** | 15.4 % | 10.1 % | 12.5 % |
| max drawdown | **−27.9 %** (Jan 2018 → Jul 2020) | −44.2 % | −38.4 % | −60.8 % |
| Sharpe (daily, 0 rf) | 0.97 | | | |
| Calmar | 0.65 | 0.35 | | |
| time invested | 63 % (avg 6.3 of 10 slots) | 100 % | 100 % | 100 % |
| trades | 761 (≈ 85 / yr) | | | |
| win rate / profit factor | 37.8 % / 1.55 | | | |
| avg win / avg loss / avg trade | +16.5 % / −6.1 % / +2.5 % | | | |
| avg hold | 18 sessions | | | |
| in-sample CAGR (→ 2022) / drawdown | 12.6 % / −27.9 % | 12.3 % / −44.2 % | | |
| out-of-sample CAGR (2023 →) / drawdown | 26.0 % / −19.8 % | 20.2 % / −21.1 % | | |
| final equity | ₹44.4 lakh | | | |

By calendar year (2017 is Oct–Dec only):

| year | VBT-1 | Midcap 150 | trades | win % | breadth > 200-DMA (avg) |
|---|---|---|---|---|---|
| 2017* | +17.3 | +12.6 | 15 | 33 | 0.20 |
| 2018 | **−19.3** | −13.3 | 52 | 27 | 0.35 |
| 2019 | −3.8 | −0.3 | 6 | 17 | 0.26 |
| 2020 | +20.4 | +24.4 | 87 | 37 | 0.49 |
| 2021 | +45.6 | +46.8 | 135 | 40 | 0.86 |
| 2022 | +16.1 | +3.0 | 104 | 39 | 0.49 |
| 2023 | +64.7 | +43.7 | 97 | 41 | 0.66 |
| 2024 | +35.1 | +23.8 | 131 | 42 | 0.71 |
| 2025 | +1.2 | +5.4 | 88 | 33 | 0.35 |
| 2026 (→ 9 Sep) | +6.3 | +4.6 | 46 | 39 | 0.35 |

Read that table before the headline: **the strategy is a bull-market instrument.** It made its
money in 2020–24, sat mostly in cash through 2018-06 → 2020-06 and most of 2025 (the gate), and
its worst year is the one where the gate was open and the market was not (early 2018). It does not
short and it has no view when breadth is thin; its advantage over holding the index is that it
*leaves*, not that it wins bear markets. Monthly returns are in `out/final_monthly.csv`, the
equity curve in `out/final_equity.png`, every trade in `out/final_trades.csv`.

### What moves the number (`out/final_sensitivity.csv`)

| change | CAGR | max DD | note |
|---|---|---|---|
| **VBT-1 as specified** | **18.2 %** | **−27.9 %** | |
| stop 10 % / 15 % | 16.4 / 16.6 | −26.8 / −29.5 | the EMA exit does the work; the stop is insurance |
| limit valid 2 / 5 sessions | 11.4 / 17.1 | −29.7 / −26.7 | two sessions is too short for the pullback to arrive |
| 8 / 15 slots | 15.5 / 12.9 | −29.6 / −26.2 | ten is the useful number for this signal count |
| breadth gate 30 / 35 / 45 / 50 % | 15.6 / 18.6 / 15.4 / 10.9 | −45 / −33 / −31 / −30 | 35–45 is a plateau; 30 lets 2018 in |
| no gate | 18.5 | **−48.8 %** | same CAGR, twice the drawdown, 85 % invested |
| next-open entry instead of limit | 9.6 | −35.9 | the open gap is the part that reverts |
| 10-EMA exit | 6.3 | −34.8 | whipsawed out of the winners |
| rank fills by day-change / none | 12.1 / 13.6 | −34 | liquidity ranking is worth keeping |
| costs 40 / 60 bps a side | 15.5 / 11.9 | −30 / −32 | at ₹10 lakh in ₹2 cr+ names 25 bps is realistic; scale changes this |
| **raw Chartink scan, same gate, same execution** | **0.8 %** | −48.1 % | the five lines alone, traded well, are flat |

Nothing in the neighbourhood falls apart, and nothing in it was tuned to a decimal. The one
parameter with a cliff is the entry window (two sessions), and the one filter with a cliff is the
20-day-high breakout (B).

## 5. Caveats, stated plainly

**This is one history, and it is a favourable one.** 2020–24 was the best four-year run Indian
small and mid caps have had; the in-sample half (12.6 %) is a better guide to a normal decade
than the out-of-sample half (26 %). The split is not a true walk-forward — the filters were chosen
looking at both halves, then required to hold in each — so treat the OOS number as "did not break",
not as an unbiased forecast. Nine years and one bear market (2018–20) is thin evidence for a
regime gate. Fills are modelled, not experienced: limit fills at the limit, stops at the stop
unless gapped, 25 bps a side. The book is long-only, cash otherwise, no interest on cash (which
would add ~2 pt to CAGR at 37 % idle). Corporate actions before 2024 are as the source adjusted
them. All of it is research code in `research/volume-breakout/`, not `packages/core`; nothing
here touches the desk, the swing book or any flag.

## 6. If it goes into Baskfy

The natural home is a **third sleeve beside the weekly rebalancer and the swing book**, gated the
same way the swing book was (`docs/swing/02` §3: DRY_RUN sessions, backtest on the page, written
risk decision, half-risk first live sessions), and it must go through the same
`plan → confirm → gateway → GTT` path — the non-negotiables already say how, and nothing in this
note asks for an exception. The pure part (signal, breadth, entry/exit rules) is ~150 lines of
DataFrame-in/DataFrame-out logic that belongs in `baskfy_core` under law 1; the limit-order entry
needs one thing the desk does not have today, a **working order that expires after three
sessions**, which the swing book's pending-entry machinery (SW: `pending_cutoff_at`) is the model
for. The breadth series is one aggregate over `factor_daily` (close vs 200-DMA per instrument) —
`market_health_daily` already carries `pct_above_20dma`; this needs the 200-day cousin.

Before any of that: re-run this study once the plant fills the **262 missing instrument-days**
(§1) and the pre-2024 corporate actions, since those are the two places a number here could move.

## 7. Files

| file | what |
|---|---|
| `STRATEGY.md` | this note |
| `export_bars.sh`, `export_bars_aws.sh` | read-only exports (local Docker; AWS box via SSM → S3 → `aws/`) |
| `vbt/data.py`, `vbt/scan.py`, `vbt/sim.py` | panel loader (thin-session drop, ETF/series rules), the scan + indicators, the EOD simulator |
| `run_research.py` | reproduction check vs the Chartink CSV, event study, first rule grid |
| `explore.py` | the conditional event study (`out/slices.csv`, `out/signal_features.pkl`) |
| `grid2.py`, `grid3.py`, `robust.py`, `robust2.py` | exit / entry / slot / gate grids and ablations |
| `final.py` | VBT-1, its sensitivity table, yearly/monthly tables, benchmarks |
| `out/final_*.{csv,json,png}` | trades, equity, yearly, monthly, metrics, sensitivity, ablation, chart |
| `out/only_chartink.csv`, `out/only_mine.csv` | the reproduction diff |
| `aws/*.csv.gz` | the exported tables (not committed to git; regenerate with the script) |

Run order from `research/volume-breakout/` with `polars pandas numpy matplotlib` installed:
`python run_research.py aws` (builds `data/panel.pkl`), `python explore.py`, `python grid2.py`
(builds `data/sig2.pkl`), `python robust2.py` (builds `out/breadth200.csv`), `python final.py`.
