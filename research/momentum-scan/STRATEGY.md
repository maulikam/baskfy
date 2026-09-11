# MOM-1 — the "MOMENTUM STOCKS" scan turned into a tradable strategy

*Research note, 11 Sep 2026. Same data as `research/volume-breakout/` (Baskfy `ohlcv_daily` from
the AWS box, 3.75 M bars, 4,186 traded instruments, 2017-01-02 → 2026-09-09, exported
10 Sep 2026); same simulator (`../volume-breakout/vbt/`), extended with a state exit, a rank
override and a drawdown lock-out for this study. Nothing here has traded real money.*

## 0. The one-paragraph answer

This scan is a **state**, not an event: it is true on every session a stock closes 20 % above the
low of five sessions earlier, or 30 % above the low of 30 or 90 sessions earlier (with a ₹30 floor
and a 50,000-share average volume). Some 200–400 names satisfy it on a normal day and a name stays
in it for weeks, so the tradable moment is **the day a stock enters the list**. Raw entries have a
small positive drift (about +1 % over 20 sessions, +4–5 % over 60) but, traded as they come, lose
money (1.6 % CAGR, −73 % drawdown): most are bounces off lows in downtrends and thin names.
Filtered to **liquid, calm, not-yet-extended names in an uptrend whose trigger is the 30/90-day
rule** (seven filters, §3), bought at the next open and held to the **first close below the
50-day SMA**, a 10-slot book returns **14.6 % CAGR with a −46 % maximum drawdown**
(2017-10 → 2026-09). NIFTY Midcap 150 did 15.4 % at −44 % over the same window. **Made coherent,
this scan is the index with extra steps** — a fully invested trend-follower that loses more than
the index in a bear market and makes it back afterwards. A breadth gate (**MOM-1g**) brings the
drawdown to −31 % at 13.9 % CAGR, the best risk-adjusted form of it (Calmar 0.44 vs the index's
0.35), and it pairs well with 10 Sep's VBT-1 (18.2 % at −28 %): 14 shared trades in 1,412, monthly
correlation 0.4–0.5, and a 50/50 blend of VBT-1 and MOM-1g does 16.5 % at −26 %. The honest use of
this scan is as a **candidate feed and a second, gated sleeve** — not as the main book.

## 1. The scan, and how faithfully it was reproduced

| # | Rule | Reading used here |
|---|---|---|
| 1 | Close > 30 | `close_raw` (an exchange price) |
| 2 | SMA(Volume, 50) ≥ 50,000 | adjusted volume; the SMA includes the day |
| 3 | any of: Close ≥ 1.2 × Low[t−5] · Close ≥ 1.3 × Low[t−30] · Close ≥ 1.3 × Low[t−90] | "5 days ago Low" is the low of the single bar five sessions earlier, not a 5-day minimum |

Universe and data handling exactly as in `../volume-breakout/STRATEGY.md` §1 (EQ/BE/BZ series,
ETFs out, the six thin special sessions dropped, 10 % missing-bar tolerance in rolling windows).

**Verification against the Chartink backtest CSV (21 Jan → 9 Sep 2026, 45,269 non-ETF
stock-days): recall 83.9 %, precision 99.2 %.** Of the 7,293 Chartink stock-days the panel does
not produce, 3,992 are days Baskfy has **no bar** for the instrument (the plant gap noted on
10 Sep, five times larger here because a state scan has ten times the membership), 2,624 are
names without a clean 50-session volume window, and 677 (1.5 %) fail a rule at the margin —
mostly recently listed names where Chartink evidently evaluates the 90-day rule on fewer than
90 bars.

The scan carries **214 names a day on average** since 2017 (54 in 2019, 395 in 2021). A stay is
short-tailed: median 4 sessions, mean 12. So an "entry" is **the first session in the scan after
at least five sessions out of it** — 27,053 such events since 2017, 8,915 after the filters below.

## 2. Event study — what an entry is worth

Buy at the next open, mark at the close *k* sessions later; in-sample = entries to 2022-12,
out-of-sample = 2023 onward (`out/entry_slices.csv`, computed on entries after ≥ 10 sessions out):

