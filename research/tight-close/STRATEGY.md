# TWT-1 — the "3 Week Tight Close" scan turned into a tradable strategy

*Research note, 11 Sep 2026. Same data and simulator as `../volume-breakout/` (Baskfy
`ohlcv_daily` from the AWS box, 3.75 M bars, 4,186 traded instruments, 2017-01-02 → 2026-09-09);
the simulator gained a buy-stop-at-level entry and a level stop for this study. Nothing here has
traded real money.*

## 0. The one-paragraph answer

The scan finds a stock that has run 30 % above its low of three months ago and whose **last three
weekly closes sit within 3 % of each other** — O'Neil's "three weeks tight" in Chartink form,
with a ₹30 floor and a 10,000-share volume floor. It is a state (≈ 50 names a day, held for a
median of five sessions), so the event is **the first day the closes are tight**. This is the best
raw material of the three scans: an entry, bought at the next open with no filter at all, is worth
**+2.3 % after 20 sessions and +7.3 % after 60**, in both halves of the history, and no context
filter improves on it — the pattern *is* the filter. What the entry needs is **room**: a 10 % stop
turns it into a loser (the names are volatile, base depth is typically over 12 %, and the median
trade is a stop-out), a 20 % stop and a **20 % trailing stop** turn it into a position-trading
book — 10 slots, ~18 trades a year, held 100 sessions on average, **20.9 % CAGR with a −25 %
maximum drawdown**, Sharpe 1.2, profit factor 2.7 — with a 40 % breadth gate keeping it out of
2018-style markets. The textbook breakout entry (buy-stop above the base high) is **worse** here,
as it was for MOM-1. The price of those numbers is **164 trades in nine years, ten of which are
half the profit** (§5); the higher-frequency form (exit on a close below the 50-SMA: 543 trades,
15.6 % at −38 %) is the same edge with the index's drawdown. Three sleeves now, and they are
different: zero shared trades with VBT-1, monthly correlation 0.5, and an equal-weight blend of
VBT-1, MOM-1g and TWT-1 does 18.4 % at a −22 % monthly-marked drawdown.

**A finding about Chartink itself (§1):** its *backtest* of this scan uses the full week's close
on every day of the week — it knows Friday's close on Monday. The signals in the backtest CSV are
therefore not the signals a live run of the same scan produces (65 % overlap), and any Chartink
backtest built on weekly or monthly candles should be read with that in mind.

## 1. The scan, and how faithfully it was reproduced

| # | Rule | Reading used here |
|---|---|---|
| 1 | Close > 30 | `close_raw` |
| 2 | Close ≥ 1.3 × "3 months ago Low" | the low of the calendar month three months before the current one (2 and 4 months, and a rolling 63-session low, all score worse) |
| 3 | \|Max(3 weekly closes) / Min(3 weekly closes) − 1\| × 100 ≤ 3.01 | the current week's close so far (= today's close) and the closes of the two previous ISO weeks |
| 4 | Market cap > 1 | a no-op |
| 5 | SMA(Volume, 50) ≥ 10,000 | |

Universe and data handling as in `../volume-breakout/STRATEGY.md` §1.

