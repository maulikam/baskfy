# 05 — Factor formulas

This is the numerical contract of the whole product. Implement it in `packages/core/factors.py`
as pure Polars expressions over a per-instrument daily frame, and pin it with golden tests.

**Notation.** `P_t` = adjusted close on trading day `t` (`ohlcv_daily.close`).
`r_t = P_t / P_{t-1} - 1` (simple daily return).
Windows are **calendar offsets snapped to trading days** — this was recovered exactly from the
reference CSV export (see `docs/13-csv-export-schema.md` §3):

```
anniversary = as_of - relativedelta(months=K)
window      = all trading days in (anniversary, as_of]   # K ∈ {1, 3, 6, 9, 12}
start       = the first trading day STRICTLY AFTER the anniversary
```

> **CORRECTED 2026-08-31 (M11).** This block previously read
> `start = snap_forward_to_trading_day(as_of - relativedelta(months=K))` with a **closed**
> interval `[start, as_of]` — the first trading day *on or after* the anniversary. That
> contradicted the table immediately below, which is the empirically recovered ground truth, and
> the table wins. The two rules differ by exactly one bar whenever the anniversary is itself a
> trading day and agree otherwise. Counted against the exchange calendar in `ohlcv_daily`
> (`docs/PARITY-M11.md`):
>
> | K | `(anniversary, as_of]` | `[anniversary, as_of]` | recovered `N` (below) |
> |---|---:|---:|---:|
> | 1M | **22** | 22 | 22 |
> | 3M | **64** | 65 | 64 |
> | 6M | **121** | 122 | 121 |
> | 9M | **185** | 186 | 185 |
> | 12M | **247** | 248 | 247 |
>
> The half-open interval reproduces all five; the closed one reproduces one. 1M agreed only by
> accident — 2026-07-18 was a Saturday — which is why `*_one_month` was the single column family
> that reproduced before this was fixed.

As of **2026-08-18** those offsets span exactly:

| Window | Trading days (`N`) as of 2026-08-18 |
|---|---|
| 1 month | **22** |
| 3 months | **64** |
| 6 months | **121** |
| 9 months | **185** |
| 1 year | **247** |

> These are *derived*, not constants. They were recovered by solving `positive_days_percent = k/N`
> across all 271 rows of the reference export; only one minimal `N` fits each window, and the
> **same** `N` fits every instrument — which proves the window start is a shared calendar date,
> not a per-instrument bar count.
>
> An instrument without a full window of history gets `NULL` for that window and is excluded —
> never computed on a short window, because that would break the shared-denominator property.

---

## 1. Absolute return — CORRECTED 2026-08-22 (was off by one bar)

```
ret_N = (P_t / P_{t-(N-1)} - 1) × 100
```

where `N` is the window length in trading days from §"Notation" — the calendar-offset window
**inclusive of both endpoints**, as `docs/13` §3 recovered it. The base is therefore the window's
**first bar**, not the bar before the window starts.

**This line previously read `P_{t-N}`, and that was wrong.** It was written before any real price
history existed and was never exercised: the parity test that would have caught it needed bars the
repository did not have, so it skipped. Measured against all 271 rows of the reference export on
real NSE bars, the base one bar later reproduces the file and the documented base does not:

| column | base `P_{t-N}` (was) | base `P_{t-(N-1)}` (is) |
|---|---:|---:|
| `absolute_return_one_month` | 0 / 271 exact | **265 / 271** |
| `absolute_return_three_months` | 1 / 270 | **260 / 270** |
| `absolute_return_six_months` | 0 / 270 | **256 / 270** |
| `absolute_return_nine_months` | 0 / 269 | **250 / 269** |
| `absolute_return_one_year` | 0 / 268 | **240 / 268** |

CUPID is the worked example: the published `ret_1m` of 37.03 is `284.03 / 207.27 − 1`, where
207.27 is the close **21** bars back; 22 bars back is 214.78 and gives 32.24.

Everything downstream inherits the base: `sharpe_N` is a ratio of two window quantities, and
`vol_N`, `rsi_N`, `high_1y` and `away_from_high_1y` all read the same window. All of them failed
271/271 for this one reason.

**The residual is a calendar difference, not a formula one.** Exact matches fall from 265 to 240
as the window lengthens, which is drift between our trading calendar and the reference's, not a
second formula defect — see `docs/DECISIONS.md` §21.7 and the merge's `DECISIONS-MERGE.md` M10.1
and M11.1.

## 2. Volatility — annualised, per window — AMENDED 2026-08-31 (`ddof=1` → `ddof=0`)