| slice | IS n | IS +20 | IS +60 | OOS n | OOS +20 | OOS +60 |
|---|---|---|---|---|---|---|
| all entries | 10,834 | +1.07 | +4.13 | 10,344 | +1.60 | +5.50 |
| trigger: 5-day rule only (a 20 % pop) | 3,159 | −0.31 | +3.82 | 3,074 | +2.36 | +6.14 |
| trigger: 30/90-day rule only | 6,294 | +2.22 | +4.78 | 6,218 | +1.40 | +5.49 |
| 20-day avg turnover < ₹1 cr | 1,876 | −0.01 | +3.71 | 880 | −1.43 | +0.98 |
| 20-day avg turnover ≥ ₹2 cr | 7,623 | +1.40 | +4.29 | 8,789 | +1.86 | +5.92 |
| 5-day return ≥ 25 % at entry | 1,029 | −2.31 | +1.16 | 767 | +0.32 | +1.25 |
| 20-day return ≥ 50 % at entry | 265 | −3.11 | −1.16 | 119 | −2.42 | +5.66 |
| day change > 10 % | 1,873 | −1.28 | +1.51 | 1,617 | +0.28 | +2.38 |
| 20-day ADR > 8 % | 849 | +0.59 | +10.83 | 211 | −3.48 | −3.18 |
| close above 200-DMA | 6,707 | +1.08 | +3.62 | 7,076 | +1.87 | +6.23 |
| within 10 % of the 52-week high | 3,203 | +1.24 | +4.41 | 3,863 | +2.63 | +7.65 |

Unlike the volume scan, the raw entry here has a positive mean at every horizon — it is a
continuation signal, not a spike — and the money is in the *longer* horizon (+4–5 % at 60
sessions), which is why the working exit ends up being a slow one. The same bad neighbourhoods
recur: thin names, parabolic bars, entries already 25–50 % up on the month. The "5-day rule only"
trigger — a 20 % pop off a low in five sessions — is the one that carries the downtrend bounces.

## 3. The strategy — MOM-1

### Signal (at the close of session *t*)

The stock is in the scan today and was **out of it for the previous five sessions**, and at that
close:

| | Rule | Why |
|---|---|---|
| A | the 30-day or 90-day rule fired (not the 5-day rule alone) | the 5-day-only trigger is the bounce, not the trend |
| B | Close > SMA(Close, 200) | trend context |
| C | 20-session average turnover ≥ ₹2 crore | the thin tail is where the losses live |
| D | 20-session average daily range ≤ 6 % | calm names — the filter the book can least do without (8.7 % CAGR without it) |
| E | 5-session return < 15 % | not entering on a pop |
| F | 20-session return < 30 % | not already extended (10.4 % without it) |
| G | Close > SMA(Close, 50) | **coherence with the exit**: without it, 974 entries sit below the 50-SMA on the day they are bought and 122 trades are sold the next morning by the exit rule at −2.6 % each |

8,915 entries since 2017, ≈ 17 a week. Filter G is worth reading twice: *without* it the
backtest scores 18.1 % CAGR — better — because the churn trades happen to free slots for names
that did well. A rule that buys what it will sell tomorrow is not a rule, so G stays and 14.6 %
is the number; the 3.5-point gap is a measure of how much of any figure here is slot-allocation
luck rather than signal (§5).

Chartink form (append to the existing scan): `Daily Close > Daily Sma(Daily Close, 200)`,
`Daily Close > Daily Sma(Daily Close, 50)`, `Daily Sma(Daily Close × Daily Volume, 20) ≥ 20000000`,
`Daily Sma(Daily High / Daily Low − 1, 20) ≤ 0.06`, `Daily Close / 5 days ago Close < 1.15`,
`Daily Close / 20 days ago Close < 1.30`, with the 30/90 sub-rules on their own line so the
5-day-only case can be excluded. "First day back after five out" is a state Chartink cannot
express — run yesterday's scan too and take the difference, or let Baskfy do it (§6).

### Entry, sizing, exits

**Next session's open, market order.** For this signal the pullback-limit entry that made VBT-1
work *hurts* (8.1 % vs 14.6 %): a continuation entry that waits for a dip misses the names that
do not dip, which are the ones that pay. Equal weight, **10 slots, 10 % each (12.5 % cap), at
most 3 new entries a session**, ranked by 20-day turnover when there are more signals than slots,
never more than 1 % of a name's 20-day turnover, one position per name.

1. **Disaster stop 15 % below the fill**, as a GTT the same session. It fires on 7 % of trades;
   10–20 % all give 13–16 % CAGR — insurance, not the exit.
2. **First close below the 50-day SMA → sell at the next open.** 596 of 651 exits. The 21-EMA
   (−0.8 % CAGR) and the 20-SMA (−2.0 %) are far too tight for this signal. Dropping out of the
   scan itself is a bad exit (membership flickers). A 20 % trailing stop scores higher here
   (16.7 %, −32 %) but on 212 trades held 99 sessions — a different, slower strategy that this
   note does not have the trade count to vouch for.
3. No target, no partials, no time stop. The book's profit is a right tail: average winner
   +20.7 %, average loser −6.9 %, best trade +667 % (TANLA, Jul 2020 → Jan 2021, 137 sessions),
   the ten best trades 31 % of gross profit.