**Verification against the Chartink backtest CSV (21 Jan → 9 Sep 2026, 9,254 stock-days).**
Read point-in-time — today's close as the current weekly close — the panel reproduces
**64.9 % of Chartink's stock-days at 61.5 % precision**. Read with **look-ahead** — the current
week's *final* close used on every day of that week — it reproduces **83.1 % at 97.8 %**, the same
agreement the other two scans reach. That is not a coincidence: Chartink's backtester evaluates
weekly candles as completed candles, so on a Tuesday it already knows the week's close. The
remaining 17 % are the usual plant gaps (832 no-bar days, 602 names without a clean 50-session
volume window) and near-misses at the 3.01 % edge. **Everything below uses the point-in-time
reading**, because that is the scan a trader can actually run at 15:30; it is also what Chartink's
*live* scan shows (the 63 names in `chartink_today.csv` are Thursday's closes).

The state holds ≈ 50 names a day (18 in 2018, 74 in 2021), median stay 5 sessions. An entry is
the first tight day after ≥ 5 sessions out: 26,767 since 2017, 21,378 in names turning over
≥ ₹2 crore a day.

## 2. Event study — what an entry is worth

Buy at the next open, mark *k* sessions later; in-sample = to 2022-12, out-of-sample = 2023 →
(`out/entry_slices.csv`). "Breakout" = a buy-stop at the 15-session high, working 10 sessions
(fills 47 % of the time).

| slice | IS n | IS +20 | IS +60 | IS breakout +60 | OOS n | OOS +20 | OOS +60 | OOS breakout +60 |
|---|---|---|---|---|---|---|---|---|
| all entries | 14,069 | +2.31 | +7.30 | +7.55 | 12,698 | +2.11 | +6.89 | +7.38 |
| turnover ≥ ₹2 cr | 10,355 | +2.16 | +6.87 | +6.90 | 11,023 | +2.19 | +6.39 | +7.04 |
| turnover < ₹1 cr | 2,377 | +2.80 | +9.12 | +10.23 | 1,052 | +1.73 | +11.31 | +11.28 |
| above 200-DMA | 11,106 | +2.28 | +6.98 | +7.14 | 10,830 | +2.35 | +7.54 | +7.94 |
| below 200-DMA | 2,963 | +2.42 | +8.48 | +9.09 | 1,868 | +0.70 | +2.90 | +3.41 |
| within 10 % of 52-week high | 4,717 | +2.23 | +7.32 | +6.87 | 6,042 | +3.14 | +8.48 | +8.27 |
| ADR ≤ 4 % | 3,751 | +1.92 | +4.97 | +4.33 | 4,716 | +2.17 | +7.14 | +7.77 |
| base depth ≤ 8 % | 228 | +2.08 | +2.72 | +1.23 | 307 | −0.11 | +3.47 | +3.81 |
| breadth > 40 % | 10,752 | +2.70 | +7.92 | +8.00 | 10,923 | +2.00 | +7.05 | +7.75 |
| the VBT-1-style combo filter | 382 | +1.59 | +2.67 | +1.54 | 637 | +1.64 | +5.86 | +5.83 |

Two things are unusual. The raw entry is positive at every horizon in both halves, more so than
either previous scan; and the slices barely move it — the thin names are *better*, the tightest
bases are *worse*, and the trend-and-quality combination that made VBT-1 work makes this worse.
The consolidation-after-a-run is the whole signal. Year by year the 60-session number is
+6.8 / −7.2 / −4.7 / +14.3 / +12.7 / −0.1 / +15.8 / +2.9 / −1.3 / +9.6 (2017 → 2026): still a
bull-market edge, but a smaller loser in the bad years than the other two.

## 3. The strategy — TWT-1

### Signal (at the close of session *t*)

The scan is true today and was false on each of the previous five sessions, and the name turns
over ≥ ₹2 crore a day on a 20-session average (the only filter, for tradability; without it the
result is the same). ≈ 40 signals a week; the book takes at most three a day.

### Entry, sizing

**Next session's open, market order.** The base-high breakout (3.7 % CAGR) and the
pullback limit (11.6 %) are both worse — the third time in three studies that waiting costs
money on a momentum entry. Equal weight, **10 slots, 10 % each, at most 3 new a session**,
ranked by 20-day turnover when there are more signals than slots (ranking by day-change,
relative volume or nothing: 16–17 %), never more than 1 % of the name's turnover.

### Exits

1. **Disaster stop 20 % below the fill**, as a GTT. 15 % is equivalent (21.6 % CAGR, −23 %);
   10 % breaks the strategy (the names swing 12 %+ inside their bases; §4).
2. **Trailing stop 20 % below the highest high since entry**, re-set daily as a GTT. This is the
   exit: 137 of 164 trades. Tighter (15 %) halves the CAGR and doubles the drawdown; wider
   (25–30 %) holds for a year at a time and thins the trade count further.
3. No target, no partials, no time stop, no moving-average exit (a 50-SMA exit is the
   higher-frequency variant, §4; a 21-EMA exit is a loser at 4.7 %).

### Regime

New entries only when **> 40 % of the tradable universe is above its 200-DMA** (35–40 % is the
plateau). It turns −43 % into −25 % for the loss of nothing: 17.2 % ungated, 20.9 % gated.

## 4. Results

₹10 lakh, 2017-10-16 → 2026-09-09, 25 bps a side, fills on the exchange tick.

| | **TWT-1** | TWT-1 · 50-SMA exit | VBT-1 (10 Sep) | MOM-1g (11 Sep) | Midcap 150 |
|---|---|---|---|---|---|
| **CAGR** | **20.9 %** | 15.6 % | 18.2 % | 13.9 % | 15.4 % |
| max drawdown | **−24.7 %** (Sep 2024 → Aug 2025) | −38.3 % | −27.9 % | −31.4 % | −44.2 % |
| Calmar | 0.85 | 0.41 | 0.65 | 0.44 | 0.35 |
| Sharpe | 1.21 | 0.86 | 0.97 | 0.76 | |
| trades · win rate · profit factor | **164** · 41 % · 2.71 | 543 · 35 % · 1.68 | 761 · 38 % · 1.55 | 509 · 33 % · 1.56 | |
| avg win / avg loss / avg trade | +55.6 % / −12.4 % / +15.4 % | | +16.5 / −6.1 / +2.5 | | |
| avg hold · time invested | 105 sessions · 78 % | 27 · 67 % | 18 · 63 % | 31 · 71 % | 100 % |
| IS CAGR (→ 2022) / OOS (2023 →) | 11.1 % / 36.1 % | 6.2 / 30.1 | 12.6 / 26.0 | 9.5 / 19.8 | 12.3 / 20.2 |
| final equity | ₹54.1 lakh | | ₹44.4 lakh | ₹31.8 lakh | |

| year | TWT-1 | Midcap 150 | trades | win % |
|---|---|---|---|---|
| 2017 (Oct–Dec) | +3.0 | +12.6 | 4 | 0 |
| 2018 | −2.8 | −13.3 | 22 | 41 |
| 2019 | +3.5 | −0.3 | 4 | 25 |
| 2020 | +13.0 | +24.4 | 26 | 23 |
| 2021 | **+53.1** | +46.8 | 12 | 67 |
| 2022 | −3.5 | +3.0 | 25 | 40 |
| 2023 | **+66.9** | +43.7 | 14 | 50 |
| 2024 | **+62.8** | +23.8 | 15 | 67 |
| 2025 | −6.2 | +5.4 | 18 | 22 |
| 2026 (→ 9 Sep) | +22.9 | +4.6 | 24 | 50 |

No year worse than −6 %, which is what the gate plus the wide trail buy; and three years that
made everything, on 12–15 trades each. Monthly returns are in `out/final_monthly.csv`, the
curves in `out/final_equity.png`, every trade in `out/final_trades.csv`.

### The three sleeves together

| monthly-marked | VBT-1 | MOM-1g | TWT-1 | equal-weight, rebalanced monthly | 50/50 VBT-1 + TWT-1 |
|---|---|---|---|---|---|
| CAGR | 18.2 % | 13.7 % | 21.3 % | **18.4 %** | **20.3 %** |
| max drawdown | −24.9 % | −28.4 % | −23.6 % | **−22.1 %** | **−19.3 %** |

Monthly correlations 0.48 (VBT-1/MOM-1g), 0.50 (VBT-1/TWT-1), 0.60 (MOM-1g/TWT-1); TWT-1 shares
**no trade** with VBT-1 and two with MOM-1g. VBT-1 turns over in three weeks, TWT-1 in five
months: one is a swing book and the other a position book, drawn from the same universe by
different events. If two of the three ever run, VBT-1 + TWT-1 is the pair.

### What moves the number (`out/final_sensitivity.csv`)

| change | CAGR | max DD | trades | note |
|---|---|---|---|---|
| **TWT-1 as specified** | **20.9 %** | **−24.7 %** | 164 | |
| stop 15 / 30 % | 21.6 / 19.4 | −23 / −25 | 186 / 162 | flat |
| trail 15 / 25 / 30 % | **9.6** / 18.2 / 15.5 | −43 / −23 / −28 | 399 / 128 / 73 | 15 % is a cliff; 20–25 is the plateau |
| 8 / 15 slots | 26.3 / 14.7 | −27 / −29 | 133 / 277 | concentration pays, again; and again it is where the luck lives |
| rank by nothing / relative volume / day-change | 15.8 / 17.2 / 16.3 | −30 / −31 / −28 | | |
| costs 40 / 60 bps a side | 20.3 / 19.5 | −25 / −26 | | long holds make costs nearly irrelevant |
| gate 30 / 35 / 45 / 50 % · none | 18.6 / 20.3 / 13.5 / 14.4 · 17.2 | −31 / −26 / −26 / −35 · −43 | | |
| breakout entry (buy-stop at base high) | 3.7 | −38 | 195 | |
| limit-at-close entry | 11.6 | −28 | 174 | |
| add the 50/200-SMA trend filter | 12.3 | −29 | 166 | the filter that made VBT-1 hurts here |
| entry after ≥ 10 / ≥ 20 sessions out | 19.7 / 15.8 | −27 / −32 | 188 / 199 | |
| no liquidity filter / turnover ≥ ₹5 cr | 19.5 / 22.5 | −28 / −27 | 169 / 169 | |
| 50-SMA exit instead of the trail | 15.6 | −38 | 543 | the higher-frequency form |

## 5. Caveats, stated plainly

**164 trades.** Ten of them are 53 % of gross profit; the best single trade is 11 %. Three years
(2021, 2023, 2024) carry the CAGR on 12–15 trades each. Sharpe 1.2 on that trade count is a
result that a different draw of the same market could easily make 0.6, and the one-year hold
means the 2017–2026 window contains perhaps 15 *independent* observations of the book. Read the
20.9 % as "an edge with the right sign and a wide confidence interval". The 50-SMA variant's
543 trades and 15.6 % are the more believable statement of the same thing.

**The trail is the strategy, and it is a slow one.** Average hold 105 sessions, longest 601
(BOSCHLTD, Aug 2022 → Jan 2025, +78 %). The GTT trailing stop has to be re-set every session it
ratchets, which is a process, not a signal; and a 20 % give-back on a ₹1-lakh line is a
₹20,000 open loss that the book will sit through as a matter of routine.

**Regime and history** as before: the out-of-sample 36 % is 2023–24; the in-sample 11 % is the
guide. Modelled fills, 25 bps, no interest on idle cash, sparse corporate actions before 2024,
one history, research code. And the reproduction question in §1: the scan traded here is the
one visible at the close, not the one in Chartink's backtest export.

## 6. If it goes into Baskfy

The simplest of the three to build: the signal is three weekly closes and one monthly low
(both derivable from `ohlcv_daily` in the nightly job), the entry is a next-open market order,
and the exit is a **ratcheting GTT** — which the swing book's GTT path already places and which
`kite_client.place_gtt_stop` already carries a guard for (non-negotiable #6's caveat). It needs
one new mechanism: **modify the GTT upward** whenever the high makes a new high, once a session,
after the close. As a sleeve it is a position book, not a swing book, and should be sized as
one: ten lines, each held for months, is not the swing sleeve's risk shape.

## 7. Files

| file | what |
|---|---|
| `STRATEGY.md` | this note |
| `tscan.py`, `tscan_verify.py` | the scan (weekly / monthly readings), entry detector, and the reproduction check including the look-ahead finding |
| `explore_tc.py` | entry event study (`out/entry_slices.csv`, `out/entry_features.pkl`) |
| `grid_tc.py` | entry × stop × gate × exit grid (`out/grid_tc.csv`, `out/grid_tc2.csv`) |
| `final_tc.py` | TWT-1, sensitivity, yearly / monthly tables, benchmarks |
| `out/final_*.{csv,json,png}` | trades, equity, yearly, monthly, metrics, sensitivity, chart |
| `chartink_backtest.csv`, `chartink_today.csv` | Chartink's export of the scan's history and of 11 Sep's 63 names |
| `data/index_series.csv`, `out/breadth200.csv` | benchmarks and the 200-DMA breadth series |

Run order, from this folder, with `../volume-breakout/data/panel.pkl` built: `python tscan_verify.py`,
`python explore_tc.py`, `python grid_tc.py`, `python final_tc.py`. `vbt/` is a symlink to
`../volume-breakout/vbt`.
