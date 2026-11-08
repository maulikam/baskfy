# Swing-ML → Multibagger-ML — a LightGBM ranker over Baskfy's data plant

*Research note, 8 Nov 2026. Data: the `research/volume-breakout/aws/` export of `ohlcv_daily`
(3,744,814 bars, 4,919 instruments, 2017-01-02 → 2026-09-09), `instrument`, `index_def`,
`index_member_daily`, plus `data/index_series.csv` (NIFTY 50 / Midcap 150 / Smallcap 250 closes).
Everything quoted here is reproduced by the scripts beside this file. Nothing here has traded
real money. The first question (short swing trades) is answered **no**; the second (which names
double inside a year) is answered **yes, with a stated weakness** — read §0 and §6 before anything
else.*

## 0. The one-paragraph answer

The Kepler analogy — train a model to find weak signals in a universe too large for a human to
scan — does **not** work for 2–4-week swing trades on NSE: across four barrier geometries
(+5/−3/10 sessions through +15/−7/30) a walk-forward LightGBM ranker is reliably better than chance
(out-of-sample AUC 0.53–0.60, top-20-a-day hit rate 4–7 points above the base rate) and reliably
**unable to pay for itself** — the top-20's mean trade return beats the universe by ~0.1–0.4 % and a
0.5 % round trip eats it in all but the widest geometry's best years (§2). The same machinery pointed at a different
question — *which of today's ~1,200 liquid names will touch 2× within 250 sessions?* — carries a
real, persistent edge: AUC 0.60–0.69 **in every test year 2020–2025**, and through signal-year 2024 the
model's top 20 double at **1.6–2.5× the universe's rate** (34 % of picks touch 2×, 22 % still hold it at
12 months, against 17 % / 10 % for an equal-weight universe); signal-year 2025 is 1.0×. A monthly-tranche
book of the top 20 investable names (≥ ₹50, ≥ ₹5 cr/day) returns **35.7 % CAGR, −22 % max drawdown,
Sharpe 2.0** from Jan 2020 to 6 Nov 2026 (32.7 % / −22 % / 1.8 measured from Jan 2021, once the book is
fully loaded), against 23.9 % / −23 % for the universe equal-weight, 33.1 % / −31 % for a naive 12-1
momentum top-20, and 20.3 % / −44 % for NIFTY Smallcap 250 (§4). The honest caveats: the whole test
window is a small-cap bull market with one bear year; the model's favourite features are *high
volatility, low price, young listing, illiquidity* — lottery traits punished in signal-years 2024
(unfiltered top-20: −11 % over 12 months, universe 0 %) and 2025 (−9 % vs +3 %); the export carries no
fundamentals, promoter holding, delivery % or market cap; and the ranking is a candidate generator, not
a portfolio. §6 lists what has to be true before this becomes a sleeve.

**Numbers refreshed 9 Nov 2026** on the export through 6 Nov 2026 (bars and index history from 2011; the
10 Sep export started in 2017, so training has ~100k more rows per fold). Against 10 Sep: investable
top-20 CAGR 34.1 → 35.7 %, drawdown −19.8 → −22.0 %, Sharpe 1.94 → 1.98; 2024 signals unfiltered −19 →
−11 %, investable +1 → +5 %; 2025 is now the weak year (investable +3 → −2 %, a fuller year).

## 1. The two questions, as labels

| | Swing (§2) | Multibagger (§3–5) |
|---|---|---|
| Entry | next session's open | next session's open |
| Label = 1 | high touches +U % before low touches −D %, within H sessions (both on one day → loss) | high touches **2×** entry within **250** sessions (`touch`); also recorded: `held` (close ≥ 2× at session 250), `fwd_ret_h`, best and worst point |
| Geometries tested | (U, D, H) ∈ {(5,3,10), (8,4,15), (10,5,20), (15,7,30)} | (2×, 250), (2×, 500), (1.5×, 250) |
| Base rate | 23–37 % by year | **1 % (2018) → 39 % (2020)** by signal year — see §3 |
| Code | `portfolioml/labels.py::triple_barrier` | `portfolioml/labels.py::multibagger` |

A stop gapped through at the open fills at the open, not at the barrier. Rows without a complete
forward path are scored but never trained on. Labels are computed on the **adjusted** `close/high/low`
(house rule 6); `close_raw` is used only for price floors.

