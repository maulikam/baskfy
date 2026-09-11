# TW2 — the engine, and the study reproduced by it

**Plan:** `docs/twt/06-module-plan.md` § TW2 (and § TW9, which runs this same engine over the
plant's bars). **Rules:** `docs/twt/04-business-rules.md` §5–§8 and §11 are the sequencing.
**Law 1** (`/CLAUDE.md`): `packages/core` touches nothing — no database, no network, no disk, no
clock. **House rule 5**: no look-ahead. **House rule 9**: money and price levels are `Decimal`.
**House rule 3**: no `Any`, no `# type: ignore`, no swallowed exception.

**What already existed.** TW1 shipped the pure core (`config`, `calendar`, `indicators`,
`signals`, `breadth`, `sizing`, `exits`, `plan`, `sleeve`). A sibling leaf shipped the **golden
harness** — `tools/twt/` and the committed fixtures — green at 15 of 15 gates, with one named seam
(`gates/twt-2-harness.md` G12): `baskfy_core.twt.backtest` and the five names
`panel_from_frame`, `gate_vector`, `run_backtest`, `summarise`, `BacktestParams`.

**Ships:** `packages/core/src/baskfy_core/twt/backtest.py` (the engine),
`packages/core/tests/test_twt_backtest.py` (52 tests), the exports in
`baskfy_core/twt/__init__.py`, one name added to `packages/core/tests/test_twt_purity.py`'s module
set, and `TW2.9`–`TW2.13` in `docs/twt/DECISIONS-TW.md`.

**Owned by this leaf.** `backtest.py` and its tests. **NOT owned:** `services/` (two sibling
agents), `tools/twt/` (the harness leaf — read, never edited: `git diff tools/twt/` is empty).

**Headline.** The study reproduces at **164 of 164 trades, zero missing, zero extra**, with
`exit_date`, `exit_price`, `quantity`, `hold_sessions` and `reason` **identical** and entry and
exit prices agreeing **to the paisa on both legs of every trade**. What differs is `pnl_inr` and
`return_pct` (and `entry_price` on 44 trades) by between `5E-13` and `2E-10` — the study's own
float64 error against this engine's exact decimal, sized in DECISIONS-TW **TW2.10**. **No
unexplained difference survives, so nothing here blocks TW9.**

---

- [x] G1: **The seam is closed.** `baskfy_core.twt.backtest` exists and exports the five names
      `tools/twt/twt_goldens.py` asks for; the harness reports READY instead of `SeamNotReady`.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run python ../tools/twt/twt_goldens.py --seam
  EXPECT: /seam: READY/
  EVIDENCE: `seam: READY — baskfy_core.twt.backtest provides panel_from_frame, gate_vector,
  run_backtest, summarise, BacktestParams`. Baseline at the start of this leaf was
  `seam: NOT READY — baskfy_core.twt.backtest does not exist yet.`

- [x] G2: **Law 1.** The engine imports no database, no network, no disk and no clock, and
      `test_twt_purity.py`'s exact-module-set assertion is **widened, not weakened** — it still
      names every module and still refuses an unlisted one.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_twt_purity.py 2>&1 | tail -2 && grep -c '"backtest.py"' packages/core/tests/test_twt_purity.py
  EXPECT: /passed/
  EVIDENCE: `18 passed in 0.10s`, then `1`. `EXPECTED_MODULES` now holds eleven names; the AST
  scan runs over `backtest.py` like every other module and finds only `datetime`, `math`,
  `bisect`, `dataclasses`, `decimal`, `itertools`, `numpy`, `numpy.typing`, `polars` and three
  `baskfy_core.twt` siblings. The **only** deletion in the whole diff of that file is the
  docstring line that read `#: The ten modules ``docs/twt/06`` TW1 names.` — the assertion itself
  is untouched (`git diff … | grep "^-[^-]"` prints that one line and nothing else).

- [x] G3: **House rule 9.** No money and no price level is a float. TW1's G9 grep over the whole
      package still returns **0** with this module in it.
      ⚠️ **Repaired 12 Sep 2026, identically to `gates/twt-1.md` G9.** The six hits are
      `published.py`'s `years`, `calmar`, `sharpe`, `profit_factor`, `avg_hold_sessions` and
      `drift.py`'s `_points` — the study's published metrics, which are ratios and counts. House
      rule 9 governs money and prices; a Sharpe ratio is neither. Excluded by name, not by widening
      the pattern, and checked for vacuity: the filter still returns 2 against a planted
      `entry_price: float`.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && grep -rnE ": *float|float\(" packages/core/src/baskfy_core/twt/ | grep -viE "ratio|pct|share|weight|tolerance|#|years|calmar|sharpe|profit_factor|avg_hold|_points" | wc -l
  EXPECT: /^\s*0\s*$/
  EVIDENCE: `0`. Every statistic that is a ratio is named one (`sharpe_ratio`, and the `ratios`
  list its deviation is taken over); `years`, `calmar`, `profit_factor`, `avg_hold_sessions` and
  `avg_open_positions` are `Decimal` rather than float, which is also what makes them exact. The
  one float that is genuinely unavoidable — a fractional power for the CAGR — is isolated in
  `_annualised_pct` and read straight back into a `Decimal`. No `# type: ignore` and no `Any` in
  either file (`test_no_escape_hatches.py` green).

- [x] G4: **The 164 trades reproduce, identity first.** Every golden trade is produced and no
      trade is invented: 164 matched on `(symbol, entry_date)`, **zero** missing, **zero** extra.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run python ../tools/twt/twt_goldens.py 2>&1 | sed -n '1,3p'
  EXPECT: /164 of 164 golden trades matched \(164 produced\)/
  EVIDENCE: `164 of 164 golden trades matched (164 produced); 361 differences` and
  `by kind: {'field': 361}` — the `missing` and `extra` buckets are **absent**, which
  `TradeComparison.by_kind` only does when they are zero. Exit labels produced:
  `STOP_HIT 137, STOP_GAP 15, STOP_DAY0 2, END_OF_RUN 10` — the goldens' own four counts.

- [x] G5: **To the tick.** Five of the eight compared fields carry **no difference at all**, so
      entry and exit prices agree to the paisa on both legs of every trade.
      ⚠️ **Repaired 12 Sep 2026: the line moved out of the window.** The check sliced lines 1–4;
      the tool now prints a `seam:` line first — because the seam **closed**, which is TW2's whole
      achievement — and `by field:` slid to line 5. Grepping the line it wants cannot drift again.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run python ../tools/twt/twt_goldens.py 2>&1 | grep "by field:"
  EXPECT: /by field: \{'pnl_inr': 153, 'return_pct': 164, 'entry_price': 44\}/
  EVIDENCE: `by field: {'pnl_inr': 153, 'return_pct': 164, 'entry_price': 44}` —
  **`exit_date`, `exit_price`, `quantity`, `hold_sessions` and `reason` are absent from the map**,
  which the comparer only does when a field matched on every trade. `r_mult`, which the comparer
  does not check, agrees on all 164 to 1e-9. The thing that made this exact is DECISIONS-TW
  **TW2.9**: a bar becomes a `Decimal` through its shortest repr, which reproduces the research's
  `floor(x * 100 + 1e-9)` guard without carrying an epsilon into a live GTT level.

- [x] G6: **Every remaining difference is named and sized** — cause and magnitude, never a widened
      tolerance. A difference that survived investigation would be a blocker for TW9.
      ⚠️ **Repaired 12 Sep 2026: this CHECK could not execute at all.** It was a multi-line
      `python -c`; the runner hands a CHECK to `sh -c` as a single line, so everything after the
      first line was lost and the command died on a syntax error — a failure that says nothing
      about the tolerances. Rewritten as one line. Same fault as `gates/twt-11-funding-clock.md` F6.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run python -c "import sys; sys.path.insert(0, '../tools/twt'); from twt_goldens import run; c = run().trade_comparison; print('passes', c.passes, 'outside tolerance', len(c.outside_tolerance()))"
  EXPECT: /passes True outside tolerance 0/
  EVIDENCE: `passes True outside tolerance 0`, then per field:

  | field | trades differing | largest | tolerance | in ulps of the study's own double |
  |---|---|---|---|---|
  | `exit_date` · `exit_price` · `quantity` · `hold_sessions` · `reason` | **0** | none | — | — |
  | `entry_price` | 44 | `5E-13` | 0.01 | 1.10 |
  | `pnl_inr` | 153 | `2E-10` | 1 | ~2,000 |
  | `return_pct` | 164 | `8.31152528456E-14` | 0.01 | ~2,300 |

  **Cause, one fact for all three:** this engine's arithmetic is exact decimal and the study's is
  `float64`. `entry_price` is a single multiplication and is off by at most **one ulp** — the
  study rounding a product this engine does not have to round (`2436.9271249999997` against
  `2436.927125`). `pnl` and `ret_pct` subtract two numbers around ₹1e5 to get one sometimes around
  ₹1e2, so the study's relative error is amplified by cancellation to `3e-13` — still a hundredth
  of a microrupee on the worst trade. The exact value is ours; the golden is the float's
  approximation of it. **DECISIONS-TW TW2.10**, with the table. **No unexplained difference
  remains, so TW9 is not blocked.**

- [x] G7: **The headline numbers match to the precision the research printed**, each inside the
      harness's own tolerance, **with no tolerance moved**.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run python ../tools/twt/twt_goldens.py 2>&1 | tail -7 && cd .. && git diff --stat tools/twt/ | wc -l
  EXPECT: /ok  cagr_pct/
  EVIDENCE: all seven `ok`, then `0` — `tools/twt/` is unmodified.

  ```
  ok  cagr_pct         study      20.92 ours      20.92 delta +0.002306 (tolerance 0.01)
  ok  max_dd_pct       study      -24.7 ours      -24.7 delta -6.233e-05 (tolerance 0.05)
  ok  trades           study        164 ours        164 delta +0        (tolerance 0.0)
  ok  win_rate_pct     study       40.9 ours      40.85 delta -0.04634  (tolerance 0.05)
  ok  profit_factor    study       2.71 ours      2.707 delta -0.002761 (tolerance 0.005)
  ok  avg_hold         study      104.6 ours      104.6 delta +0.009756 (tolerance 0.05)
  ok  exposure_pct     study         78 ours      77.99 delta -0.008178 (tolerance 0.05)
  ```

  **Every delta is the study's own stored rounding, not a disagreement.** `final_metrics.json`
  stores each to one or two places: our unrounded 40.853658… *is* the stored 40.9, 2.707238… *is*
  the stored 2.71, 104.60975… *is* the stored 104.6. Unrounded, this run reads CAGR
  **20.92230550317644**, max drawdown **-24.700062333447505**, final equity **₹5,413,122.91**
  (the study rounds to 5413123), Sharpe **1.206** (stored 1.21), Calmar **0.847** (stored 0.85).

- [x] G8: **The reproduction test stops skipping.** The four tests `gates/twt-2-harness.md` G12
      left gated on the seam now run.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_twt_goldens.py -k "TestTheStudyIsReproduced" 2>&1 | tail -1
  EXPECT: /4 passed/
  EVIDENCE: `4 passed, 48 deselected in 4.97s` — the class that was
  `SKIPPED … baskfy_core.twt.backtest does not exist yet`. Both golden modules together:
  `80 passed, 1 skipped in 18.51s` against the harness's `77 passed, 4 skipped`. The remaining
  skip is the **inverse** gate and is correct: `test_twt_goldens.py:406` is
  `skipif(_SEAM_READY, reason="the engine has landed; there is nothing to fail")` — the test that
  asserts `produce()` raises loudly *without* an engine, which can no longer be exercised.

- [x] G9: **The look-ahead reading is still not reachable** (`04` §3.2, TW0.1). Adding a module to
      the package must not add a path to it; the AST scan runs over `backtest.py` too.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_twt_lookahead_recall.py -k "NotReachable" 2>&1 | tail -2
  EXPECT: /4 passed/
  EVIDENCE: `4 passed, 25 deselected in 0.28s`. `backtest.py` names no `tight_state_lookahead`,
  `include_current_week`, `lookahead`, `look_ahead` or `TightReading`; it takes the signal as a
  column it is **handed** and never re-derives one, so there is no reading for it to choose.

- [x] G10: **House rule 5 at the engine's own level.** Truncating the panel changes no trade that
      had already closed, and the gate and the size are both read at `col - 1` — the two places a
      next-open book could accidentally read the fill session.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_twt_backtest.py -k "NoLookAhead" 2>&1 | tail -2
  EXPECT: /6 passed/
  EVIDENCE: `6 passed, 46 deselected in 0.15s`. `TestNoLookAhead` truncates a four-name panel at
  each of four cut points and asserts every trade closed before the cut is identical in all ten
  fields; then plants a huge turnover on the **fill** session and asserts the 1 %-of-turnover cap
  still binds off the **signal** session's reading; then reverses the rank key on the fill session
  and asserts the order is unchanged. `TestTheGateIsReadAtThePreviousClose` does the same for the
  gate in both directions — shut on the fill session still fills, shut on the signal session does
  not and counts `gate_shut`.

- [x] G11: **TW9-ready: the engine reads the frame the plant produces, not the research's panel.**
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_twt_backtest.py -k "PlantsShape" 2>&1 | tail -2
  EXPECT: /6 passed/
  EVIDENCE: `6 passed, 46 deselected in 0.16s`. `PANEL_COLUMNS` is asserted to be a **subset** of
  `REQUIRED_COLUMNS | INDICATOR_COLUMNS | SIGNAL_COLUMNS`, so there is no column the study's panel
  has and `ohlcv_daily` lacks; a 300-session frame built from bare OHLCV runs the whole chain
  (`with_twt_columns` → `signal_mask` → `panel_from_frame` → `run_backtest`); and the **defaults
  are the shipped sleeve's** — ₹0.05 (not the study's paisa), ₹5 crore (not ₹2 crore),
  `BacktestConfig.initial_capital_inr`, `CostConfig.cost_bps_per_side / 100`. The study's values
  reach the engine only through `BacktestParams` and `signal_mask`'s one keyword. What TW9 still
  has to decide for itself is written down as DECISIONS-TW **TW2.13**.

