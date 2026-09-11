# TW1 — the pure core: the arithmetic of TWT-1, touching nothing

**Plan:** `docs/twt/06-module-plan.md` § TW1. **Rules:** `docs/twt/04-business-rules.md` is the
spec; every threshold is a field of `baskfy_core.twt.config` and a test asserts no threshold is
written as a literal anywhere else. **Law 1** (`/CLAUDE.md`): `packages/core` touches nothing —
no database, no network, no disk, no clock.

**Ships:** `packages/core/src/baskfy_core/twt/` — `config.py`, `calendar.py`, `indicators.py`,
`signals.py`, `breadth.py`, `sizing.py`, `exits.py`, `plan.py`, `sleeve.py`, `__init__.py` — and
its tests under `packages/core/tests/`.

**The shape is VBT-1's**, which shipped: `baskfy_core/vbt/` has the same ten modules. Copy its
shape, its purity test and its config discipline. Do NOT copy its rules.

- [x] G1: The package exists with every module `06` names, and `baskfy_core.vbt.breadth` is
      CALLED rather than copied (`04` §4.4) — TWT passes its own spelled-out thresholds.
  CHECK: cd decile-blueprint && ls packages/core/src/baskfy_core/twt/ | tr '\n' ' ' && grep -c "from baskfy_core.vbt" packages/core/src/baskfy_core/twt/breadth.py
  EXPECT: /config.py.*signals.py/
  EVIDENCE: `__init__.py __pycache__ breadth.py calendar.py config.py exits.py indicators.py plan.py
      signals.py sizing.py sleeve.py` — the ten modules `06` TW1 names — and `grep -c "from
      baskfy_core.vbt" twt/breadth.py` = **5**: breadth_series, breadth_above_dma, gate_for,
      BreadthConfig, VbtConfig, all called through `twt.breadth.as_shared_config`, which writes
      TWT's own two thresholds out. `twt/calendar.py` does the same for the thin-session rule.
      `test_twt_purity.py` asserts the module set is exactly the ten and that both imports exist.

- [x] G2: **Law 1.** The package imports no database, no network, no disk and no clock.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_twt_purity.py 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `uv run pytest packages/core/tests/test_twt_purity.py` -> **17 passed in 0.11s**. The scan
      also proves the look-ahead reading is unreachable (TW0.1, over the syntax tree so the prose
      that explains the absence is allowed) and that no module names AUTO_EXECUTE.

- [x] G3: The signal is `04` §3 — the five lines with their senses, the three weekly closes with
      the current week's close taken as the latest daily close, the month-3 low, and the entry
      event. **The year-boundary case is named in the AC because it is the one that silently
      sorts wrong:** ISO week 1 of the next year is AFTER week 52, and a `searchsorted` on
      `year × 100 + week` must not place it before.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_twt_signals.py 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `uv run pytest packages/core/tests/test_twt_signals.py` -> **33 passed in 0.54s**.
      `TestTheYearBoundary`: the fixture ends 2024-12-31 — calendar year 2024, **ISO year 2025,
      week 1**; `test_the_week_key_never_goes_backwards_in_time` asserts the key list is sorted
      (a calendar-year key numbers 2024-12-30 as 202401 and is not monotone), and the December
      session reads w1 = 2024-12-27 and w2 = 2024-12-20 rather than nulls. December->March is
      `TestTheMonthThreeLow::test_march_reads_december_across_the_year_boundary`.
      Cross-checked out of band against a direct transcription of `research/tight-close/tscan.py`
      (`weekly_closes`, `monthly_low_months_ago`) over a 48-name x 300-session panel spanning ISO
      202409 -> 202517 with 6 % random missing bars: **9,600 cells compared, 0 mismatches**.
      `entry_events` vs a brute-force reading of `04` §3.4 + TW0.6 over 400 random state
      patterns: **0 mismatches**. `TestNoLookAhead` adds house rule 5 at this level: truncating
      the panel at session t changes no answer at or before t.