**Universe (point-in-time by construction, no survivorship):** `instrument_type='EQ'`, series
EQ/BE/BZ (SME `SM/ST` out, ETFs out by `etf` membership and name regex), 20-day average traded value
≥ ₹2 cr, `close_raw` ≥ ₹20, ≥ 260 sessions of history, and not within ±60 sessions of a suspicious
overnight gap in the *preceding* 60 sessions (|r| > 60 %, the unadjusted corporate actions VBT §1
found; 1,244 rows quarantined — backward-looking only, so the universe never knows about a gap to come).
Six thin sessions (muhurat / special Saturdays, < 800 names printed) are dropped from the calendar,
the same six VBT found. Result: **1,697,930 name-sessions, 815 → 1,456 names a day, 2018 →**.
Index membership from the export starts 2021-08 and is not used.

## 2. Swing trades: the negative result

Features (§5), lambdarank with one query per session, yearly walk-forward (train ≤ 31 Dec Y−1 minus
a 45-session purge; test = year Y), 25 bps a side. `out/swing_barrier_geometry.csv`; per-year rows
from `explore_barriers.py`.

| Geometry | mean AUC | top-20 hit | base | top-20 trade ret | **net of 0.5 %** | universe ret | avg hold |
|---|---|---|---|---|---|---|---|
| +5/−3/10 | 0.535 | 38.4 % | 34.0 % | +0.13 % | **−0.37 %** | −0.03 % | 3 sessions |
| +8/−4/15 | 0.555 | 34.3 % | 29.2 % | +0.27 % | **−0.23 %** | +0.14 % | 4–6 |
| +10/−5/20 | 0.558 | 33.6 % | 28.9 % | +0.42 % | **−0.08 %** | +0.27 % | 6–9 |
| +15/−7/30 | 0.582 | 32.9 % | 25.7 % | +0.88 % | **+0.37 %** | +0.72 % | 10–13 |

Per year the picture is the same at every geometry: positive net only in 2021 and 2023 (when the
universe itself was positive), negative in 2022 and 2025. The top-20's return tracks the *universe's*
return — the model is ranking correctly but the dispersion it can exploit over 2–4 weeks is smaller
than the costs. Alternatives tried on fold 2022 (`explore_objectives.py`): binary with AUC early
stopping (0.528), lambdarank (0.543), regression on the cross-sectionally demeaned exit return
(0.518); NDCG early stopping on a 10 % time slice was too noisy (2–190 iterations) and was replaced
by a fixed 250 rounds. Adding breakout / liquidity-trend / breadth features (v2) did not change it.

**Verdict:** do not build a swing sleeve on this. If the ML score is ever used in the swing book it
is as a tie-breaker on Kullamägi's own candidates, never as the signal.

## 3. Multibagger: the walk-forward

`explore_multibagger.py data/dataset_mb_u100_h250.parquet --target=touch`. Train on every fifth
signal date ≤ 31 Dec Y−1 minus 256 sessions (the full horizon, so no training label's path overlaps
the test year); test on every universe row of year Y whose 12-month path is complete. Lambdarank,
31 leaves, 300 rounds, lr 0.03, `label_gain` default. `out/multibagger_mb_u100_h250_touch.csv`.

| Signal year | train rows | base rate (touch 2×) | **ML top-20** | ML top-decile | 12-1 mom top-20 | 12m-return top-20 | universe | held 2× at 12m: top-20 / univ | AUC |
|---|---|---|---|---|---|---|---|---|---|
| 2020 | 121,820 | 39.2 % | **61.3 %** | 60.1 % | 41.4 % | +111 % | +78 % | 42 % / 27 % | 0.690 |
| 2021 | 142,347 | 17.3 % | **42.6 %** | 34.4 % | 45.3 % | +50 % | +19 % | 23 % / 8 % | 0.692 |
| 2022 | 167,848 | 12.0 % | **23.9 %** | 21.5 % | 13.8 % | +34 % | +24 % | 17 % / 8 % | 0.682 |
| 2023 | 205,200 | 30.7 % | **51.9 %** | 47.4 % | 57.7 % | +85 % | +57 % | 33 % / 19 % | 0.653 |
| 2024 | 244,129 | 8.0 % | **16.4 %** | 13.6 % | 19.7 % | **−11 %** | −1 % | 5 % / 2 % | 0.658 |
| 2025 (→ Nov) | 288,789 | 5.5 % | **5.4 %** | 6.8 % | 8.5 % | **−8 %** | +3 % | 2 % / 3 % | 0.601 |