```
vol_N = stdev(r_{t-N+1 … t}) × sqrt(252) × 100        (POPULATION stdev, ddof=0)
```

Each window is annualised, so `vol_1m` and `vol_12m` are on the same scale. This is confirmed
by the site exposing `Volatility 1y/9m/6m/3m/1m` values that are all in the 20–60 range for the
same stock, which is only possible under per-window annualisation.

**What changed, and why.** This section said "sample stdev, ddof=1" in words from the day it was
written, and nothing had ever checked it: the row-by-row reproduction test needed adjusted price
history the repository does not carry, so it skipped, and a skipped test is never run. Measured
against the 271-row reference export (`docs/PARITY-M11.md` §4B) the ratio published/computed is
**constant at `√((N−1)/N)` to seven significant figures**, with a p05→p95 spread of 4×10⁻⁶ across
271 independent instruments:

| window | measured ratio (p05 / median / p95) | `√((N−1)/N)` |
|---|---|---|
| 1M (N=22) | 0.977006 / **0.977008** / 0.977010 | **0.977008421** |
| 3M (N=64) | 0.992156 / **0.992157** / 0.992158 | **0.992156742** |

That is the signature of `ddof` and of nothing else. Sweeping the alternatives, `ddof=0` with
`m=N` returns and √252 annualisation gives a median absolute error of **1.6×10⁻⁷**, against
6×10⁻³ for `m=N−1` and 1.3×10⁻³ for √250 — a factor of 40,000.

**On whose authority.** This is a deliberate **spec amendment**, not a bug fix: the prose above
was explicit and the code faithfully implemented it. Maulik took the decision on **2026-08-31**,
in answer to a question that named the consequence — `vol_12m` is the input to the desk's GTT
stop sizing (non-negotiable #4), so amending it moves live stop prices. It was landed together
with the §5 RSI amendment, in one change with one fixture regeneration, so stops move once rather
than twice in a week. `sharpe_N` (§3) moves with it by construction.

**A residual that this does not close, and cannot.** Even at `ddof=0` the reproduction error is
~1.6×10⁻⁷, while `COLUMN_TOLERANCE["volatility_*"]` in the parity harness is 5×10⁻¹¹. The export
stores volatility to 8 decimals and our `close` is the adjusted price rounded to 2 dp at write
time (house rule 8), which injects ~10⁻⁵ relative noise per return and lands at ~10⁻⁷ in
annualised volatility. **`volatility_*` cannot reproduce to 5×10⁻¹¹ from stored data at any
`ddof`.** That is a tolerance defect, recorded here and in `docs/PARITY-M11.md` §8 rather than
silently loosened.

## 3. "Sharpe return" — **VERIFIED EXACTLY**

The product's "Sharpe return" is *not* the textbook Sharpe ratio. It is simply the window's
absolute return divided by that window's annualised volatility, with **no risk-free rate**:

```
sharpe_N = ret_N / vol_N
```

Verification against live values (CUPID, 19 Aug 2026):

| Window | `ret_N` | `vol_N` | `ret/vol` | Site value |
|---|---|---|---|---|
| 1 year  | 753.00 | 57.93 | 12.999 | **13.00** ✅ |
| 6 months| 234.50 | 55.02 | 4.262  | **4.26** ✅ |
| 3 months| 138.25 | 49.12 | 2.814  | **2.81** ✅ |
| 1 month |  37.29 | 54.03 | 0.690  | **0.69** ✅ |

Second instrument (WELCORP, 1 year): `126.18 / 35.82 = 3.522` → site shows **3.52** ✅
Third (HFCL, 1 year): `200.96 / 49.08 = 4.094` → site shows **4.09** ✅

**Bulk verification against the CSV export** (`fixtures/reference-screen-export-2026-08-18.csv`,
271 instruments × 5 windows): the identity holds for **1,355 / 1,355 cells**, maximum absolute
error 0.0051 — exactly the rounding granularity of 2-decimal storage. There is no risk-free
rate, no annualisation adjustment and no excess-return term anywhere in it.

Note the storage convention: `volatility_*` is persisted as a **decimal fraction**
(e.g. `0.5793179400`) and rendered as `57.93`. So in code the identity is:

```
sharpe_N = ret_N_pct / (vol_N_fraction * 100)
```

Guard: if `vol_N == 0` or is NULL → `sharpe_N = NULL`.

## 4. Blended (average) factors — **VERIFIED EXACTLY**

A blend is the plain arithmetic mean of its component single-window factors:

```
avg_sharpe_12_6_3_1 = (sharpe_12m + sharpe_6m + sharpe_3m + sharpe_1m) / 4
```

Verification (CUPID): `(13.00 + 4.26 + 2.81 + 0.69) / 4 = 5.19` — the site's "Sorting Factor"
column for that screen shows **5.19** ✅

Bulk verification: recomputing `avg_sharpe_12_6_3_1` from the CSV export's rounded components
reproduces the export's row order, with only 48 inversions all ≤ 0.0075 — i.e. below the
0.0025 granularity that 2-dp component rounding induces. Confirmed.

If **any** component is NULL the blend is NULL (do not silently average over fewer terms — that
would rank young listings above seasoned ones).

The eleven blend shapes used by all three families (absolute / sharpe / rsi):
`12·9·6·3·1`, `12·9·6·3`, `12·9·6`, `12·9`, `12·6·3·1`, `12·6·3`, `12·6`, `12·3·1`, `12·3`,
`12·9·3·1`, `12·9·3` — plus `6·3` for the sharpe family only.

## 5. RSI over a window — AMENDED 2026-08-31 (Wilder@N → **Cutler@N−1**)

**Cutler's RSI** with period = the number of **returns inside** the window, `N−1` (so "RSI 1 year"
is still a ~246-period RSI, not a 14-period RSI sampled yearly):

