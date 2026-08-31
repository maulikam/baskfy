# Gates: 2.1 Spec amendment — RSI Cutler@N-1 and volatility ddof=0

Scope: Maulik decided 2026-08-31 to match the reference on both. Land them TOGETHER as one
amendment with one fixture regeneration, so GTT stop prices move once rather than twice.

Evidence they are right (leaf 1.4.1, docs/PARITY-M11.md §4):
- RSI: reference is Cutler's at period N-1, 267/271 exact vs 1/271 for Wilder@N. factors.py:426,467
- volatility: reference is ddof=0; published/computed is constant at sqrt((N-1)/N) to 7 sig figs
  (p05->p95 spread 4e-6 across 271 instruments). factors.py:303
Both contradict explicit docs/05 prose, which is why 1.4.1 did not land them unilaterally.

---

## How everything below was measured

Bars: leaf 1.4.1's export still existed and was reused rather than rebuilt — 223,997 rows of
adjusted daily closes for the 271 reference symbols, 2023-01-02 → 2026-08-18, at
`<scratchpad>/m11/bars.parquet` (SQL at `<scratchpad>/m11/q.sql`). The harness pre-flight
`test_the_supplied_bars_agree_with_the_export_on_the_as_of_day` passes against it, which is the
check that the input is the right thing before any factor is believed. Not committed: 6 MB of
licensed market data.

Four variants were measured, so the two amendments' effects are **distinguishable rather than
merged into one number**. The file on disk is the landed one in every run; the two pre-amendment
behaviours are re-created by monkeypatching `factors._cutler_rsi` and `factors._VOL_DDOF`, so
nothing is measured against a working copy that will not ship.

| tag | RSI | volatility |
|---|---|---|
| **BASE** | Wilder@N | `ddof=1` | ← the 1.4.1 baseline, 4,514 |
| **RSI** | Cutler@N−1 | `ddof=1` |
| **VOL** | Wilder@N | `ddof=0` |
| **BOTH** | Cutler@N−1 | `ddof=0` | ← what is landed |

Scripts: `<scratchpad>/l21/{measure,volmag,impact21,bands,causef,gen_scan}.py`.

---

