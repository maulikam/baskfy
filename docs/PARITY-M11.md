# M11 — reference parity, measured

> **STATUS UPDATE, 31 Aug 2026 — read before the sections below.**
> Causes **B (volatility ddof)** and **C (RSI Cutler@N−1)** were authorised by Maulik and
> **landed as M65**. Sections §4B, §4C and §8 still describe them as "NOT fixed — a spec
> decision is required"; that wording is stale and is left in place only as the record of what
> was weighed. Parity went **4,514 → 3,168 of 9,186**.
>
> Cause **E (the 2026-02-01 Budget special session)** was the single largest remaining cause and
> is **now fixed at the data level**: NSE published a full 3,229-row bhavcopy for that Sunday and
> only 322 bars had been ingested — 301 of them SME bars written earlier the same night, and just
> 21 main-board bars from Kite's deep-history pass, against 2,310 on 30 Jan. Re-running the
> bhavcopy backfill for that one date took it to **2,304**. The calendar was never wrong here
> (unlike 28 Aug / M62); this was a silent partial ingest.
>
> Remaining 3,168 breaks down as ~1,355 `volatility_*` sitting on a tolerance floor that cannot
> be reached (5e-11 against a ~1.6e-7 residual from 2-dp stored closes), the 9M/12M residual that
> cause E gated and should now recede, 271 from cause D (`MEDIAN_VOL_BARS` 252 → 247, not landed),
> and the rest cause F (five symbols with unadjusted corporate actions).


**Measured at git SHA `7d6b7fb3aa6d23dadb30d645d28884a8b7eb6d84`** (branch `developer`),
as-of date **2026-08-18**, against the 271-row reference export
`decile-blueprint/fixtures/reference-screen-export-2026-08-18.csv`.

> **The headline the status page got wrong.** `docs/00-merge-status.md` carries "6,934/9,166
> cells fail" dated 22 Aug. That number had **never been executed**: the reproduction test
> skips unless `BASKFY_PARITY_BARS` is set, and a skipped test is never run. It is also
> **not a window-length failure** in the way the leaf title assumed. Window length is the
> largest single cause — worth 1,882 cells — but it is under half of it. There are five
> further independent causes, two of which are data, not code.

---

## 1. How the inputs were obtained

The suite makes no network calls by design (Prompt 2 acceptance criterion 1), and the bundle
carries no price history, so the reproduction needs bars supplied from outside.

| Input | Source | Status |
|---|---|---|
| Adjusted daily bars, 271 symbols, 2023-01-02 → 2026-08-18 (223,997 rows) | `ohlcv_daily` on the staging box via `AWS_PROFILE=baskfy-poc bash tools/deploy/box.sh`, exported to Parquet | **supplied** |
| Exchange trading calendar | `select distinct date from ohlcv_daily` on the same box — the *full* instrument set, not the 271 | **supplied** |
| NIFTY 50 benchmark (`BASKFY_PARITY_BENCHMARK`) | not assembled | **absent** → `beta` reported unchecked |
| Listing-date history (`BASKFY_PARITY_BARS_ARE_FULL_HISTORY`) | bars start 2023-01-02, not at listing | **absent** → `high_all_time`, `away_from_high_all_time` unchecked |
| `marketcap_cr` | no step in docs/03 fetches fundamentals | **absent** → `marketcap` unchecked |

Reproduce with:

```
cd decile-blueprint
BASKFY_PARITY_BARS=<bars.parquet> uv run pytest packages/core/tests/test_reference_parity.py -q
```

The bars Parquet used is at
`<scratchpad>/m11/bars.parquet`; the SQL that produced it is `<scratchpad>/m11/q.sql`.
It is **not committed** — it is 6 MB of licensed market data, and the pre-flight test
`test_the_supplied_bars_agree_with_the_export_on_the_as_of_day` passes against it, which is the
check that the input is the right thing before any factor is believed.

## 2. The measured numbers