### Regime — MOM-1g

New entries only when **more than 35 % of the tradable universe closed above its own 200-DMA**
(`out/breadth200.csv`; open positions are managed regardless). This is what turns the index-like
drawdown into a tolerable one: −46 % → −31 %, CAGR 14.6 % → 13.9 %, Sharpe 0.73 → 0.76, invested
91 % → 71 % of the time. 35 % rather than 10 Sep's 40 % because this signal needs a lower bar to
get back in after a bear market (40 % gives 11.4 %, 45 % gives 5.8 %); it is a plateau, not a
point. A sleeve-equity lock-out (stop entering after a 15 % fall from the equity peak, resume
after a cooldown, in the SW9.5 spirit) was tested and **is not adopted**: neighbouring settings
swing from 10 % to 18 % CAGR (`out/lockout.log`), the signature of a rule that fits history.

## 4. Results

₹10 lakh, 2017-10-16 → 2026-09-09, 25 bps a side, fills on the exchange tick.

| | MOM-1 | **MOM-1g** (breadth > 35 %) | VBT-1 (10 Sep) | Midcap 150 | NIFTY 50 |
|---|---|---|---|---|---|
| **CAGR** | 14.6 % | **13.9 %** | 18.2 % | 15.4 % | 10.1 % |
| max drawdown | −46.3 % (Jan 2018 → May 2020) | **−31.4 %** | −27.9 % | −44.2 % | −38.4 % |
| Calmar | 0.32 | **0.44** | 0.65 | 0.35 | 0.26 |
| Sharpe | 0.73 | 0.76 | 0.97 | | |
| time invested | 91 % | 71 % | 63 % | 100 % | 100 % |
| trades · win rate · profit factor | 651 · 35 % · 1.50 | 509 · 33 % · 1.56 | 761 · 38 % · 1.55 | | |
| avg hold | 31 sessions | 31 | 18 | | |
| IS CAGR (→ 2022) / OOS (2023 →) | 11.9 % / 18.2 % | 9.5 % / 19.8 % | 12.6 % / 26.0 % | 12.3 % / 20.2 % | |
| final equity | ₹33.6 lakh | ₹31.8 lakh | ₹44.4 lakh | | |

| year | MOM-1 | MOM-1g | Midcap 150 | MOM-1 trades | avg names in scan |
|---|---|---|---|---|---|
| 2017 (Oct–Dec) | +11.2 | +11.2 | +12.6 | 13 | 134 |
| 2018 | **−26.4** | −8.2 | −13.3 | 81 | 86 |
| 2019 | **−14.6** | −15.7 | −0.3 | 78 | 54 |
| 2020 | +77.3 | +54.0 | +24.4 | 58 | 243 |
| 2021 | +38.1 | +38.7 | +46.8 | 72 | 395 |
| 2022 | +5.1 | −12.6 | +3.0 | 84 | 203 |
| 2023 | +34.9 | +40.7 | +43.7 | 63 | 308 |
| 2024 | +20.9 | +22.1 | +23.8 | 73 | 338 |
| 2025 | +11.5 | +12.6 | +5.4 | 66 | 153 |
| 2026 (→ 9 Sep) | +2.9 | +2.2 | +4.6 | 63 | 230 |

Two losing years in a row at the start, both worse than the index: a fully invested breakout
book in a small-cap bear keeps buying the names that get back above their averages for a week.
The gate halves 2018 and costs 2022. Monthly returns are in `out/final_monthly.csv`, the equity
curves in `out/final_equity.png`, every trade in `out/final_trades.csv` and
`out/final_trades_gated.csv`.

### Pairing with VBT-1

| | VBT-1 | MOM-1 | MOM-1g | 50/50 VBT-1 + MOM-1g, rebalanced monthly |
|---|---|---|---|---|
| CAGR (monthly marks) | 18.1 % | 14.3 % | 13.6 % | **16.5 %** |
| max drawdown (monthly marks) | −24.9 % | −42.5 % | −28.4 % | **−26.4 %** |
| monthly-return correlation with VBT-1 | | 0.39 | 0.48 | |
| identical trades (symbol + entry day) | | 14 of 1,412 | | |

Same pond, different nets: VBT-1 wants a volume spike bought on the pullback and exits in three
weeks; MOM-1 wants a quiet re-entry into a trend bought at the open and held six. The blend is not
better than VBT-1 alone on these numbers — it is a diversification of *which* history you are
betting on, and that is the reason to run it, if any. If both run they are one risk budget.

### What moves the number (`out/final_sensitivity.csv`, `out/final_ablation.csv`)