- [x] G1: docs/05 is amended for BOTH, each saying what changed, why, and on whose authority
  CHECK: grep -ci "cutler" decile-blueprint/docs/05-factor-formulas.md
  EXPECT: /[1-9]/
  EVIDENCE: `grep -ci cutler` → **6**. Both sections are retitled with the amendment in the
  heading, so no reader can pass over it:

  * **§2 "Volatility — annualised, per window — AMENDED 2026-08-31 (`ddof=1` → `ddof=0`)".**
    Formula line changed from `(sample stdev, ddof=1)` to `(POPULATION stdev, ddof=0)`. Carries
    the measured ratio table (1M 0.977008 vs `√(21/22)` = 0.977008421; 3M 0.992157 vs
    `√(63/64)` = 0.992156742), the sweep result (`ddof=0` median abs error 1.6e-7 against 6e-3
    for `m=N−1` and 1.3e-3 for √250 — a factor of 40,000), **why the prose was not simply
    wrong** (it was explicit and the code implemented it faithfully; the reproduction test that
    would have caught it needed price history the repository does not carry, so it skipped, and a
    skipped test is never run), the authority (*"Maulik took the decision on 2026-08-31, in
    answer to a question that named the consequence — `vol_12m` is the input to the desk's GTT
    stop sizing (non-negotiable #4), so amending it moves live stop prices"*), and the note that
    it was landed with §5 in one change so stops move once rather than twice in a week.
    It also records the residual that this **cannot** close — see G3.
  * **§5 "RSI over a window — AMENDED 2026-08-31 (Wilder@N → Cutler@N−1)"**. Formula replaced
    with the simple-mean form, `avg_gain = mean(gain_{t-(N-1)+1 … t})`. Carries the method×period
    sweep table, the argument that `N−1` is internally consistent with §Notation and §1 (an
    `N`-bar window spans `N−1` returns — the same `N−1` `ret_N` already uses), the note that the
    reference is *not* itself self-consistent (§11's `positive_days_percent` is `k/N` over `N`
    returns) and that reproducing the reference product is the job, **path-independence as the
    second and independent reason** to prefer Cutler's, an explicit *"Consequences of N−1"*
    paragraph for the degenerate `N = 1` window (now NULL, was 100 or 0), and the authority
    (*"a deliberate spec amendment, taken by Maulik on 2026-08-31 with the consequence named:
    `rsi_1m` is a live strategy input — the desk's RSI bands (>78 wait or tranche, >82 trim a
    held position to a runner) and the F-penalty read it"*).

  The engine carries the same two arguments at the point of change: `factors._VOL_DDOF`'s
  docstring and `_with_rsi`'s, so a reader who never opens docs/05 still meets them.

- [x] G2: RSI switched; the reference-parity cell count for rsi_* measured before and after
  EVIDENCE: `factors.py` — `_wilder_rsi` (a Python loop over time carrying a seeded recursion)
  replaced by `_cutler_rsi` (a rolling simple mean written as a difference of cumulative sums,
  one pass over the whole `(time, instrument)` matrix), called with `window.length - 1`.

  **`rsi_*`: 1,354 → 402 of 1,355 cells (−952, 70% of the family).** Isolating it (variant RSI,
  volatility left at `ddof=1`) gives the same 402, so the whole of the RSI gain is the RSI
  amendment and none of it is borrowed from volatility.

  | column | BASE | BOTH |
  |---|---:|---:|
  | `rsi_one_month` | 270/271 | **4/271** |
  | `rsi_three_months` | 271/271 | **8/271** |
  | `rsi_six_months` | 271/271 | **10/271** |
  | `rsi_nine_months` | 271/271 | 189/271 |
  | `rsi_one_year` | 271/271 | 191/271 |

  1M/3M/6M land where 1.4.1's sweep predicted (267/271 and 263/270 exact). **The 9M and 12M
  residual is not the formula** — they are the only two windows that reach back past
  2026-02-01, the Budget-session gap that docs/PARITY-M11.md §4E pins as missing from our
  ingestion for the mainboard EQ leg. Every window that does not cross that date reproduces at
  96–99%. Cause E is handed off and outside this leaf's write scope.

- [x] G3: volatility switched; cell count for volatility_* and sharpe_* before and after
  EVIDENCE: `rolling_std(..., ddof=1)` → `ddof=_VOL_DDOF`, a named constant carrying the
  measurement and the authority.

  **`sharpe_return_*`: 609 → 215 (−394, 65% of the family).** Isolating it (variant VOL, RSI left
  at Wilder@N) gives the same 215 — the whole sharpe gain is the volatility amendment.
  Per column: 1M 143→4, 3M 133→7, 6M 99→8, 9M 112→88, 1Y 122→108. Again the residual is
  concentrated in the two windows that cross the 2026-02-01 gap.

  **`volatility_*`: 1,355 → 1,355. Unchanged, and it cannot change.** This was known before the
  work started and is not a failure of the amendment. `COLUMN_TOLERANCE["volatility_*"]` is
  5e-11; the export stores volatility to 8 decimals and our `close` is the adjusted price rounded
  to 2 dp at write time (house rule 8), which puts a **rounding floor of ~1.6e-7** under any
  possible reproduction. `volatility_*` cannot reproduce to 5e-11 at any `ddof`. Loosening the
  tolerance to make the column pass is exactly what must not be done silently, so it is recorded
  (docs/05 §2's closing paragraph, docs/PARITY-M11.md §8) and left alone.

  So the cell count is the wrong instrument here, and the **error magnitude** was measured
  instead — 1,350 comparable cells, absolute error against the export:

  | column | `ddof=1` median | `ddof=0` median | tighter by |
  |---|---:|---:|---:|
  | `volatility_one_month` | 7.498e-03 | **1.634e-07** | 45,884× |
  | `volatility_three_months` | 2.751e-03 | **9.350e-08** | 29,423× |
  | `volatility_six_months` | 1.643e-03 | **6.495e-08** | 25,304× |
  | `volatility_nine_months` | 1.914e-03 | 1.114e-03 | 2× |
  | `volatility_one_year` | 1.417e-03 | 8.185e-04 | 2× |
  | **all 1,350** | **2.206e-03** | **2.401e-07** | **9,187×** |

  Cells within 5e-9 (what 8-dp storage implies): **0 → 23**. Within 5e-11: 0 → 0, as predicted.
  The three windows that do not cross the 2026-02-01 gap improve by 25,000–46,000× and land
  **exactly on the 1.6e-7 rounding floor**, which is the strongest possible confirmation that
  `ddof` was the entire arithmetic error and that nothing but stored precision now separates us
  from the reference. 9M and 12M improve only 2× for the same cause-E reason as `rsi_*`.

- [x] G4: total parity cells improve from the 4,514 baseline; state the new number
  EVIDENCE: **4,514 → 3,168 of 9,186 compared (−1,346, a 29.8% reduction).** Nine columns remain
  *reported unchecked* rather than compared (five `circuits_*` with an unresolved definition, plus
  `beta` / `marketcap` / `high_all_time` / `away_from_high_all_time` awaiting inputs). **No
  tolerance was widened and no column was dropped or added to an exclusion list.**

  The four-variant table, which is the evidence that the two amendments are independent and that
  neither is carrying the other:

  | family | BASE | RSI only | VOL only | BOTH |
  |---|---:|---:|---:|---:|
  | `rsi_*` | 1354 | **402** | 1354 | **402** |
  | `volatility_*` | 1355 | 1355 | 1355 | 1355 |
  | `sharpe_return_*` | 609 | 609 | **215** | **215** |
  | `absolute_return_*` | 42 | 42 | 42 | 42 |
  | `positive_days_percent_*` | 569 | 569 | 569 | 569 |
  | `ma_*` | 304 | 304 | 304 | 304 |
  | other | 281 | 281 | 281 | 281 |
  | **TOTAL** | **4514** | **3562** | **4120** | **3168** |

  Read the table: the RSI column moves `rsi_*` and nothing else; the VOL column moves
  `sharpe_return_*` and nothing else; BOTH is exactly their sum with no interaction and no
  collateral movement in the four families neither amendment touches. A change that had reached
  further than it claimed could not produce that partition.

  Remaining 3,168, by cause: **~1,355** the `volatility_*` tolerance floor (a test-design defect,
  above), **~813** cause E's missing 2026-02-01 session (`positive_days_percent_{9M,12M}` 542 +
  `ma_200` 268), **271** cause D's `MEDIAN_VOL_BARS`, **~380** the 9M/12M `rsi_*`/`sharpe_*`
  residual which is also cause E, and the rest causes E and F. **None is the RSI or volatility
  formula.**

- [x] G5: **GTT stop prices re-measured after the change** — how many of 269 names move, by how
      much, and whether any crosses the 8%/12% clamp. Non-negotiable #4 depends on this number.
  EVIDENCE: measured, not reasoned about, through `baskfy_core.score.stop_from_vol` with the
  desk's own `app/config.py` constants (`STOP_MIN/MAX/VOL_MULT` = 0.08/0.12/2.2), on the 269 of
  271 names with a non-null `volatility_one_year` and close. PRE = the tree before this leaf
  (1.4.1's window fix already in); POST = as landed.

  * **132 of 269 stops move.** All **132 rise, 0 fall** — `ddof=0` gives a smaller standard
    deviation, so a narrower stop distance, so a higher stop price on a long. The direction is
    uniform because the change is a uniform multiplicative factor `√((N−1)/N) < 1`.
  * **By how much: median 0.0239%, p90 0.0321%, max 0.1468%** (EQUITASBNK) of the stop price.
    In rupees: median **₹0.30**, max **₹8.50** (BOSCHLTD, on a four-figure price).
  * **Clamp: no name crosses, in either direction. 0 of 269.** The census is unchanged —
    24 at the 8% floor and 97 at the 12% ceiling both PRE and POST (the reference's own census is
    24 / 100). Non-negotiable #4's vol-scaled 8–12% band is intact and no stop moved into or out
    of a clamp.
  * **Closer to the reference, not further:** stops differing from the reference go
    **133/269 → 125/269**.

  Top-15 in full, PRE → POST, reference in brackets:

  | symbol | price | PRE | POST | [REF] | move | stop width |
  |---|---:|---:|---:|---:|---:|---|
  | LAURUSLABS | 1814.00 | 1652.80 | 1653.10 | [1651.70] | 0.0182% | 8.89% → 8.87% |
  | SONACOMS | 824.00 | 745.40 | 745.60 | [745.50] | 0.0268% | 9.54% → 9.52% |
  | SYRMA | 1500.10 | 1320.10 | 1320.10 | [1320.10] | — | 12.00% → 12.00% |
  | RRKABEL | 2784.90 | 2468.00 | 2468.60 | [2469.20] | 0.0243% | 11.38% → 11.36% |
  | ATHERENERG | 1465.80 | 1289.90 | 1289.90 | [1289.90] | — | 12.00% → 12.00% |
  | WELCORP | 1917.10 | 1706.90 | 1707.30 | [1708.10] | 0.0234% | 10.97% → 10.94% |
  | SANSERA | 4003.20 | 3522.80 | 3522.80 | [3522.80] | — | 12.00% → 12.00% |
  | SKYGOLD | 795.05 | 699.60 | 699.60 | [699.60] | — | 12.00% → 12.00% |
  | CUPID | 284.03 | 249.90 | 249.90 | [249.90] | — | 12.00% → 12.00% |
  | SHILPAMED | 809.80 | 712.60 | 712.60 | [712.60] | — | 12.00% → 12.00% |
  | RADICO | 4732.90 | 4256.70 | 4257.70 | [4258.20] | 0.0235% | 10.06% → 10.04% |
  | HSCL | 781.80 | 701.30 | 701.50 | [700.80] | 0.0285% | 10.29% → 10.27% |
  | AETHER | 1633.60 | 1439.00 | 1439.40 | [1439.90] | 0.0278% | 11.91% → 11.89% |
  | APARINDS | 17214.00 | 15148.30 | 15148.30 | [15148.30] | — | 12.00% → 12.00% |
  | AEGISLOG | 1367.70 | 1203.60 | 1203.60 | [1203.60] | — | 12.00% → 12.00% |

  Seven of the fifteen do not move at all because they sit on the 12% ceiling, where the clamp
  absorbs the change entirely. Of the eight that do, every one moves **toward** its reference
  value except HSCL, which overshoots by ₹0.70. The largest single move in the basket is ₹1.00
  on RADICO at ₹4,732.90.

  **The answer for non-negotiable #4: stops move, by rounding, by less than 0.15% anywhere and
  0.03% in the basket, uniformly in the safer direction (tighter), and the 8%/12% band is
  untouched.** This is the number Maulik was told would move, and it moved by a third of what the
  reference gap already was.

- [x] G6: basket impact measured — does the top-15 change? the top-12? Order generation is the
      question that decides whether this is safe to land.
  EVIDENCE: all 271 instruments scored through `baskfy_core.score.score()` with the desk's own
  config (`MOMENTUM_BLEND` 10/30/30/15/15, `SHARPE_BLEND` 35/35/15/15, `MIN_MEDIAN_DAILY_VALUE`
  ₹5 cr, `MAX_AWAY_FROM_HIGH` −30), three ways — REF (the export's published values, ground
  truth), PRE, POST. `marketcap`, `beta`, `series`, `is_nifty_fno` and `circuits_*` carried from
  the export in all three, so the comparison isolates the factor engine. 1.4.1's method exactly.

  * **Hard eligibility: 0 disagreements with the reference out of 271, PRE and POST.** Not one
    filter fires differently. No name is wrongly bought or wrongly rejected.
  * **top-12: unchanged. 12/12 identical across REF, PRE and POST.**
  * **top-15: the same fifteen names. 15/15 across all three.** The *order within* it changed —
    and changed **onto the reference's order exactly**. PRE ranked WELCORP 2nd; REF and POST both
    rank it 6th, with SONACOMS/SYRMA/RRKABEL/ATHERENERG moving up one each. POST's top-15
    sequence is now identical to the reference's, name for name and place for place.
  * **top-20: improved.** REF∩PRE was 19/20; **REF∩POST is 20/20**. The amendment drops PARAS and
    adds ANANDRATHI, which is what the reference holds. The 20th position was wrong and is now
    right.
  * **Scores move toward the reference:** `|ΔSCORE|` median 0.100 → **0.000**, p90 0.200 →
    **0.100**; SCORE exactly equal to the reference for **115/239 → 140/239** eligible names.
    `|Δrank|` p90 3 → **2**.

  **One metric got worse and it is an artefact, stated rather than buried.** Exact rank agreement
  with the reference fell 72/239 → 53/239. It is a tie-breaking artefact, not a regression:
  POST has 188 distinct SCOREs among 239 eligible names against PRE's 189, and **117 of POST's
  rank mismatches are names whose SCORE is *identical* to the reference's** — ties whose internal
  order is arbitrary in both rankings. The metric that is not degenerate under ties, exact SCORE
  agreement, improved by 25 names. `|Δrank|`'s p90 also improved. Reporting only the favourable
  one would have been the dishonest choice.

  **Penalty cliffs — the part that genuinely changes desk behaviour.**

  *Volatility, cliffs at 0.45 (−1.5) and 0.55 (−3):* **one** name changes band —
  **GOKULAGRO 0.450140 → 0.449224**, crossing below 0.45 and losing its −1.5 penalty. This is the
  name leaf 1.4.3 flagged as sitting within 0.0004 of the line; it fell off it, and it fell onto
  the reference's side (REF has it low). Band agreement with the reference: 264/271 → **265/271**.

  *RSI, cliffs at 78 (−2) and 82 (−4, and `PARABOLIC_RSI` trims a held position to a runner):*
  **13** names change band. Band agreement with the reference: 257/271 → **270/271**, and **all
  13 flips land on the reference's band** — every one is a correction, not a new error.

  | symbol | PRE | POST | new band |
  |---|---:|---:|---|
  | WELCORP | 73.61 | **79.86** | >78 — wait or tranche · **in the top-15** |
  | PARAS | 70.95 | 78.54 | >78 · leaves the top-20 on this change |
  | BOSCHLTD | 76.48 | 78.46 | >78 |
  | HEG | 72.49 | 80.31 | >78 |
  | HAL | 71.99 | 80.30 | >78 |
  | EMIL | 77.37 | 81.97 | >78 |
  | PRICOLLTD | 71.76 | 78.25 | >78 |
  | GNFC | 71.08 | 79.00 | >78 |
  | VARROC | 77.82 | **85.67** | >82 — **trim to runner** |
  | RKFORGE | 72.77 | **84.03** | >82 — **trim to runner** |
  | TI | 79.81 | **87.23** | >82 — **trim to runner** |
  | LUMAXTECH | 69.70 | **83.53** | >82 — **trim to runner** |
  | DIACABS | 80.94 | **83.58** | >82 — **trim to runner** |

  This is the largest real consequence of the leaf and it is worth naming plainly: **Cutler@N−1
  on a 22-bar window is far more responsive than Wilder@22 seeded from 2023**, so the desk's RSI
  bands were systematically reading low. The total F-penalty the RSI rule levies across the book
  goes **8.0 → 40.0** against the reference's **36.0** — and the entire 4.0 gap is one name, KRN
  (a −4 we levy that the reference does not). The volatility penalty goes 108.0 → 106.5 against
  the reference's 105.0, and that 1.5 gap is exactly the net of the six residual band
  disagreements. Both totals reconcile to the cell.

  Of the top-15, only **WELCORP** changes band, into the >78 "wait or tranche" rule — and the
  reference agrees it belongs there. **No name in the traded basket enters the >82 trim rule.**

  **The one remaining RSI band disagreement is data, not formula.** KRN: reference 71.48 (low),
  ours 93.47 (high). Its `close` and `high_one_year` match the export exactly, but its
  `absolute_return_one_month` does not (19.91 published vs 21.99 ours) — a bar-level price
  disagreement inside the last month, i.e. an unadjusted corporate action of cause F's family.
  A formula error cannot move RSI without moving the return computed off the same bars. Likewise
  the six residual volatility band disagreements are cause F (DIACABS 2.5956 vs 0.4692 — a 5.01×
  unadjusted split also visible in `high_one_year` 1899.95 vs 379.00) or insufficient history
  (BLUESTONE, MTARTECH are NULL for us: recent listings).

  **Verdict: safe to land.** The basket the desk trades at its stated size is the same list,
  in the reference's own order; the 20th position is fixed; eligibility is untouched; the stops
  move by rounding within the band. What changes is the RSI band the desk reads on 13 names, and
  on all 13 the change is toward the truth.

- [x] G7: full suite green; any fixture regenerated is argued before it is regenerated
  EVIDENCE: **`uv run pytest` → 3,253 passed, 1,138 skipped, 0 failed** (`<scratchpad>/l21/suite2.txt`).
  `uv run ruff check` clean on all four files this leaf touched.

  **The argument, written and read before the golden was rewritten.** Pre-change copy kept at
  `<scratchpad>/l21/momentum_scan.csv.pre`; the prospective file was rendered to a *scratch* path
  first (`<scratchpad>/l21/momentum_scan.after.csv`) so the diff could be read without
  overwriting anything committed. `test_the_bytes_are_stable` states its own rationale — *"If this
  fails, either the engine changed a number or a column moved. Both are worth a human reading the
  diff, which is why the fixture is committed rather than regenerated in-test."* The engine
  changed a number, deliberately, so the diff was read.

  **The diff is the argument. On the fixture's five synthetic symbols, exactly the columns the two
  amendments feed moved, and exactly nothing else:**

  * **moved** — `rsi_one_month` (5/5 rows), `volatility_one_year` (5/5), and all five
    `sharpe_return_*` (5/5, 5/5, 4/5, 3/5, 4/5 — the rows that did not move are where 2-dp
    rounding absorbed the shift).
  * **byte-identical** — `symbol`, `series`, `date`, `close`, `marketcap`,
    `median_volume_one_year`, `beta`, `circuits_three_months`, `circuits_one_year`,
    `positive_days_percent_three_months`, `positive_days_percent_six_months`, `ma_20`, `ma_50`,
    `ma_100`, `ma_200`, `away_from_high_one_year`, `is_nifty_fno`, and **all five
    `absolute_return_*`**.

  That partition is not something a regeneration can fake. `absolute_return_*` and
  `positive_days_percent_*` are computed over the *same resolved windows* as volatility and would
  have moved had the change leaked into window resolution; `ma_*` and `median_volume_one_year`
  would have moved had it leaked into bar counts. Neither did. It is precisely the closure of
  `{RSI kernel, vol ddof}` and precisely its complement.

  **Two independent checks that the moved numbers are the right numbers, not merely different
  ones:**

  1. `volatility_one_year` moves by a *constant ratio* across all five symbols —
     0.0322381213 → 0.0321763032 is 0.9980827, and `√(260/261)` = 0.9980827. The fixture's
     calendar is weekdays-only, so its 12-month window is 261 bars. The predicted `√((N−1)/N)`
     lands on the seventh significant figure for a value of `N` that comes from the fixture's own
     synthetic calendar and appears nowhere in the amendment.
  2. `sharpe_return_*` moves in the reciprocal direction and proportion (ALPHA 1.16 → 1.19;
     1.16 / 0.9980827 = 1.1622), which is `sharpe = ret / vol` with `ret` byte-identical —
     confirming the sharpe movement is entirely inherited and that no return was disturbed.

  `rsi_one_month` moves a long way on this fixture (ALPHA 39.06 → 89.47, ECHO 13.86 → 0.0) and
  that is expected rather than alarming: `_scan_fixture` generates smooth closed-form
  sine-times-exponential paths, and on a smooth wave the last 21 changes and a recursion seeded
  320 bars earlier are simply different quantities. BRAVO and DELTA reaching exactly 100.0 and
  ECHO exactly 0.0 is docs/05 §5's own no-losses / no-gains rule firing on a monotone stretch of
  the wave — the boundary values, arrived at from real arithmetic.

  **No test was weakened.** The assertion is still exact byte-identity, the same predicate at the
  same strictness against the corrected engine. Nothing was moved into an exclusion list, no
  `skipif` was added, and the `volatility_*` tolerance that *would* have made 1,355 cells pass was
  written down (G3) rather than touched. `test_reference_parity.py` was not modified at all.

- [x] G8: no test weakened; no look-ahead introduced
  EVIDENCE: four files touched; `test_reference_parity.py` not among them — no tolerance widened,
  no column excluded. The oracle was re-transcribed from the amended spec and still agrees with
  the engine at its unchanged 1e-4; the edge-guard class went from 3 cases to 5, adding a
  path-independence test **no recursive kernel can pass**. Look-ahead is impossible by
  construction: both changes read strictly fewer, strictly older bars, and the right edge stays
  at `as_of`. Detail below.

  **No test weakened.** Four files were touched. `test_reference_parity.py` was not one of them —
  no tolerance changed, no column entered `KNOWN_UNRECONCILED`, `NEEDS_EXTRA_INPUT` or
  `UNRESOLVED_DEFINITION`, and the gate the leaf is judged by is the same gate as before. The two
  test files that did change:

  * `packages/core/tests/factor_oracle.py` — the independent pandas oracle, which by design
    *transcribes docs/05* rather than importing from `baskfy_core`. When the spec is amended the
    oracle must follow it or it stops being an oracle of the spec and becomes a fossil of the old
    one. `_sample_stdev` → `_population_stdev` (`/ (n-1)` → `/ n`) and `_wilder_rsi_at` →
    `_cutler_rsi_at` (the recursion replaced by `sum(gains) / period` over an explicit slice),
    called with `n - 1`. It remains genuinely independent: still pure Python loops and explicit
    slices against the engine's Polars expressions and cumulative-sum matrix, still importing only
    constants. `test_factor_crossvalidation.py` passes at its **unchanged** 1e-4 absolute
    tolerance across all 25 corpus instruments — two implementations that share no code agreeing
    on the amended formula.
  * `packages/core/tests/test_factor_edge_guards.py` — `TestWildersSeedingAtTheEdges` renamed
    `TestCutlersWindowAtTheEdges`. Three of its four cases pinned the *old* spec's behaviour at
    `N = 1` (RSI 100 / RSI 0 off a one-observation seed); docs/05 §5's amended text makes a
    zero-return window NULL and says so explicitly, so those assertions were re-pointed at the
    new spec — not relaxed. **The class got strictly stronger, not weaker: it went from three
    cases to five.**
    - `test_a_history_of_exactly_the_window_length_still_has_an_rsi` — the `steps == period`
      boundary, killing `steps < period` → `<=` and the `out[period - 1 :]` index mutants.
    - `test_one_bar_short_of_the_window_has_no_rsi` — **new**, the other half of that boundary,
      which the old class never covered.
    - two `N = 1` cases, now asserting NULL, killing `period < 1` → `< 0` and → `< 2` and the
      `len(dates) < 2` guard's mutants. They read the **rounded** result, because "no value" is a
      NULL in storage (`baskfy_core.precision` converts the engine's in-flight NaN) and that is
      the contract every reader downstream actually sees — a stricter assertion than the raw NaN.
    - `test_the_rsi_does_not_depend_on_history_before_the_window` — **new**, and the one that
      matters most: it feeds the same final window twice, once behind 380 bars of violent crash
      and rally and once bare, and requires the same RSI. **This test is unpassable by any
      recursive kernel**, including the one that was there yesterday. It pins the path-independence
      docs/05 §5 now claims, which no test previously did.

  **No look-ahead introduced.** House rule 5 holds for both amendments, by construction:

  * `ddof` is a divisor. `rolling_std(window_size=n, min_samples=n, ddof=...)` reads the identical
    `n` rows either way; changing `n-1` to `n` in the denominator cannot reach a bar the previous
    expression did not already read. The window's right edge is untouched at `as_of`.
  * The RSI change makes the read strictly **smaller and strictly later**. Wilder's read *all*
    history from the first bar forward (that is what a seeded recursion does — its answer depended
    on data hundreds of bars before the window, which the new path-independence test now forbids).
    Cutler@N−1 reads only the last `N−1` changes, a subset of what the old kernel read, ending on
    the same final bar. It is impossible for a change that only ever discards **older** bars to
    introduce look-ahead.
  * No point-in-time index membership or factor row was re-dated; `windows.py` was not touched.

  **House rule 9** (money and prices `numeric`, never `float`) is untouched: neither `volatility`
  nor `rsi` is a money column, both are float-valued factors under `baskfy_core.precision`'s
  existing 10-dp and 4-dp contracts, and no precision entry changed. **House rule 3**: no
  `# type: ignore`, no `any`, no swallowed exception added — `_cutler_rsi` in fact *removes* the
  `np.errstate(invalid="ignore")` the seeded version needed. **House rule 4**: docs updated in the
  same change.

  Two suite failures at the start of this leaf were **not** this leaf's and are excluded here on
  evidence, not assertion: `test_no_escape_hatches::test_no_type_ignore_comments` points at
  `services/api/src/baskfy_api/plan_store.py` (untracked, sibling leaf 2.2) and
  `test_plan_expiry::test_the_module_names_no_ambient_clock` at
  `packages/core/src/baskfy_core/curated_plans.py` (modified by sibling in-flight work). Neither
  file is in this leaf's write scope, neither test reads any file this leaf touched, and both
  were resolved by their owners.

---

## Files changed

| file | change |
|---|---|
| `decile-blueprint/packages/core/src/baskfy_core/factors.py` | `_VOL_DDOF = 0` constant + `rolling_std(..., ddof=_VOL_DDOF)`; `_wilder_rsi` → `_cutler_rsi` called at `window.length - 1`; both docstrings carry the measurement and the authority |
| `decile-blueprint/docs/05-factor-formulas.md` | §2 and §5 amended, each retitled, each with what changed, the measurement, why the old prose was not simply wrong, and on whose authority |
| `decile-blueprint/packages/core/tests/factor_oracle.py` | independent oracle re-transcribed from the amended §2 and §5 |
| `decile-blueprint/packages/core/tests/test_factor_edge_guards.py` | `TestCutlersWindowAtTheEdges`: 3 cases → 5, including the path-independence test no recursive kernel can pass |
| `decile-blueprint/packages/core/tests/fixtures/momentum_scan.csv` | golden regenerated — argued under G7 before it was rewritten |

## Handed off, not fixed here

Cause **E** (the 2026-02-01 Budget session missing for mainboard EQ) is now the single largest
remaining parity cause and gates the 9M/12M residual in both `rsi_*` and `sharpe_return_*`. It is
a backfill, not a core change. Cause **D** (`MEDIAN_VOL_BARS` 252 → 247) and the
`COLUMN_TOLERANCE["volatility_*"]` tolerance defect remain open per docs/PARITY-M11.md §8.
