# 06a — Screener implementation notes

Companion to `docs/06-screener-semantics.md`, written while building Prompt 6. Same role as
`docs/04a`, `docs/04b` and `docs/13a`: everywhere the implementation departs from the letter of
the spec, or resolves something the spec leaves ambiguous, it is written down here rather than
buried in a commit message.

Implementation: `packages/core/src/decile_core/screener.py` (pure query builder) and
`services/api/src/decile_api/screener.py` (execution, as-of resolution, cache).

---

## 1. `QUALIFY` does not exist in PostgreSQL

docs/06 §"Reference SQL skeleton" writes the `apply_filters_on` bucket as:

```sql
QUALIFY PERCENT_RANK() OVER (ORDER BY f.marketcap_cr DESC) <= :bucket_pct   -- or LIMIT n
```

`QUALIFY` is Snowflake/DuckDB syntax. docs/02 locks PostgreSQL 16, which has no `QUALIFY` and
forbids a window function in the `WHERE` of the select that computes it. The window is therefore
computed in the `bucketed` CTE and filtered in a `selected` CTE immediately after it. Identical
semantics; one more CTE.

`PERCENT_RANK` is kept as written rather than swapped for `CUME_DIST` or a row count. Note the
consequence, which docs/06 does not: `PERCENT_RANK` is `(rank - 1) / (n - 1)`, so the first row
scores 0 and `<= 0.10` over 271 rows keeps 28 rows, not 27. Ties on marketcap are admitted
together. `top_50` / `top_100` use `ROW_NUMBER`, as the skeleton's "or LIMIT n" implies.

## 2. `NULLS LAST` on the marketcap ordering

docs/06 writes `ORDER BY f.marketcap_cr DESC`. PostgreSQL sorts NULLs **first** under `DESC`, so
taken literally every instrument with an unknown marketcap would land in the top decile — the
exact opposite of docs/06 §step 4's "NULLs never satisfy a predicate". The implementation orders
`marketcap_cr DESC NULLS LAST`.

## 3. A tie-breaker inside every `ROW_NUMBER`

docs/06 §"Determinism guarantee" promises byte-identical results for the same definition, `as_of`
and `data_version`. `ROW_NUMBER() OVER (ORDER BY f DESC NULLS LAST)` does not deliver that on its
own: the order *within* a tie is unspecified, so two runs may hand the same two rows opposite
ranks, and the cached JSON differs byte for byte from identical inputs.

Every ranking window therefore orders by `(<factor> <dir> NULLS LAST, instrument_id ASC)`. Ties
still get consecutive integers — docs/06 §step 5's requirement, and what keeps combined sums
comparable — they now get the *same* consecutive integers every time.

## 4. Only active filters are emitted

The skeleton parameterises the on/off switch as well as the value
(`:min_ret_1y IS NULL OR ret_12m >= :min_ret_1y`). The builder emits a clause only when the
filter is active, deciding activity through the `is_active()` predicates on `ScreenDefinition` —
the one place docs/01 §2.4-§2.10's sentinels are interpreted. Values are still bound parameters
and the result is still one statement; the sentinel logic simply is not restated in SQL, and the
planner gets a `WHERE` clause with nothing inert in it.

## 5. `ignore_top_beta.count` cannot be honoured, and is inert

docs/01 §2.10 gives each of "Ignore Top Beta / Volatility" a *count/percentile input*. docs/06
§step 4 ⚠ and docs/13 §2 finding 10 then settle the semantics the other way: the flag is
precomputed per instrument **per universe**, nightly, at `TOP_RISK_FLAG_PERCENTILE = 0.10`, and
the screener tests one bit. A per-request count cannot be served by testing a bit.

The switch is honoured; `count` is accepted, persisted and ignored. Two definitions differing only
in `count` produce identical SQL and identical cache keys. If the reference product's count input
turns out to be live, serving it needs either a windowed exclusion over the whole universe
(computable, but no longer order-independent or index-friendly) or a flag per (universe,
percentile) pair. Neither is worth building against an inference — revisit if a real screen is
ever captured with a count set.

## 6. The captured "Investing 001" screen filters on series EQ **and** BE

`seed_data.EXAMPLE_SCREENS` previously left this screen on the `["EQ"]` default. The export is a
*result set*, and two of its 271 rows are series BE, so the captured screen cannot have been
filtering to EQ alone. Corrected in `seed_data.py`; docs/13 is the arbiter for this screen.

## 7. The export's row order is reproducible only to the rounding granularity

docs/13 §2 finding 3 already records this: the file's order is monotonic in
`mean(sharpe 1y, 6m, 3m, 1m)` except for 48 adjacent inversions, all `<= 0.0075`, "exactly the
rounding granularity of 2-dp inputs".