**Signal-year 2025 is the newest walk-forward year and the price-only model showed no edge there**
(investable top-20 hit 4.5 % vs 5.4 % base, −2.2 % vs +3.9 %). It is out of sample for training, but the
design was chosen with it visible, so no year here is a clean holdout. Live expectation is set from 2025 until
PM4 passes, not from the 2020–25 average.

Three things to read off this table:

1. **The ranking edge is persistent; the top-20 edge is not.** AUC ≥ 0.60 in every year, including the
   two bad ones. The top-20 doubles at 1.6–2.5× the base rate through 2024, and 1.0× in 2025.
2. **The base rate is the regime.** Signals taken in 2018 doubled 1 % of the time, in 2020 39 %. A
   model cannot know which year it is; the ranker only promises *which names* within a day. The
   portfolio (§4) has to carry that risk (2022 +10 %, 2025 −9 %).
3. **2024–25 is the weakness, and it is structural.** The top 12 features by mean gain are
   `vol60` 16.7 %, `log_px` 8.4 %, `atr14_pct_cs` 7.7 %, `n_hist` 5.0 %, `r504_cs` 3.3 %,
   `px_sma200_cs` 2.9 %, `amihud20_cs` 2.8 %, `hi252_vs_ath` 2.8 %, `r756` 2.7 %, `px_sma200` 2.7 %,
   `r252_cs` 2.5 %, `dist_lo252` 2.4 %. Volatile, cheap, thinly traded, recently listed names *do*
   double more often — and in the 2024–25 small/micro-cap correction the unfiltered top-20 lost
   11 % (2024) and 8–9 % (2025) over twelve months while the universe was flat to up. The 12-1 momentum
   baseline had a higher top-20 hit rate in 2021/2023/2024/2025; what the ML adds is a better *whole
   ranking* and lower drawdowns in the book, not more doublers at the very top.

Variants (`out/multibagger_mb_u100_h500_touch.csv`, `..._mb_u50_h250_touch.csv`,
`..._mb_u100_h250_held.csv`): 2× within **500** sessions has only four complete folds (2021–24), AUC
0.52–0.61, worse than 250. **1.5×** within 250 has the same shape as 2× (AUC 0.56–0.65, top-20 hit
1.0–1.4× base) with less lift. Training on `held` (≥ 2× at the close) instead of
`touch` is marginally worse everywhere. **Label repair tested 9 Nov** (`--target=clean40` /
`clean30`: touched 2× *and* never closed below −40 % / −30 %): it changes nothing, because
round-trippers are rare — 0.1–1 % of the top-20 in every year but 2020 (3.3 %). The 2024 picks that
hurt did not double and then fall; they simply fell 20–40 % without ever doubling. The label is not
the problem; the information is (§6.1–6.3); graded relevance (0/1/2/3 by horizon return) was unstable
(AUC 0.45 in 2020, trained on the 2018–19 bear alone). **`touch`, 2×, 250 sessions is the label.**

## 4. The book: monthly tranches

`run_multibagger.py mb_u100_h250 touch`; `portfolioml/tranches.py`. On the first session of each month,
1/12 of capital goes equal-weight into the top-N names by score; each line is held 250 sessions
(a close below −35 % freezes the line at −35 % where `stop35` is on); 25 bps a side. Twelve slices
compound independently, so the book is continuously ~fully invested after the first year. A line
with no bar on a day carries its last close (suspended/delisted names are not treated as zero —
one of the open items is to decide whether they should be). Marks are daily closes.

