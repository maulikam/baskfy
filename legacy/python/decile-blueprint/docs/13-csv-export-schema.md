# 13 — The reference CSV export: schema and everything it proves

Source: an export of the "Investing 001" screen (universe NIFTY TOTAL MARKET, sort factor
AVERAGE SHARPE RETURN 12 6 3 1 MONTHS), 271 rows, trade date **2026-08-18**, 93 columns.
The file is committed at `fixtures/reference-screen-export-2026-08-18.csv` and is the single most
valuable artefact in this bundle: it is a labelled answer key.

---

## 1. The 93 columns, in export order

### Identity & OHLCV (11)
`name, symbol, series, date, open, high, low, close, volume, marketcap, volume_shares`

### Returns (5) — percent, 2 dp
`absolute_return_one_year, _nine_months, _six_months, _three_months, _one_month`

### Sharpe returns (5) — 2 dp
`sharpe_return_one_year, _nine_months, _six_months, _three_months, _one_month`

### RSI (5) — 4 dp
`rsi_one_year, _nine_months, _six_months, _three_months, _one_month`

### Volatility (5) — **decimal fraction**, 8–10 dp
`volatility_one_year, _nine_months, _six_months, _three_months, _one_month`

### Risk (1) — 10 dp
`beta`

### Circuits (5) — integers
`circuits_one_year, _nine_months, _six_months, _three_months, _one_month`

### Positive days (5) — percent, 2 dp
`positive_days_percent_one_year, _nine_months, _six_months, _three_months, _one_month`

### Highs (4)
`high_one_year, high_all_time, away_from_high_one_year, away_from_high_all_time`

### Moving averages (4)
`ma_200, ma_100, ma_50, ma_20`

### Liquidity (1)
`median_volume_one_year`   — in **rupees** (÷ 1e7 → ₹ crore)

### Universe membership flags (14) — 0/1
`is_nifty_50, is_nifty_next_50, is_nifty_100, is_nifty_200, is_nifty_500,
is_nifty_total_market, is_nifty_large_mid_250, is_nifty_midcap_150, is_nifty_smallcap_250,
is_nifty_microcap_250, is_nifty_mid_small_400, is_nifty_allcap, is_nifty_fno, is_etf`

### Top-beta flags (14) — 0/1, one per universe
`is_nifty_50_top_beta … is_etf_top_beta`

### Top-volatility flags (14) — 0/1, one per universe
`is_nifty_50_top_volatility … is_etf_top_volatility`

> Encoding note: the file is **UTF-8 with BOM** (Excel-friendly) and quotes only the `name`
> field. Reproduce both behaviours.

---

## 2. What the export proves (all checks run over all 271 rows)

| # | Finding | Evidence |
|---|---|---|
| 1 | `sharpe_return_N = absolute_return_N / (volatility_N × 100)` | **1,355/1,355 cells match**, max error 0.0051 = pure 2-dp rounding |
| 2 | `away_from_high = (close / high − 1) × 100` | **542/542 match**, max error 0.005 |
| 3 | Blend factors = arithmetic mean of components | Export order reproduces `mean(sharpe 1y,6m,3m,1m)` monotonically; the only 48 "inversions" are ≤ 0.0075, i.e. exactly the rounding granularity of 2-dp inputs |
| 4 | `volatility` is stored as a **decimal fraction**, annualised | range 0.179–0.618; the UI multiplies by 100 |
| 5 | `volume` is exchange **turnover in ₹**, not `close × shares` | ratio to `close × volume_shares` = 0.9999 ± 0.008 → it is VWAP-based traded value |
| 6 | `median_volume_one_year` is in **rupees** | CUPID 1,687,913,366 → ₹168.79 cr, matching the site's "Median Vol 1Y (₹ crs) 169.57" |
| 7 | `marketcap` is in **₹ crore**, integer | 4,901 … 971,984 |
| 8 | The price series **is adjusted for splits and bonuses** | CUPID (bonus 4:1 ex 09-Mar-2026) has `ma_200 = 120.21` vs `close = 284.03`. An unadjusted series would put MA200 *above* the close. Correct earlier assumption: adjustment for splits/bonus already exists; the Dec-2026 update adds **dividend** adjustment and a longer history |
| 9 | Universe flags are **denormalised onto the fact row** | 14 `is_*` booleans, not a join |
| 10 | `*_top_beta` / `*_top_volatility` are **precomputed per universe** | Within every universe the flagged rows' minimum beta strictly exceeds the unflagged rows' maximum beta — a clean rank threshold, impossible if computed post-filter |
| 11 | Index construction identities hold exactly | `NIFTY 500 = NIFTY 100 ∪ MIDCAP 150 ∪ SMALLCAP 250`, `LARGE MID 250 = NIFTY 100 ∪ MIDCAP 150`, `MID SMALL 400 = MIDCAP 150 ∪ SMALLCAP 250`, and the full containment chain 50 ⊂ 100 ⊂ 200 ⊂ 500 ⊂ TOTAL MARKET ⊂ ALLCAP — **0 violations** |
| 12 | P/E is **not** on the fact row | `price_to_earnings` is absent from the export despite being a filter, a column and a factsheet field → it lives in a separate fundamentals join |