- [x] G4: The exits are `04` §7 — the 20 % hard stop, the trailing stop off the highest high
      clamped to the exchange tick, the ratchet that never lowers a stop, and the fill-day rule.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_twt_exits.py 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `uv run pytest packages/core/tests/test_twt_exits.py` -> **39 passed in 0.20s**. §7.1's 20 %
      stop floored to the tick (101.23 -> 80.95); §7.2's trail off `high_since` with both clamp
      branches (117.85 from the fallback, 119.95 from the fraction); `TestAStopNeverFalls` — the
      `max`, the stop in force, and the one pathological branch surfaced as `clamped_below_stop`
      rather than carried; §7.3's split refusal (TW0.7, TW1.3); §7.4's fill-day rule at the stop
      and at the open.

- [x] G5: Sizing applies every cap of `04` §6.2 **in order**, and §6.4's half size.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_twt_sizing.py 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `uv run pytest packages/core/tests/test_twt_sizing.py` -> **22 passed in 0.17s**.
      `caps_applied == (POSITION_PCT, TURNOVER, CASH)` asserts §6.2's order; §6.4's half size is
      asserted to be applied **before** every cap — a ₹2 crore name sizes to ₹1.25 lakh bound by
      SLOT, not ₹2 lakh bound by TURNOVER — and a DRY_RUN plan is full size.

- [x] G6: The breadth gate is **strict** (> 40 %, not ≥), and a zero denominator is SHUT rather
      than open — an empty universe must not read as a healthy market.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_twt_breadth.py 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `uv run pytest packages/core/tests/test_twt_breadth.py` -> **22 passed in 0.20s**.
      `gate_for(40.0000) is SHUT`, `gate_for(40.0001) is OPEN`, `gate_for(39.9999) is SHUT`;
      `measured_count == 0` gives `0.0000` and **SHUT** in the single reading and in the series,
      including a 50-name universe that all printed and none of which has a 200-session average
      yet. The series and the single reading were also checked to agree over 1,000+ shapes.

- [x] G7: **No threshold is a literal outside `config.py`.** This is the gate that keeps `04`
      the spec rather than a description of the code.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_twt_no_literals.py 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `uv run pytest packages/core/tests/test_twt_no_literals.py` -> **13 passed in 0.19s**. AST
      scan: no numeric constant outside {0,1,2,100} in signals/breadth/sizing/exits/plan/sleeve;
      no config value duplicated as a literal in indicators/calendar; 47 configured values; and
      `research_min_turnover_inr` / `RESEARCH_TICK_INR` have no production reference anywhere but
      `config.py` itself.

- [x] G8: Thin sessions (`04` §2.1) and the 10 % missing-bar tolerance (§2.2) are TESTS, not
      comments — `research/tight-close/STRATEGY.md` §1 is where they come from.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests -k "twt and (thin or tolerance or calendar)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `uv run pytest packages/core/tests -k "twt and (thin or tolerance or calendar)"` ->
      **73 passed, 2 skipped, 3945 deselected in 4.14s**. `test_twt_calendar.py` asserts a session
      at 10 % of the centred median is dropped and one at 30 % is not (the 0.25 share, read as
      *below*), that TWT's own `DataConfig` reaches the shared implementation, and that a name
      missing 5 of 50 sessions still has a `vol_sma` while one missing 6 does not
      (`min_samples(50) == 45`).