| | cells failed | of compared |
|---|---:|---:|
| before (`bisect_left`) | **6,396** | 9,186 |
| after (`bisect_right`) | **4,514** | 9,186 |

Nine columns are *reported unchecked* rather than compared (five `circuits_*` with an unresolved
definition, plus `beta` / `marketcap` / `high_all_time` / `away_from_high_all_time` awaiting the
inputs above). No tolerance was widened and no column was dropped.

## 3. Characterisation — which factors, which windows, and what shape the error is

Failures per column, out of 271 rows each.

| column family | 1M | 3M | 6M | 9M | 12M | shape of the error | cause |
|---|---:|---:|---:|---:|---:|---|---|
| `absolute_return_*` before | 4 | **271** | **270** | **270** | **270** | off-by-one **period** — wrong base bar | A |
| `absolute_return_*` after | 4 | 7 | 8 | 9 | 14 | residual only | E, F |
| `positive_days_percent_*` before | 4 | **271** | **271** | 161 | 147 | off-by-one **period** — wrong denominator `N` | A, E |
| `positive_days_percent_*` after | 4 | 9 | 14 | **271** | **271** | 9M/12M only | E |
| `volatility_*` before & after | **271** | **271** | **271** | **271** | **271** | **exactly proportional**, ratio `√((N−1)/N)` | B |
| `rsi_*` before & after | 270 | **271** | **271** | **271** | **271** | wrong smoothing family *and* off-by-one period | C |
| `sharpe_return_*` after | 143 | 133 | 99 | 112 | 122 | inherited from `volatility_*` | B |
| `ma_20 / 50 / 100 / 200` after | 21 / 6 / 9 / **268** | | | | | `ma_200` only — a **constant one-bar** window shift | E |
| `median_volume_one_year` after | **271** | | | | | wrong **bar count** (252 vs 247) | D |
| `high_one_year`, `away_from_high_one_year` | 5 | | | | | five named symbols only | F |

Read the shapes: **A** is a clean off-by-N-periods (N = 1); **B** is a clean proportional
factor; **C** is neither (a different formula); **D** is a clean bar-count offset; **E** and
**F** are data, not arithmetic.

---

## 4. Root causes

### A. The window included the anniversary day — `packages/core/src/baskfy_core/windows.py:177`

```python
index = bisect.bisect_left(ordered, calendar_start)   # was
index = bisect.bisect_right(ordered, calendar_start)  # is
```

`docs/05` §Notation said `start = snap_forward_to_trading_day(as_of − relativedelta(months=K))`
with a **closed** interval `[start, as_of]` — the first trading day *on or after* the
anniversary. `bisect_left` implements that faithfully. It is wrong, and the same §Notation
contradicted itself: the table of recovered window lengths four lines below the formula does not
follow from the formula.

Counted directly against the exchange calendar in `ohlcv_daily` (all instruments, so no
extraction gap):

| K | anniversary | trading day? | `(anniversary, as_of]` | `[anniversary, as_of]` | recovered `N` |
|---|---|---|---:|---:|---:|
| 1M | 2026-07-18 | **no** (Sat) | **22** | 22 | 22 |
| 3M | 2026-05-18 | yes (Mon) | **64** | 65 | 64 |
| 6M | 2026-02-18 | yes (Wed) | **121** | 122 | 121 |
| 9M | 2025-11-18 | yes (Tue) | **185** | 186 | 185 |
| 12M | 2025-08-18 | yes (Mon) | **247** | 248 | 247 |

The half-open interval reproduces all five recovered lengths; the closed one reproduces one.
The `EXPECTED_WINDOW_LENGTHS_2026_08_18` constant was **right all along** and the code
contradicted it.

Independently corroborated from the export itself: recovering the return base bar by exhaustive
search over candidate dates gives, for `absolute_return_{1M,3M,6M,9M,12M}`, the base
2026-07-20 / 2026-05-19 / 2026-02-19 / 2025-11-19 / 2025-08-19 — the first trading day
**strictly after** the anniversary in every case, at 268/265/268/267/260 exact matches out of
~268, against ≤4 for every neighbouring date.