- [x] G12: **The sequencing is tested branch by branch** (`04` §11), not only through the golden.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_twt_backtest.py 2>&1 | tail -2
  EXPECT: /52 passed/
  EVIDENCE: `52 passed in 0.25s`, in nine classes named for the rules they assert: the panel
  builder (boolean and `SIGNAL`/`SCAN_ONLY` readings, a missing bar that stays NaN); the gate at
  the previous close; exits at the open (`STOP_GAP` at the open, `STOP_HIT` at the stop, the
  five-blank-session write-off **and** the four-blank case that is tolerated); the fill-day rule
  including the proof that its gapped branch is *unreachable from this engine* because the stop is
  derived from the same open; the ratchet (it rises, it clamps under the close at `159.98`, and
  `clamped_below_stop` is counted); the caps (three a session, ten slots, rank by signal-day
  turnover, `SLOT`/`TURNOVER`/`CASH` each shown binding, `POSITION_PCT` proved unable to bind at
  ten slots, `BELOW_MIN_TRADE_VALUE`, locked-at-the-open, no-bar); the end-of-run liquidation;
  no-look-ahead; and the statistics. Each asserts a number the rule produces, not a number the
  code happens to produce (house rule 2).

- [x] G13: **Every difference from the research is in `docs/twt/DECISIONS-TW.md`**, numbered,
      `⚠ UNREVIEWED`, with its cause and its size.
      ⚠️ **Repaired 12 Sep 2026: missing `m` flag**, so an anchored `/^13$/` could not match a
      `13\n` the runner appends a newline to. The count has been 13 all along.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -c "^## TW2\." docs/twt/DECISIONS-TW.md
  EXPECT: /^13$/m
  EVIDENCE: `13` — the harness's eight, plus **TW2.9** (the shortest-repr conversion, and why
  `Decimal(x)` would have been a bug here where it is a virtue in VBT-1), **TW2.10** (the three
  float-epsilon fields, the table above, and the statement that none of them blocks TW9),
  **TW2.11** (the trail is reported `STOP_HIT` — TW2.4's limit accepted, and why a `TRAIL_HIT`
  member is the thing that would break the goldens), **TW2.12** (the backtest writes the
  research's `next_trigger`, which can lower a stop; the *plan* refuses to; measured at **zero**
  occurrences over the whole run and counted rather than assumed), and **TW2.13** (the five steps
  TW9 needs, and the four things TW2 explicitly cannot tell it: the tick, the floor, a real
  `adj_factor`, and two numbers that are zero *on this panel only*).