```
gain_t = max(P_t - P_{t-1}, 0);   loss_t = max(P_{t-1} - P_t, 0)
avg_gain = mean(gain_{t-(N-1)+1 … t});   avg_loss = mean(loss_{t-(N-1)+1 … t})
RS  = avg_gain / avg_loss
RSI = 100 - 100 / (1 + RS)        (RSI = 100 when avg_loss == 0, 50 when both are 0)
```

A plain simple moving average of gains and losses. There is no seed and no recursion: the value
at `t` reads the window and nothing before it.

**What changed, and why.** This section previously specified Wilder's recursive smoothing
(`avg_x_t = (avg_x_{t-1} × (N-1) + x_t) / N`, seeded with a simple mean over the first `N`
observations) at period `N`. Sweeping method × period against the reference export
(`docs/PARITY-M11.md` §4C):

| column | Wilder, best period | Cutler, best period |
|---|---|---|
| `rsi_one_month` (N=22) | p=22 → **1/271** exact | **p=21 → 267/271** exact |
| `rsi_three_months` (N=64) | p=67 → **0/270** exact | **p=63 → 263/270** exact |

Two findings in one, and neither is a tuning difference. The smoothing family is a different
formula, and the period is `N−1`. `N−1` is internally consistent with §Notation and §1: a window
of `N` bars spans `N−1` returns, which is the same `N−1` that `ret_N = P_t / P_{t-(N-1)} - 1`
already uses. (The reference is not itself fully self-consistent — §11's `positive_days_percent`
is `k/N` over `N` returns, one of which reaches back before the window. Reproducing the reference
product is the job, so that inconsistency is reproduced too.)

Cutler's is also **path-independent**, which is the second, independent reason to prefer it:
Wilder's is seeded from the first `N` observations of whatever history it is handed, so its answer
depends on where the series was cut and it could never reproduce the reference without history
back to the listing date.

**Consequences of `N−1`.** A window that resolves to `N = 1` trading day spans zero returns, so
its RSI is **NULL**. Under Wilder@N that degenerate case returned 100 or 0 off a one-observation
seed; there is nothing to average now, and NULL is §Notation's own answer for a window without
enough history.

**On whose authority.** A deliberate **spec amendment**, taken by Maulik on **2026-08-31** with
the consequence named: `rsi_1m` is a live strategy input — the desk's RSI bands (>78 wait or
tranche, >82 trim a held position to a runner) and the F-penalty read it. Landed together with the
§2 volatility amendment in one change with one fixture regeneration.

Sanity: CUPID `rsi_12m = 67.41`, `rsi_1m = 75.48` — consistent with a strongly trending stock.

## 6. Beta — 1 year, against Nifty 50

```
beta_12m = Cov(r_stock, r_bench)_252 / Var(r_bench)_252
```

Benchmark = Nifty 50 total-return proxy (price index is acceptable; be consistent).
Align on common trading days; require ≥ 200 overlapping observations else NULL.
Negative betas are legitimate and must not be clipped (the site shows `OIL: -0.22`).

## 7. Beta-scaled factors

```
abs_div_beta_12m    = ret_12m    / beta_12m
sharpe_div_beta_12m = sharpe_12m / beta_12m
avg_sharpe_div_beta_12_9_6_3 = mean(sharpe_div_beta over those windows)
```
`beta <= 0` → NULL (the ratio is meaningless and would invert the ranking).

## 8. Skip-month momentum — INFERRED, calibrate