**1M is the tell.** 2026-07-18 was a Saturday, so `bisect_left` and `bisect_right` agree there
and nowhere else. That is exactly why every `*_one_month` column was the single family that
already reproduced, and it is the fingerprint that identified the bug.

**Fixed.** `docs/05` §Notation corrected in the same change; the module docstring, the
`FactorWindow.start` docstring, the inline comment and the error message all moved with it.

### B. Volatility uses the sample standard deviation; the reference uses the population one — `packages/core/src/baskfy_core/factors.py:303`

```python
pl.col("daily_return").rolling_std(window_size=n, min_samples=n, ddof=1)
```

The ratio `published / computed` is **constant across all 271 rows to seven significant
figures**, and the constant is `√((N−1)/N)`:

| window | measured ratio (p05 / median / p95) | `√((N−1)/N)` |
|---|---|---|
| 1M (N=22) | 0.977006 / **0.977008** / 0.977010 | **0.977008421** |
| 3M (N=64) | 0.992156 / **0.992157** / 0.992158 | **0.992156742** |

A p05-to-p95 spread of 4×10⁻⁶ over 271 independent instruments is not a coincidence: it is
`ddof`. Sweeping the alternatives confirms it — with `ddof=0`, `m=N` returns and √252
annualisation the median absolute error is **1.6×10⁻⁷**, against 6×10⁻³ for `m=N−1` and
1.3×10⁻³ for a √250 annualisation. That is a factor of 40,000.

**NOT fixed — a spec decision is required.** `docs/05` §2 says "sample stdev (ddof=1)" in
words. Changing it is a spec amendment, it moves `sharpe_return_*` with it, and
`volatility_one_year` is the input to the desk's GTT stop sizing (§6 below). This is one line,
it is proven, and it should be its own leaf with its own fixture regeneration — not a quiet
edit inside a window-length leaf.

**A second, separate problem sits behind it.** Even with `ddof=0` the residual is ~1.6×10⁻⁷,
while `COLUMN_TOLERANCE["volatility_*"]` is `5×10⁻¹¹`. The export stores volatility to 8
decimals, so the tolerance the stored precision implies is `5×10⁻⁹` — and our `close` is the
adjusted price **rounded to 2 dp at write time** (house rule 8), which injects ~10⁻⁵ relative
noise per return and lands at ~10⁻⁷ in annualised volatility. **`volatility_*` cannot reproduce
to 5×10⁻¹¹ from stored data at any ddof.** That is a tolerance defect, not an engine defect.
It is recorded here rather than edited, because loosening a tolerance to make a column pass is
precisely what must not be done silently. `sharpe_return_*` (tolerance 5×10⁻³) is unaffected
and will pass once `ddof` is settled.

### C. RSI is Wilder's at period N; the reference uses Cutler's at period N−1 — `packages/core/src/baskfy_core/factors.py:426,467`

Sweeping method × period against the export:

| column | Wilder, best period | Cutler, best period |
|---|---|---|
| `rsi_one_month` (N=22) | p=22 → **1/271** exact | **p=21 → 267/271** exact |
| `rsi_three_months` (N=64) | p=67 → **0/270** exact | **p=63 → 263/270** exact |