| Book (Jan 2020 → 6 Nov 2026) | CAGR | max DD | Sharpe | from Jan 2021: CAGR / DD / Sharpe | picks | touched 2× | ≥ 2× at 12m | mean 12m | median 12m |
|---|---|---|---|---|---|---|---|---|---|
| **ML top-20, investable (≥ ₹50, ≥ ₹5 cr/day)** | **35.7 %** | **−22.0 %** | **1.98** | 32.7 % / −22.0 % / 1.82 | 1,420 | 29.9 % | 18.2 % | +45 % | +18 % |
| ML top-20, investable, −35 % stop | 33.8 % | −22.7 % | 1.98 | 31.6 % / −22.7 % / 1.83 | 1,420 | 29.9 % | 18.2 % | +45 % | +18 % |
| ML top-20, investable, Smallcap-250 > 50-DMA gate, stop | 24.8 % | −15.7 % | 1.93 | 24.7 % / −15.7 % / 1.86 | 1,040 | 30.8 % | 18.4 % | +45 % | +18 % |
| ML top-20, whole universe | 37.2 % | −23.4 % | 2.01 | 33.7 % / −23.4 % / 1.84 | 1,420 | 33.9 % | 21.7 % | +46 % | +16 % |
| ML top-50, whole universe | 36.0 % | −24.3 % | 1.93 | 33.3 % / −24.3 % / 1.77 | 3,550 | 31.9 % | 20.2 % | +46 % | +16 % |
| 12-1 momentum top-20 (baseline) | 33.1 % | −30.7 % | 1.52 | 29.6 % / −30.7 % / 1.32 | 1,420 | 31.6 % | 18.4 % | +43 % | +17 % |
| 12-month return top-20 (baseline) | 32.2 % | −30.3 % | 1.51 | 28.6 % / −30.3 % / 1.31 | 1,420 | 32.0 % | 19.2 % | +43 % | +16 % |
| Universe equal-weight | 23.9 % | −22.6 % | 1.38 | 21.9 % / −22.6 % / 1.23 | 60,274 | 16.9 % | 9.9 % | +26 % | +10 % |
| Investable equal-weight | 22.4 % | −20.8 % | 1.39 | 20.5 % / −20.8 % / 1.24 | 45,336 | 14.5 % | 8.4 % | +23 % | +9 % |
| NIFTY 50 / Midcap 150 / Smallcap 250 (price) | 11.1 / 20.1 / 20.3 % | −38 / −39 / −44 % | | 10.4 / 19.3 / 19.5 % | | | | | |

Calendar years, ML top-20 investable vs investable equal-weight vs Smallcap 250: 2020 **+53 %** /
+33 % / +24 %; 2021 **+83 %** / +52 % / +60 %; 2022 **+10 %** / +4 % / −5 %; 2023 **+66 %** / +48 % /
+47 %; 2024 **+62 %** / +28 % / +26 %; 2025 **−9 %** / −4 % / −7 %; 2026 (→ 6 Nov) +2 % / +1 % / +4 %.

