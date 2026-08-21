# 06 — Screener execution semantics

The order of operations is load-bearing. Getting it wrong changes results silently.

## The pipeline

```
 1. Resolve as-of date
 2. Resolve universe (point-in-time index membership)
 3. Resolve the "apply filters on" bucket
 4. Apply ALL filters (everything except sort_by / sort_direction)
 5. Rank by factor one (and two, and three)
 6. Combine ranks, final ascending sort
 7. Project the requested columns
```

### Step 1 — as-of date

`historical_date` if set, else the latest `date` in `factor_daily` that belongs to a published
`pipeline_run` (never a half-written day). If the requested date is not a trading day, snap
**backwards** to the previous trading day and tell the client which date was actually used
(the UI prints "Results are shown for 19 Aug 2026").

### Universe flags (denormalised)

The reference product denormalises membership onto the fact row: 14 `is_*` booleans plus 14
`is_*_top_beta` and 14 `is_*_top_volatility` booleans. We keep `index_member_daily` as the
normalised, point-in-time source of truth **and** materialise these 42 flags onto `factor_daily`
during the nightly build, because they turn the universe filter and the risk-exclusion filter
into index-friendly boolean predicates. The nightly job asserts the two representations agree.

Free data-quality assertions, verified as exact in the reference export (0 violations):

```
NIFTY 500        = NIFTY 100 ∪ MIDCAP 150 ∪ SMALLCAP 250
NIFTY LARGE MID 250 = NIFTY 100 ∪ MIDCAP 150
NIFTY MID SMALL 400 = MIDCAP 150 ∪ SMALLCAP 250
NIFTY 50 ⊂ NIFTY 100 ⊂ NIFTY 200 ⊂ NIFTY 500 ⊂ NIFTY TOTAL MARKET ⊂ ALLCAP
NIFTY NEXT 50 ⊂ NIFTY 100
NIFTY MICROCAP 250 ⊂ NIFTY TOTAL MARKET
```

### Step 2 — universe

```sql
SELECT instrument_id FROM index_member_daily
WHERE index_id = :index_id AND date = :as_of
```

Special universes: `nifty-allcap` = all instruments with `instrument_type='EQ'` and a bar on
`as_of`; `etf` = `instrument_type='ETF'`. Never resolve a historical universe from today's
membership — that is the single most common source of backtest look-ahead bias.

### Step 3 — `apply_filters_on`

Rank the universe by **marketcap descending**, then take:

| Value | Selection |
|---|---|
| `all` | everything |
| `decile_1` | top 10% |
| `decile_2` | top 20% |
| `decile_3` | top 30% |
| `decile_4` | top 40% |
| `decile_5` | top 50% |
| `top_50` | first 50 rows |
| `top_100` | first 100 rows |

> INFERRED: the reference product does not state the ranking key for its deciles. Marketcap is
> the only key that makes "top decile of the index" mean what users expect. Make the key a
> config constant (`DECILE_RANK_KEY = "marketcap_cr"`) so it can be changed in one place, and
> surface it in the UI help text.

### Step 4 — filters (all AND-combined)

| Filter | Predicate | Disabled when |
|---|---|---|
| min 1y return | `ret_12m >= X` | X is NULL |
| median volume | `median_vol_12m >= X` | X is NULL |
| MA above/below | `close > ma_K` / `close < ma_K` per enabled switch | group switch off |
| away from ATH | `abs(away_high_ath) <= X` | `X = 100` |
| away from 1Y high | `abs(away_high_1y) <= X` | `X = 100` |
| positive days (5) | `pos_days_N >= X` | `X = 0` |
| circuits (5) | `circuits_N <= X` | `X > 250` |
| marketcap range | `marketcap_cr BETWEEN a AND b` | both NULL |
| P/E range | `pe BETWEEN a AND b AND pe IS NOT NULL` | switch off |
| series | `series = ANY(:series)` | empty list |
| ignore above beta | `beta_12m <= X` | `X = 100` |
| price range | `close_raw BETWEEN a AND b` | both NULL |
| custom filters ×3 | `<left_expr> <op> <right_expr>` | slot off |
| ignore top beta | `NOT is_{universe}_top_beta` | switch off |
| ignore top volatility | `NOT is_{universe}_top_volatility` | switch off |

Two subtleties:

- ⚠️ **Corrected by the CSV export.** "Ignore Top Beta / Volatility" is **not** a relative filter
  over the surviving rows. The reference product stores a precomputed boolean per instrument
  **per universe** (`is_nifty_500_top_beta`, `is_nifty_total_market_top_volatility`, …). Proof:
  within every universe in the export, the minimum beta among flagged rows strictly exceeds the
  maximum beta among unflagged rows — a clean rank threshold that is impossible if the cut were
  computed after the other filters had removed rows. So the cut is taken over the **whole
  universe**, nightly, and the screener just tests a boolean.
  Threshold: **top decile by beta / by 1-year volatility within each universe** (the reading most
  consistent with the observed flag shares). Make it a named constant
  (`TOP_RISK_FLAG_PERCENTILE = 0.10`) so it can be recalibrated against the fixture.
  This makes the filter cheaper *and* order-independent.
- NULLs never satisfy a predicate. A stock listed 3 months ago has `ret_12m = NULL` and is
  therefore excluded by any 1-year filter. That is correct and must be documented in the UI,
  because users will ask why a hot new listing is missing.

### Step 5–6 — ranking and multi-factor combination

For each enabled factor `f_i` with its own direction `d_i`:

```sql
rank_i = ROW_NUMBER() OVER (ORDER BY f_i <d_i> NULLS LAST)
```

Then:

```sql
combined = rank_1 + rank_2 + rank_3
ORDER BY combined ASC, rank_1 ASC          -- rank_1 as the tie-breaker
```

Note the direction is **per factor**: a user can rank by "highest 12-month Sharpe" *and*
"lowest volatility" simultaneously. Ties within a single factor get consecutive integers
(`ROW_NUMBER`, not `RANK`) so that combined sums stay comparable — this matches the reference
product's description of "the sum of all these ranks".

The `Sorting Factor` column always displays the value of **factor one**.

### Step 7 — projection

Default columns (see `01-product-teardown.md §4`) plus any of the 34 optional columns the user
saved on the screen.

## Reference SQL skeleton

```sql
WITH universe AS (
  SELECT m.instrument_id
  FROM index_member_daily m
  WHERE m.index_id = :index_id AND m.date = :as_of
),
bucketed AS (
  SELECT f.*
  FROM factor_daily f
  JOIN universe u USING (instrument_id)
  WHERE f.date = :as_of
  QUALIFY PERCENT_RANK() OVER (ORDER BY f.marketcap_cr DESC) <= :bucket_pct   -- or LIMIT n
),
filtered AS (
  SELECT * FROM bucketed
  WHERE (:min_ret_1y IS NULL OR ret_12m >= :min_ret_1y)
    AND (:median_vol IS NULL OR median_vol_12m >= :median_vol)
    AND (NOT :ma_above_200 OR close > ma_200)
    -- … one clause per filter, all parameterised …
),
relative AS (           -- "ignore top beta / volatility": precomputed per-universe flags
  SELECT * FROM filtered
  WHERE (NOT :ignore_top_beta OR NOT top_beta_flag)
    AND (NOT :ignore_top_vol  OR NOT top_vol_flag)
),   -- top_beta_flag / top_vol_flag are selected for the CURRENT universe (see §Universe flags)
ranked AS (
  SELECT *,
    ROW_NUMBER() OVER (ORDER BY (<factor1_expr>) DESC NULLS LAST) AS r1,
    ROW_NUMBER() OVER (ORDER BY (<factor2_expr>) ASC  NULLS LAST) AS r2,
    0 AS r3
  FROM relative
)
SELECT *, (r1 + r2 + r3) AS combined_rank
FROM ranked
ORDER BY combined_rank ASC, r1 ASC;
```

`<factorN_expr>` is produced by a **whitelisted** factor registry that maps a factor key to a
SQL expression — never string-interpolated from user input. Blends are expressions such as
`(sharpe_12m + sharpe_6m + sharpe_3m + sharpe_1m) / 4.0`.

## The factor registry

One Python dict (mirrored to TS via generated constants) with, per factor:
`key`, `label`, `family`, `sql_expr`, `unit`, `higher_is_better`, `null_policy`.
The registry is the single source of truth for: the `sort_by` dropdown, the custom-filter
operand list, the column picker, the API enum, and the backtest signal list.

## Caching

Key: `screen:{sha256(definition_canonical_json)}:{as_of}:{data_version}`.
TTL: until the next `data_version` bump. Invalidate the whole namespace on publish.
Warm the top 200 most-run screen definitions after each publish.

## Determinism guarantee

Given the same `data_version`, the same definition and the same `as_of`, results are
byte-identical. `screen_run` stores `definition_hash` so a user can prove what they saw.