- [x] G14: **Nothing was weakened to get here.** No tolerance widened, no golden regenerated, no
      harness assertion loosened.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && git diff --stat tools/twt/ decile-blueprint/packages/core/tests/fixtures/twt/ | wc -l
  EXPECT: /^\s*0\s*$/
  EVIDENCE: `0` — `tools/twt/` and the committed fixtures are **byte-identical to HEAD**. The
  three trade tolerances and the seven metric tolerances were written by the harness leaf before
  this engine existed and every difference is four to eleven orders of magnitude inside them.
  **One honest caveat about `research/`:** `git status` shows ` M research/volume-breakout/vbt/sim.py`
  — that modification is in the working tree from **before this leaf started** (it is in the
  session's opening `git status`, and it is the `stop_level`/`dd_lock` work of an earlier run).
  This leaf read `research/` and wrote nothing to it; `research/tight-close/out/` is untouched.

- [x] G15: **Both suites and `make lint` green**, and this leaf broke nothing.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests 2>&1 | tail -1 && make lint >/dev/null 2>&1; echo "make lint exit $?"
      ⚠️ **Repaired 12 Sep 2026: the EXPECT spanned two lines with `.*`.** The runner tests the
      regex against `stdout + "\n" + stderr` and `.` does not cross a newline, so a two-command
      CHECK could never match however green both halves were. `[\s\S]*` is the fix, and it is
      the same fault `gates/twt-root.md` R13 records about a missing `m` flag.
  EXPECT: /passed[\s\S]*make lint exit 0/
  EVIDENCE: **`4077 passed, 5 skipped in 247.92s`** against the baseline `4012 passed, 8 skipped`
  — +65 tests (52 in `test_twt_backtest.py`, the four seam-gated reproduction tests that now run,
  and the extra parametrised purity cases for the eleventh module), and the skip count falls from
  8 to 5 because four of them were this leaf's seam. Measured twice, 20 minutes apart, with the
  same count.

  Lint over **everything this leaf ships** is clean: `ruff check` and `ruff format --check` over
  `baskfy_core/twt/`, `test_twt_backtest.py` and `test_twt_purity.py` → `All checks passed!` and
  `13 files already formatted`; `uv run ruff format --check .` → `737 files already formatted`;
  `uv run mypy` → **`Success: no issues found in 632 source files`**.

  **`make lint` as a whole exited 0 when first measured and is now red on one error in a sibling's
  file, named rather than fixed** — the same convention `gates/twt-1.md` G12 and
  `gates/twt-2-harness.md` G14 used. `services/api/tests/test_twt_detect.py:740` reports `RUF005`
  (tuple concatenation instead of unpacking); the file is TW5's, is untracked-modified in the
  working tree by the sibling agent running now, and did not exist in this form at the earlier
  measurement. Nothing this leaf wrote contributes an error to any of the three checks.
  `apps/web lint` reports one pre-existing `react-hooks/incompatible-library` **warning**, zero
  errors, in a file this leaf never touched.

  One transient failure worth recording so nobody chases it: a single run of
  `packages/core/tests/test_twt_schema.py` failed with an `asyncpg` `DBAPIError` while a sibling
  was applying a migration to the shared Postgres on 5433. It is a db-marked test in a file this
  leaf never touched, and it passed in the full run above, before it, and after it.

<!--
A checked box whose EVIDENCE reads "pending" is UNMET. ABANDON: G<n> <reason> is the honest exit.
Never weaken a test to make this file pass; settle a wrong criterion by the charter's precedence
order, record it in docs/twt/DECISIONS-TW.md, continue.

Baseline at the start of this leaf: `uv run pytest packages/core/tests` -> 4012 passed, 8 skipped
(4 of the skips were this leaf's seam). `uv run python ../tools/twt/twt_goldens.py --seam` ->
NOT READY. `uv run python ../tools/twt/twt_goldens.py` -> exit 1, SeamNotReady.

Note on the runner's exit code: `twt_goldens.py` exits 0 only when the run is **clean** (no
difference of any kind). It exits 1 here, and correctly: 361 differences exist, all of them inside
a tolerance. `passes` is the property TW2's AC asks for and it is True; `clean` is a stronger claim
than the goldens can support, for the reason TW2.10 gives. Do not "fix" the exit code.
-->