Read it this way: the ranker's value over naive momentum is **drawdown** (−22 % vs −31 %, Sharpe 2.0
vs 1.5) with ~3 points more CAGR, not more doublers — the momentum baseline finds about as many 2×
names. The investability floor costs ~4 points of hit rate and ~1.5 points of CAGR, and is what keeps
2024 survivable (+5 % for the investable top-20's 2024 signals vs −11 % unfiltered). The index
gate (Smallcap 250 above its 50-DMA) cuts CAGR by ~9 points for ~6 points of drawdown — in this window
it is not worth it; a breadth gate deserves a proper test before being ruled out. The −35 % stop
costs ~2 points and does not reduce drawdown, which says the losers mostly bleed rather than crash.

**Book-level levers (re-run 9 Nov on the 6 Nov export, `explore_book.py`, `out/book_levers/summary.csv`)
— none moves it.** Same scores, investable universe: top-10 vs top-20 (35.9 % at −19.5 % vs 35.7 % at
−22.0 %), rank-weighting (35.5 %), an ML × 12-1-momentum ensemble (50/50 36.2 %, 2:1 36.4 %,
both-must-be-high 35.4 %; top-10 of the 50/50 36.9 % at −28 %), and "re-rank, don't sell" at the top
quartile / decile (33.1 % / 34.9 %, drawdown slightly *worse*). All 13 variants sit in a 33–37 % band
with −20 to −29 % drawdowns. The ranking's information content sets the outcome; how the list is sliced
does not. That is the argument for §6.1–6.3 over any further tuning.

**What is flattering about this test and should be said plainly:** the window is Jan 2020 → Nov 2026
— the COVID crash is only 3/12 invested, and the universe equal-weight itself compounded at 23.9 %.
There is **one** real bear year for small caps (2022) and one correction (2024-25) in the out-of-sample
set. 2018–19 are in training only (the label needs a year to resolve and the features a year to warm
up), so the model has never been *tested* on a 2018-type year where 1 % of names doubled.

## 5. Features (95), all at the close of t from data ≤ t — `portfolioml/features.py`

- **Per name (42):** returns 1/5/21/63/126/252/504/756 sessions, 12-1 and 6-1 momentum, vol-adjusted
  3-month momentum; 20/60-day return volatility and their ratio, ATR-14 %; volume vs 20-day and
  5-vs-50-day averages, 20-vs-60-day turnover trend, log 20-day turnover, Amihud illiquidity; distance
  from 20/63/252-day highs and lows, **from the all-time high** (`cum_max`), 52-week high vs ATH;
  price vs 20/50/200-DMA and 50-vs-200; gap, range position, 5- and 20-day up-day fraction, max/min
  1-day return in 20, Cutler RSI-14, 5-day range %, log price, sessions since listing (`n_hist`),
  relative strength vs NIFTY 50 at 21 and 63 sessions.
- **Market (24):** NIFTY 50 / Midcap 150 / Smallcap 250 21- and 63-day return, vs 20- and 50-DMA,
  20-day vol, drawdown from 252-day high; universe breadth above 200/50-DMA, share positive over 21
  days, share at 20-day highs, share up on the day, 20-day change in 200-DMA breadth.
- **Cross-sectional (29):** percentile rank within the day's universe of the ranked set in `RANKED`.

Everything is `float32`; `name_features` runs in six instrument chunks and the whole build is
~25 s (`build_dataset.py`), the walk-forward ~1 min per fold on two cores.

## 6. Open items, in the order they matter

1. **Fundamentals are the missing half.** The export has none; `fundamental_daily` in the plant has
   M-cap and P/E from Aug 2025 only (status page, Tree 3). Maulik's own multibagger criteria —
   revenue/EPS compounding, D/E, ROE/ROCE, promoter holding — are the features most likely to fix
   the 2024 weakness (they separate a cheap *business* from a cheap *stock*). Point-in-time
   quarterly results and shareholding patterns are the data step; screener.in exports are not
   point-in-time and must not be used for training.
2. **Sector.** The 9 Sep 2026 ranking has three sugar names in the top 20 the model cannot know are
   one bet. `instrument` carries no industry; NSE's indices (`nifty-*` sector `index_def` rows) can
   proxy it from 2021-08, and a sector-neutral top-N (max 3 per sector) is a one-line change in
   `tranches.simulate`.
3. **Delivery %, FII/DII flow, bulk deals** — in the plan from the start, not in the export; the
   bhavcopy archive on the box has delivery data (VBT KICKOFF).
4. **Delisted/suspended lines** are carried at last close in the book; count them and decide
   (likely: mark at zero after 60 sessions without a bar).
5. **A 2018-type year is untested.** Re-run when the plant has 2011→ bars (D5 backfill) so 2013
   and 2018 become test folds.
6. **Hold longer without a longer label — "re-rank, don't sell".** When a tranche turns 12 months
   old, keep every name still in the top quartile of that day's ranking and replace only the rest.
   Winners compound for years, losers leave; turnover falls below 1/12 a month. Needs the daily
   score history (`mb_score_daily`) to test properly — a one-line rule in `tranches.simulate` once
   the walk-forward scores every session, which they already do from 2020.
7. **Retrain cadence and drift.** Yearly refits here; a sleeve would refit quarterly, keep every
   model file, and compare the live ranking against the previous model's before publishing.
8. **The ranking is stale by two months** (`out/latest_ranking_mb_u100_h250.csv` is as of
   2026-09-09). `export_bars_local.sh` refreshes `data/` from the local Postgres.

## 7. Reproduce

```bash
cd machinelearning/python/research/portfolio-ml
pip install -r requirements.txt
cp ../../../../legacy/python/research/volume-breakout/aws/*.csv.gz ../../../../legacy/python/research/volume-breakout/data/index_series.csv ../../../../legacy/python/research/volume-breakout/trading_day.csv.gz data/   # or: bash export_bars_local.sh
python build_dataset.py --mb 1.0 250                       # features (once) + 2×/250 labels  → data/dataset_mb_u100_h250.parquet
python explore_multibagger.py data/dataset_mb_u100_h250.parquet --target=touch   # §3 → out/multibagger_*.csv, out/mb_scores_*
python run_multibagger.py mb_u100_h250 touch               # §4 → out/mb_u100_h250_touch/
python score_latest.py mb_u100_h250                        # today's ranking + saved model
python build_dataset.py 0.10 0.05 20 && python explore_barriers.py data/dataset_u10_d5_h20.parquet 2021 2022 2023 2025   # §2
```

`run_research.py` (swing book simulation, `portfolioml/book.py`) is kept for completeness; §2's verdict
makes it moot.