Academic 12-1 momentum skips the most recent month to avoid short-term reversal.

```
ret_12m_minus_1m = (P_{t-21}  / P_{t-252} - 1) × 100
ret_12m_minus_2m = (P_{t-42}  / P_{t-252} - 1) × 100
```

> ⚠️ The reference site's own numbers for CUPID (608.37 and 852.21) do **not** reconcile with
> this definition given its other published figures. Note that the CSV export proves the price
> series **is** already adjusted for splits and bonuses (CUPID's `ma_200 = 120.21` sits far below
> its `close = 284.03` despite a 4:1 bonus five months earlier — an unadjusted series would put
> MA200 above the close). So the discrepancy is a *definitional* one, not an adjustment artefact,
> and must be resolved empirically in Prompt 19. Implement the
> definition above on adjusted data, and add a golden test on a corporate-action-free stock.
> An alternative reading — the 12-month return of the window *ending* one month ago,
> `P_{t-21}/P_{t-273}` — should be evaluated during calibration and one of the two locked in.

## 9. Moving averages

```
ma_K = mean(P_{t-K+1 … t}),  K ∈ {20, 50, 100, 200}
```
Simple, not exponential. NULL if fewer than `K` bars.

## 10. Highs and distance from high — CORRECTED 2026-08-22 (the high is the INTRADAY high)

```
high_1y  = max(HIGH over the last N bars)      # the intraday high, NOT the close
high_ath = max(HIGH over the full history)
away_high_1y  = (P_t / high_1y  - 1) × 100    # ≤ 0
away_high_ath = (P_t / high_ath - 1) × 100    # ≤ 0
```
Verification (CUPID): `284.56 / 299.00 - 1 = -4.83%` — site shows **-4.83%** for both ✅
Bulk verification against the CSV export: **542 / 542 cells match**, max error 0.005 ✅

**This section previously read "on adjusted close", and its own worked example refuted it.**
CUPID's `299.00` is the highest price CUPID *traded* at; the highest price it *closed* at over the
same window is `294.86`. The engine implemented the prose rather than the example, and nothing
caught it because the parity test needed price history the repository did not have.

Measured against all 271 export rows on real bars, at every window length from 243 to 248 bars:

| input | exact |
|---|---:|
| `max(high)` | **249 / 268** |
| `max(close)` | 6 / 268 |

The result is flat in the window length, so this is the *input* and not the window — which is
what made it safe to correct while `docs/05` §1's window question was still open
(`DECISIONS-MERGE.md` M11.5). It also reads the way a person would say it: "away from its
one-year high" means away from the highest price it traded at.

Filter semantics: "Within Away from All Time High (%) = X" keeps rows where
`abs(away_high_ath) <= X`. `X = 100` disables the filter.

## 11. Percentage of positive days

```
pos_days_N = count(r_i > 0 for i in last N bars) / N × 100
```
Verification of plausibility (CUPID `pos_days_12m = 65.99%` ≈ 166/252 days) ✅

## 12. Circuit-hit days — INFERRED

A day counts as a circuit hit when the stock is locked at a band:

```
circuit_hit_t =  (high_t == low_t == close_t) AND (|r_t| >= band_t - epsilon)
              OR (close_raw_t >= upper_circuit_t - tick)
              OR (close_raw_t <= lower_circuit_t + tick)
circuits_N = count of circuit_hit over the last N bars
```

`upper_circuit` / `lower_circuit` come from the NSE bhavcopy where available. Where they are
not, fall back to the band heuristic on `|r_t|` ∈ {2%, 5%, 10%, 20%} with a 0.25% tolerance.
Store the method used per row so results stay auditable.

## 13. Volume / liquidity

> **Corrected from the CSV export:** the reference product's `volume` column is the exchange's
> **traded turnover in ₹**, not `close × shares`. The ratio of `volume` to `close × volume_shares`
> across 271 rows is 0.9999 ± 0.008, i.e. it is VWAP-based actual value traded. Use the exchange
> turnover field; only fall back to `close × volume` when turnover is unavailable, and record
> which was used.

```
vol_day_val   = exchange_turnover_t             # ₹; fallback: close_raw_t × volume_raw_t
vol_avg_1w    = mean(vol_day_val over 5 bars)
vol_avg_N     = mean(vol_day_val over N bars)   # N ∈ 21,63,126,189,252
median_vol_12m= median(vol_day_val over 252 bars)   # ₹; drives the liquidity filter
```
The screener's "Median Daily Volume One Year (in Rupees)" filter uses `median_vol_12m`, not the
mean — the median is the right choice because a single block deal should not qualify an illiquid
name.