Two findings in one. The smoothing family is wrong — the reference uses a simple moving average
of gains and losses (Cutler's RSI), not Wilder's recursive smoothing — and the period is the
number of **returns inside** the window, `N−1`, not the number of bars, `N`.

`N−1` is internally consistent with §A: a window of `N` bars spans `N−1` returns, which is the
same `N−1` that `ret_N = P_t / P_{t−(N−1)} − 1` already uses. Note the reference is *not* fully
self-consistent — `positive_days_percent` is `k/N` over `N` returns, one of which reaches back
before the window. That inconsistency is the reference product's, and reproducing it is the job.

Cutler's is also **path-independent**, which matters: Wilder's is seeded from the first `N`
observations of whatever history it is handed, so it can never reproduce without listing-date
history. Cutler's removes that dependency entirely.

**NOT fixed — a spec decision is required.** `docs/05` §5 says "Wilder's RSI" explicitly and
gives its recursion. `rsi_one_month` drives the desk's RSI bands (>78 wait or tranche, >82 trim)
and the F-penalty, so this is a live strategy input. Its own leaf.

### D. The 1-year median volume is a 252-bar count where the reference uses the 12-month window — `packages/core/src/baskfy_core/windows.py:86`, used at `factors.py:419`

`MEDIAN_VOL_BARS = 252`, per `docs/05` §13's `median(vol_day_val over 252 bars)`. Measured:

| field | bars | exact matches |
|---|---|---|
| `turnover` | 252 | **0/267** |
| `turnover` | **247** | **119/267** |
| `close × volume_raw` | any | 0/270 |

The field is right (exchange turnover, per §13's own correction). The **length** is wrong: it is
the 12-month calendar window, `247`, not the round number `252`. 119/267 rather than ~267/267 is
the expected signature of cause **E** below — our extract is one session short, so our "last 247
bars" is the reference's set with one element swapped, and a median over 247 values survives a
single swap about half the time.

This is the same class of bug as §A: a hard-coded bar count standing in for a calendar window.
**NOT fixed** — it is one more line, but it needs cause E resolved first to confirm 247 at full
precision, and it is a `docs/05` §13 amendment.

### E. A real NSE session is missing from our data — 2026-02-01

**Not a code defect.** The Union Budget session of **Sunday 1 February 2026** exists in
`ohlcv_daily` for only **322 instruments** against ~2,300 on an ordinary day, and for **none of
the 271** in the reference universe:

```
2026-01-30 | 2310
2026-02-01 |  322     <-- Budget special live session, Sunday
2026-02-02 | 2304
```

By segment those 322 are 282 `SM` (SME), 19 `ST`, **18 `EQ`**, 3 `BE`. The mainboard EQ leg of
that session was never ingested.

Three independent lines of evidence pin it, and they were derived *before* the database was
queried:

1. **Window arithmetic.** With §A fixed, our 271-symbol calendar gives 9M = 184 and 12M = 246,
   against the recovered 185 and 247 — short by exactly one, and only for the two windows that
   reach back past February. `select count(distinct date) ... between '2025-08-19' and
   '2026-08-18'` on the full instrument set returns **247**, which is the constant.
2. **Direction.** `k_published − k_ours` for `positive_days_percent` is in `{0, +1}` for
   262/270 rows at 9M and 254/269 at 12M, and **never systematically negative** — the reference
   has one *more* observation than we do, not one fewer.
3. **Location.** `ma_100` fails 9/271 but `ma_200` fails **268/271**. A hundred bars back from
   2026-08-18 is late March 2026; two hundred is late October 2025. The missing session must lie
   between them. Solving `ma_200` for the implied missing close and matching it against
   neighbouring prices puts it at the end of January 2026.

**Impact:** ~813 cells — `positive_days_percent_{nine_months,one_year}` (542) and `ma_200`
(268). Fixing it is a backfill of one session for the mainboard, not a core change, and it is
outside this leaf's write scope. **Handed off.**

### F. Five symbols carry an unadjusted corporate action

Already on the record as `NEEDS-MAULIK.md` item 4. Accounts for the residual 4–14 cells per
column in `absolute_return_*` after the fix, and for all 5 failures in `high_one_year` /
`away_from_high_one_year`. Not this leaf's.

---

## 5. The fix that landed

| file | change |
|---|---|
| `decile-blueprint/packages/core/src/baskfy_core/windows.py` | `bisect_left` → `bisect_right` at line 177; module docstring, `FactorWindow.calendar_start`/`.start` docstrings, inline comment and the `ValueError` message all rewritten to match |
| `decile-blueprint/docs/05-factor-formulas.md` | §Notation corrected from the closed interval to the half-open one, with the measured table |
| `decile-blueprint/packages/core/tests/fixtures/momentum_scan.csv` | golden regenerated — see §7 |

**6,396 → 4,514 cells** (9,186 compared). Full suite: **2,747 passed, 1,138 skipped, 0 failed**
(`uv run pytest packages/core services`). `uv run ruff check packages/core/src/baskfy_core/windows.py`
clean. (The repository carries 50 pre-existing ruff errors at this SHA from sibling in-flight
work in `gateway.py` and `broker_oauth.py`; none are in files this leaf touched.)

**House rule 5 (no look-ahead) is not violated.** The change makes the window *strictly
smaller* — it removes the anniversary day from `[anniversary, as_of]`, leaving
`(anniversary, as_of]`. Every bar the corrected window reads is a bar the previous one also
read, and the window's right edge is untouched at `as_of`. It is impossible for a change that
only ever discards the oldest bar to introduce look-ahead. No factor gained access to any date
it could not previously see, and no point-in-time membership or factor row was re-dated.

## 6. Does M11's failure change which orders get generated?

**This is the question the parent branch needs, so it was measured rather than reasoned about.**
The same 271 instruments were scored three ways through `baskfy_core.score.score()` with the
desk's own `app/config.py` constants (`MOMENTUM_BLEND` 10/30/30/15/15, `SHARPE_BLEND`
35/35/15/15, `MIN_MEDIAN_DAILY_VALUE` ₹5 cr, `MAX_AWAY_FROM_HIGH` −30, `STOP_MIN/MAX/VOL_MULT`
0.08/0.12/2.2):

* **REF** — the reference export's published factor values (ground truth)
* **PRE** — our engine, `bisect_left`
* **POST** — our engine, `bisect_right`

`marketcap`, `beta`, `series`, `is_nifty_fno` and the `circuits_*` columns were carried from the
export in all three, so the comparison isolates the factor engine.

| measure | PRE vs REF | POST vs REF |
|---|---|---|
| instruments passing the **hard eligibility filters** | 239/271 — **0 disagreements** | 239/271 — **0 disagreements** |
| **top-15 basket** (the desk's operating size) | **15/15 identical** | **15/15 identical** |
| **top-12 basket** | 11/12 | **12/12** |
| top-20 basket | 19/20 | 19/20 |
| \|Δ score\| among the eligible | median 0.10, p90 0.50 | median 0.10, p90 **0.20** |
| \|Δ rank\| | median 1, p90 4 | median 1, p90 **3** |
| exact rank agreement | 42/239 | **72/239** |

**The answer, precisely.**

1. **No name is wrongly bought or wrongly rejected at 15 positions.** The top-15 basket is
   byte-for-byte the same list in all three: AEGISLOG, AETHER, APARINDS, ATHERENERG, CUPID,
   HSCL, LAURUSLABS, RADICO, RRKABEL, SANSERA, SHILPAMED, SKYGOLD, SONACOMS, SYRMA, WELCORP.
   Not one hard filter fires differently on any of the 271.
2. **At 12 positions, M11-as-was did change one order.** PRE bought **AETHER** where the
   reference bought **HSCL**; POST matches the reference exactly. The desk runs 12–15 positions,
   so this was a live defect at the bottom of the book, not a theoretical one — and the fix
   removes it.
3. **Weights move, names do not.** Rank order differs on most of the book by a place or two,
   which changes position sizing at the margin. The fix roughly halves that (exact rank
   agreement 42 → 72 of 239, p90 rank error 4 → 3).
4. **GTT stops move by rounding, not by band.** Stop price is
   `clamp(ann_vol/√52 × 2.2, 8%, 12%)`, and `baskfy_core.score.stop_from_vol` implements the
   spec **exactly** (`np.clip(ann_vol/np.sqrt(52) * 2.2, 0.08, 0.12)`, with
   `STOP_MIN, STOP_MAX, STOP_VOL_MULT = 0.08, 0.12, 2.2` at `app/config.py:179`) — no
   implementation/spec divergence. Stops differ from the reference on 133/269 symbols, but on
   the top-15 the differences are ₹0.1–₹1.5 on prices of ₹700–₹4,300, i.e. **0.01–0.07%**, and
   no name crosses the 8% or 12% clamp. This gap is driven by cause **B**, not by the window,
   and is unchanged by this fix.

**Verdict for the parent: M11 does not invalidate execution.** The basket the desk would trade
at its stated size is the correct one both before and after. What M11 was corrupting is
**ordering and sizing at the margin, and the 12th position specifically** — real, worth fixing,
now fixed for the window component. Causes B, C and D remain and should be closed before parity
is claimed, but none of them changes an eligibility decision either: they were measured through
the same scoring path above and moved no name in or out of the top 15.

## 7. The regenerated golden, argued rather than edited

`packages/core/tests/test_momentum_scan.py::test_the_bytes_are_stable` asserts byte-identity
against a committed CSV, with the stated rationale: "If this fails, either the engine changed a
number or a column moved. Both are worth a human reading the diff, which is why the fixture is
committed rather than regenerated in-test." The engine changed a number, deliberately, so the
diff was read before the golden was rewritten. Pre-change copy kept at
`<scratchpad>/m11/momentum_scan.csv.pre`.

The diff is the argument. On the fixture's five synthetic symbols, **every calendar-window
column moved and no bar-count column did**:

* moved: `rsi_one_month`, `volatility_one_year`, `positive_days_percent_{three,six}_months`, all
  five `absolute_return_*`, all five `sharpe_return_*`
* **unchanged, byte for byte**: `close`, `marketcap`, `median_volume_one_year`, `ma_20`, `ma_50`,
  `ma_100`, `ma_200`, `away_from_high_one_year`, `beta`, `circuits_*`, `series`, `date`

That partition is not something a regeneration can fake. It is precisely the set of columns
`resolve_window` feeds and precisely the complement it does not, which is independent
confirmation that the change is scoped to what it claims and has no collateral reach.

**No test was weakened.** The assertion is still exact byte-identity — the same predicate, at
the same strictness, against the corrected engine. No tolerance anywhere was widened, no column
was moved into an exclusion list, no `skipif` was added, and the two `volatility_*` and
`median_volume_one_year` tolerance problems found in §4 were *written down here* rather than
adjusted. `test_reference_parity.py` was not modified at all.

## 8. What is still open, in priority order

| # | item | owner | cells | decision required |
|---|---|---|---:|---|
| B | `volatility_*` `ddof=1` → `ddof=0` | core leaf | ~1,355 + ~609 sharpe | amend `docs/05` §2; regenerate golden; touches GTT stop sizing |
| C | `rsi_*` Wilder@N → Cutler@N−1 | core leaf | ~1,354 | amend `docs/05` §5; live strategy input (RSI bands, F-penalty) |
| E | backfill the 2026-02-01 Budget session for mainboard EQ | data/ingestion | ~813 | none — it is a gap, not a judgement |
| D | `MEDIAN_VOL_BARS` 252 → the 12M window (247) | core leaf | 271 | amend `docs/05` §13; confirm at full precision after E |
| — | `COLUMN_TOLERANCE["volatility_*"]` is 5×10⁻¹¹ against 8-dp stored data and 2-dp stored closes | test-design | — | unreachable as written; set to what the stored precision implies, or store closes at higher precision |
| F | five symbols with unadjusted corporate actions | `NEEDS-MAULIK.md` item 4 | ~5–21/column | already tracked |
| — | `beta`, `marketcap`, `high_all_time`, `away_from_high_all_time` | harness inputs | 4 × 271 | supply benchmark series, `marketcap_cr`, listing-date history |

Closing B, C, D and E would take the remaining 4,514 to an estimated **~120 cells**, essentially
all of them cause F.