- [x] G9: Money and price levels are `Decimal`. No float reaches a price path (house rule 9).
      ⚠️ **Repaired 12 Sep 2026: the exclusion list had not kept up with the module.** The row went
      red on six hits, and not one of them is money or a price: `published.py`'s `years`, `calmar`,
      `sharpe`, `profit_factor` and `avg_hold_sessions` — the *study's published metrics*, which are
      ratios and counts — and `drift.py`'s `_points(value: float | Decimal) -> Decimal`, the
      converter that takes one of those metrics and returns a Decimal. House rule 9 is *"money and
      prices are numeric, never float"*; a Sharpe ratio is neither.
      The five names are excluded **by name rather than by widening the pattern**, which is the
      rule the namespace check follows for the same reason: a broad pattern silences the next real
      violation too. Checked for vacuity — the filter still returns 2 against a planted
      `entry_price: float` and `stop_level: float`.
  CHECK: cd decile-blueprint && grep -rnE ": *float|float\(" packages/core/src/baskfy_core/twt/ | grep -viE "ratio|pct|share|weight|tolerance|#|years|calmar|sharpe|profit_factor|avg_hold|_points" | wc -l
  EXPECT: /^\s*0\s*$/
  EVIDENCE: `grep -rnE ": *float|float\(" packages/core/src/baskfy_core/twt/ | grep -viE
      "ratio|pct|share|weight|tolerance|#" | wc -l` -> **0**. Every money and price threshold in
      `config.py` is `Decimal` (Polars promotes a Decimal literal to the frame's Float64 for a
      comparison, so one spelling serves the panel and the desk's exact levels); the only floats
      are `thin_session_min_share`, `rolling_min_share` and `min_pct_above_dma` — the last handed
      verbatim to VBT-1's breadth config — and each carries an excluded word.

- [x] G10: `test_no_escape_hatches.py` still green — no `# type: ignore`, no `Any`, no swallowed
      exception (house rule 3).
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_no_escape_hatches.py 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `uv run pytest packages/core/tests/test_no_escape_hatches.py` -> **8 passed in 0.45s**.
      No `type: ignore`, no `Any`, no swallowed exception anywhere the scan reaches.

- [x] G11: The whole core suite is green — TW1 broke nothing.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: `uv run pytest packages/core/tests` -> **4012 passed, 8 skipped in 445.43s**, exit 0.
      Baseline was green at 106 test files; TW1 adds twelve `test_twt_*.py` modules plus
      `twt_fixtures.py`, and breaks none of the 106.

- [x] G12: `make lint` clean — ruff, ruff format, mypy --strict.
      ⚠️ **Repaired 12 Sep 2026: this row grepped a line that had drifted out of its window.**
      It ran `make lint 2>&1 | tail -6` and matched `/Success|All checks passed/`. `make lint` runs
      the Python half first and the web half second; as the web half's output grew, the Python
      half's `Success: no issues found` fell outside the last six lines, and the row went red on a
      run in which lint was entirely clean. Worse in the other direction too: `/Success/` can match
      a run whose *later* stage failed, which is the exact fault `gates/twt-root.md` R12 records
      about `/passed/`. It now decides on the **exit code**, which is 0 only when every stage
      passed.
  CHECK: cd decile-blueprint && make lint >/dev/null 2>&1; echo "lint exit=$?"
  EXPECT: /^lint exit=0$/m
  EVIDENCE: `uv run mypy` -> **Success: no issues found in 623 source files**.
      `uv run ruff format --check .` -> **728 files already formatted**.
      `uv run ruff check` over TW1's own files (`baskfy_core/twt/`, `tests/test_twt_*.py`,
      `tests/twt_fixtures.py`) -> **All checks passed!**
      `make lint` as a whole is **red on three errors in two files TW1 neither created nor
      touched**, named rather than fixed, as the run's instruction asks:
      * `services/worker/src/baskfy_worker/tasks/vbt_rescan.py:81:101` E501 — committed at
      `29ce944` (VB13.4) and 101 characters at HEAD, so it pre-dates this module;
      * `services/api/tests/test_portfolio_write.py:40` I001 and `:757` E402 — the file is
      **modified in the working tree** by the parallel PC run (`git status` shows ` M`).
      Both are outside `packages/core` and outside TWT, and nothing TW1 wrote contributes an
      error to any of the three checks.

<!--
A checked box whose EVIDENCE reads "pending" is UNMET. ABANDON: G<n> <reason> is the honest exit.
Never weaken a test to make this file pass; settle a wrong criterion by the charter's precedence
order, record it in docs/twt/DECISIONS-TW.md, continue.
-->