---

## 3. The big one: exact window lengths — windows are **calendar-based**, not fixed trading-day counts

`positive_days_percent_N` is a ratio `k/N`, so the denominator can be recovered exactly by
finding the `N` for which every one of the 271 values is an integer multiple of `1/N`.
Only one minimal `N` fits each window:

| Window | Recovered N (trading days) | Confirmation |
|---|---|---|
| 1 month | **22** | observed values step by 4.545% = 1/22 |
| 3 months | **64** | observed values step by 1.5625% = 1/64 — exact |
| 6 months | **121** | 1/121 |
| 9 months | **185** | 1/185 |
| 1 year | **247** | 1/247 |

Critically, **the same N fits every row**, which means every instrument shares the same window
start date. So the implementation is:

```
start_date = as_of − relativedelta(months=K)     # calendar offset
start_date = snap_forward_to_next_trading_day(start_date)
window     = all trading days in [start_date, as_of]
```

…not a fixed 21/63/126/189/252-bar lookback. As of 2026-08-18 those calendar offsets happen to
span 22 / 64 / 121 / 185 / 247 trading days.

> **This supersedes the fixed trading-day constants previously written in `docs/05`.** Implement
> calendar-offset windows and assert the recovered counts against this fixture.

Consequence: an instrument with less than a full window of history must be excluded rather than
computed on a short window — otherwise its denominator would differ and the ratio identity above
would not hold across all rows, which it does.

---

## 4. Storage precision (mirror this exactly)

| Family | Decimals | Implied storage |
|---|---|---|
| open/high/low/close, MAs, highs | 2 | `numeric(18,2)` |
| returns, sharpe, away-from-high, positive-days % | 2 | rounded at write time |
| RSI | 4 | `numeric(10,4)` |
| volatility | 8–10 | `numeric(18,10)`, fraction |
| beta | 10 | `numeric(18,10)` |
| marketcap | 0 | integer ₹ crore |
| volume, volume_shares, median_volume_one_year | 0 | `bigint` ₹ / shares |

The reference product rounds **at write time**, not at render time. Do the same for the columns
above so exports and UI agree byte-for-byte, but keep full precision for volatility and beta
because they feed divisions.

---

## 5. Turn this file into the acceptance test

Add `tests/test_reference_parity.py`:

1. Load `fixtures/reference-screen-export-2026-08-18.csv`.
2. Assert our `factor_daily` row for each of the 271 symbols on 2026-08-18 matches every numeric
   column to the tolerance implied by its stored precision (± half of the last decimal place).
3. Assert the identities in §2 rows 1–3 hold in **our** output too.
4. Assert the index identities in §2 row 11 hold in our `index_member_daily`.
5. Assert our recovered window lengths equal 22 / 64 / 121 / 185 / 247 for as-of 2026-08-18.
6. Assert our CSV export reproduces this file's exact column names, order, quoting and BOM.

If step 2 passes for all 271 rows, the factor engine is provably equivalent to the reference
product. That is the definition of done for Prompt 5.