| change | CAGR | max DD | note |
|---|---|---|---|
| **MOM-1 as specified** | **14.6 %** | **−46.3 %** | |
| stop 10 / 12 / 20 % | 15.6 / 13.4 / 16.1 | −42 / −43 / −46 | flat |
| 8 / 15 slots | 16.2 / 10.9 | −47 / −44 | concentration pays, as on 10 Sep |
| rank by relative volume / none | 16.0 / 16.4 | −54 / −50 | ranking is not where the edge is; turnover keeps the drawdown lowest |
| costs 40 / 60 bps a side | 12.3 / 9.4 | −49 / −53 | |
| entry after ≥ 3 / ≥ 10 sessions out | 10.9 / 17.5 | −49 / −39 | ten is as good as five, with fewer trades |
| limit-at-close entry | 8.1 | −42 | see §3 |
| 21-EMA / 20-SMA / 20 % trail exit | −0.8 / −2.0 / 16.7 | −45 / −46 / −32 | the slow exit is the strategy |
| breadth gate 35 / 40 / 45 % | 13.9 / 11.4 / 5.8 | −31 / −32 / −31 | |
| drop filter A / B / C / D / E / F / G | 15.5 / 16.1 / 13.0 / **8.7** / 15.9 / **10.4** / 18.1 | −46 / −42 / −50 / −49 / −38 / −49 / −47 | see §5 |
| keep only C + D + F | 9.4 | −48 | the filters work as a set |
| raw entries, no filters | 1.6 | −73 | |

## 5. Caveats, stated plainly

**The number is inside the noise of slot allocation.** Removing a filter that must be there (G)
*raises* the CAGR by 3.5 points; removing the trigger rule or the 200-DMA raises it by 1–2;
removing them all together collapses it. The execution parameters (stop, slots, entry lookback)
are flat; *which names get the ten slots* is not, and nothing here can tell 14.6 % from 18 %
apart from luck. Read every figure in this note as "mid-teens with the index's drawdown".

**It is index beta with a trend filter.** 91 % invested, 0.73 Sharpe, a −46 % drawdown that
began the same month as the index's and went deeper. The in-sample 11.9 % is the better guide
than the out-of-sample 18 %. Everything in `../volume-breakout/STRATEGY.md` §5 applies verbatim:
modelled fills, 25 bps, no interest on idle cash, sparse corporate actions before 2024, one
history, research code rather than `packages/core`.

**The reproduction is weaker than 10 Sep's (84 % recall).** The plant's missing instrument-days
hit a state scan five times harder than an event scan; until they are filled, the live scan will
show fewer names than Chartink on some days, and this backtest ran on the names it had.

## 6. If it goes into Baskfy

Not as a sleeve on its own. As a **candidate feed** (the daily entry list with the seven filters
and the breadth reading is the useful artefact — it is also the list a discretionary trader would
want on the desk), and, only if VBT-1 is built first, as a second event type sharing VBT-1's
breadth series, sizing, gateway path and cash. Three mechanical differences from VBT-1: the entry
is a **next-open market order** (no working limit), the exit is a **close below the 50-SMA**
(already a `factor_daily` column), and the signal needs **yesterday's scan membership** to detect
an entry — one boolean per instrument-day, five sessions deep.

## 7. Files

| file | what |
|---|---|
| `STRATEGY.md` | this note |
| `mscan.py`, `mscan_verify.py` | the state scan and entry detector; the reproduction check (builds `data/state.pkl`) |
| `explore_ms.py` | entry event study (`out/entry_slices.csv`, `out/entry_features.pkl`) |
| `grid_ms.py`, `robust_ms.py` | entry / exit / gate grid; sensitivity and ablation around the base |
| `final_ms.py`, `lockout_test.py` | MOM-1 and MOM-1g, sensitivity, yearly / monthly tables, benchmarks; the lock-out test |
| `out/final_*.{csv,json,png}`, `out/*_gated.csv`, `out/lockout.log` | trades, equity, yearly, monthly, metrics, sensitivity, ablation, chart |
| `chartink_backtest.csv`, `chartink_today.csv` | Chartink's export of the scan's history and of 11 Sep's 333 names |
| `data/index_series.csv`, `out/breadth200.csv` | benchmarks and the 200-DMA breadth series (copied from `../volume-breakout/`) |

Run order, from this folder, with `../volume-breakout/` present and its `data/panel.pkl` built
(`python ../volume-breakout/run_research.py ../volume-breakout/aws`): `python mscan_verify.py`,
`python explore_ms.py`, `python grid_ms.py`, `python robust_ms.py`, `python final_ms.py`,
`python lockout_test.py`. `vbt/` is a symlink to `../volume-breakout/vbt`.