## 14. Marketcap, P/E

From `fundamental_daily`; sourced from NSE. `pe` is nullable and the P/E range filter must
exclude NULLs when enabled (matching the reference product's documented behaviour).

**Source, as built.** NSE's `GetQuoteApi` (`functionName=getSymbolData`), one request per symbol,
archived per `(symbol, series, date)` before it is parsed. `/api/quote-equity`, which this section
originally implied, was retired in NSE's Next.js migration and now answers 403 at the Akamai edge;
the retired payload shape still parses out of the archive but is never fetched
(`DECISIONS-MERGE.md` §T3F.1).

* `marketcap_cr` = `tradeInfo.issuedSize` × that date's `close_raw` ÷ 1e7, rounded half-up to an
  integer rupee-crore. The exchange print wins over the quote's `lastPrice`; NSE's own
  `tradeInfo.totalMarketCap` is the fallback when issued size is absent.
* `pe` = `secInfo.pdSymbolPe`, **re-priced onto the target date**: `quoted_pe × close_raw ÷
  lastPrice`. The quote carries no history, so storing it verbatim into a past date's row would
  put the fetch day's price inside that date — look-ahead, forbidden by house rule 5. Verified
  against the archived bytes over 120 sampled rows of the 2026-08-18 fill (0 mismatches,
  `tools/tree3/repricing.sh`); the largest error it avoids in that sample is **11.10%**. The EPS vintage is still the
  fetch day's and cannot be otherwise: NSE publishes no point-in-time EPS series
  (`DECISIONS-MERGE.md` §T3F.3).
* `pb` and `div_yield` are **not in this payload** and are always NULL. They are kept as columns
  because the schema documents them, not because anything fills them.

**Scope.** The instruments with a bar on the date, not the whole listing register: NSE is fetched
at 1 req/s, so 2,540 traded names cost ~40 minutes where 10,481 listings would cost most of a
night, and a name with no bar has no `close_raw` to price against.

## 15. Wasserstein regime — INFERRED design

The instrument page shows `Market Quality → Wasserstein Regime: BULL`. Specify it as:

1. Take the instrument's last 63 daily returns → empirical distribution `Q`.
2. Take two reference distributions from that instrument's own history: `B_bull` = returns from
   the best-performing 20% of rolling 63-day windows, `B_bear` = worst 20%.
3. Compute the 1-Wasserstein (earth-mover) distance `W1(Q, B_bull)` and `W1(Q, B_bear)`
   (for 1-D this is the mean absolute difference of sorted quantiles).
4. `BULL` if `W1(Q,B_bull) < W1(Q,B_bear) × (1 - margin)`, `BEAR` if the reverse,
   else `NEUTRAL`. `margin = 0.1`.

Expose the two distances in the API so the label is explainable rather than magic.

## 16. PROS / CONS rules (instrument page)

Pure boolean rules over `factor_daily`, rendered as sentences. Ship at least:

| Rule | PRO text |
|---|---|
| `close > ma_200` | The close is above 200-day moving average. |
| `close > ma_100` | …100-day… |
| `close > ma_50` | …50-day… |
| `close > ma_20` | …20-day… |
| `abs(away_high_ath) <= 25` | The close is within 25% of all time high. |
| `beta_12m < 1.25` | The beta is less than 1.25 |
| `pos_days_12m > 55` | More than 55% of days in the last year closed positive. |
| `median_vol_12m >= 1e7` | Median daily turnover is above ₹1 crore. |

CONS are the negations, with their own wording. Keep the rule table in one module so it stays
consistent between API and UI.

---

## Golden-test requirements

`packages/core/tests/test_factors_golden.py` must assert:

1. The four CUPID identities in §3 and the blend identity in §4 reproduce to 2 decimals from a
   fixture of that instrument's adjusted history.
2. `away_high_*` reproduces −4.83% for the CUPID fixture.
3. A synthetic constant-return series produces `vol = 0` and `sharpe = NULL`.
4. A synthetic series with a known 2:1 split produces identical factors before and after
   adjustment is applied (adjustment invariance).
5. Blend with one NULL component yields NULL.
6. Beta of the benchmark against itself is exactly 1.0.
7. **Reference parity (the decisive test):** every numeric column of every one of the 271 rows in
   `fixtures/reference-screen-export-2026-08-18.csv` is reproduced within the tolerance implied by
   its stored precision. See `docs/13-csv-export-schema.md` §5.
8. The recovered window lengths for as-of 2026-08-18 equal 22 / 64 / 121 / 185 / 247.
9. Storage precision matches `docs/13` §4: values are rounded **at write time**, not at render.