Recomputing the blend from the file's own 2-dp columns and sorting reproduces those 48 inversions
exactly (with `Decimal`; CLAUDE.md's note of 53 came from float arithmetic and is superseded).
114 of the 271 positions differ from the file's order, and the largest blend gap at any differing
position is exactly 0.0075.

So "the same 271 symbols in the same order" is available in the first half and not in the second,
and no implementation can do better from this file: the reference product ranked on unrounded
values that the export does not carry. `test_screener.py` asserts the set exactly, asserts our own
order is exactly monotonic, and asserts that where our order and the file's differ the two rows
are within 0.0075 of each other — i.e. that every disagreement is accounted for by the rounding
that caused it.

## 8. `ix_factor_daily_date_marketcap_cr` is shadowed by `ix_factor_daily_date`

Prompt 6 asks for an EXPLAIN assertion that the screen uses the `(date, marketcap_cr)` index. It
does not, and the query is not why.

`factor_daily` carries two indexes whose leading column is `date`. Neither can serve an index-only
scan for a screen — the projection needs `instrument_id` and the row itself — so both end in a
heap fetch, and the narrower index is a strictly cheaper way to satisfy `WHERE date = :as_of`.
PostgreSQL picks `ix_factor_daily_date` every time. Dropping it inside a transaction makes the
planner choose `ix_factor_daily_date_marketcap_cr` immediately, which is how we know the composite
is shadowed rather than unusable.

Measured on a production-sized dataset (2,300 instruments × 120 trading days, 276,271
`factor_daily` rows), against a 300 ms budget: the full-universe screen is **8.7 ms** of server
execution time, and the heaviest shape — decile bucketing plus three ranking factors, four window
functions in one statement — is **7.2 ms**. Round-tripped through Python and the ORM the whole
call is 20-50 ms. Both ends of the hot path are index-driven: `ix_factor_daily_date` on the fact
table, `ix_index_member_daily_date_index_id` on the point-in-time membership join.

The redundancy is a Prompt 1 schema question, not a Prompt 6 query question, so it is recorded
here rather than fixed by a migration written to make a test pass. For Prompt 16: either drop
`ix_factor_daily_date` (the composite is a valid prefix index for every query that uses it), or
rebuild the composite as `(date, marketcap_cr) INCLUDE (instrument_id)` so the bucket step can go
index-only. The second is the only one that would make the index earn its keep.

## 8a. The cache key covers the projection as well as the definition

docs/06 §Caching specifies `screen:{sha256(definition_canonical_json)}:{as_of}:{data_version}`.
Superseded in Prompt 9: the digest now covers the resolved column list too.

The cached value is the response *body*, which carries `columns` and one member per column
(docs/07 §"Running a screen"). Two requests can share a definition and differ in columns — that is
exactly what saving a column layout does, and `POST /screens/preview` takes columns as a separate
field — so hashing the definition alone serves the second request the first one's columns. The key
shape is unchanged; `screen_run.definition_hash` still uses the definition-only hash. `docs/09a` §3.

## 9. The universe is resolved from `index_member_daily`, including for the rule-derived universes

docs/06 §step 2 defines `nifty-allcap` and `etf` by rule ("all instruments with
`instrument_type='EQ'` and a bar on `as_of`"), while the skeleton resolves every universe by
joining `index_member_daily`. There is no conflict in practice: `refresh_index_membership`
(Prompt 4) already materialises both by rule, as rows with `source='derived'`. The screener has
one read path for all fourteen universes, and it is the point-in-time one.

The denormalised `universe_mask` is *not* used for the universe filter, even though docs/06
§"Universe flags" offers it as the index-friendly option, because the skeleton is explicit and the
join costs ~2 ms at production size. It is used for the risk flags, which is where docs/06 §step 4
⚠ requires it.

## 10. Volatility stays a fraction in the result payload

docs/07 §"Running a screen" shows an example row with `"vol_12m": 57.93`. Storage is a decimal
fraction (docs/13 §2 finding 4, `0.5793…`), and docs/13 says "the UI multiplies by 100". The
screener projects the stored value unchanged; the ×100 belongs to the presentation layer, and
doing it here would put two different meanings of `vol_12m` into the same system. Flagged for
Prompt 7/8 to resolve at the boundary where it belongs.

## 11. `resolve_as_of` refuses to answer before the first publish — and no longer clamps

docs/06 §step 1: the default as-of is "the latest `date` in `factor_daily` that belongs to a
published `pipeline_run` (**never** a half-written day)". Before any run has published, that set is
empty, and answering from the newest `factor_daily` row anyway would be exactly the half-written
day the document rules out. `resolve_as_of` raises `NoPublishedData` instead.

**Superseded in Prompt 7.** This section originally said a requested date newer than the latest
published day was *clamped* down to it. It is now refused: `AsOfOutOfRange`, which the API maps to
`422 no-trading-day` (docs/07's catalogue, and Prompt 7's acceptance criteria). Clamping is right
for a weekend — docs/06 §step 1 asks for exactly that, and the response says which date was used —
but wrong for a date the service holds no data for, where it turns a client bug into a
plausible-looking answer about a different day. See `docs/07a` §1.
