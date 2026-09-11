# TW7 — the fill-day rule, and the assertion that no line sleeps unprotected

**Plan:** `docs/twt/06-module-plan.md` § TW7. **Spec:** `04` §7.4.

**Goal:** a line is never unprotected for a session, and the backtest's `stop_day0` is the live
book's same-session GTT. These are the same rule measured two ways.

- [ ] G1: In the backtest — a fill whose session low breaches the initial stop closes **that
      session**, reason `STOP_DAY0`, at the stop.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_twt_fill_day.py 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G2: The three boundary cases, because this is where an off-by-one costs money: an **open
      already below** the stop fills at the open; a bar that **touches exactly** the stop fills at
      the stop; a bar that misses by a tick does not close.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_twt_fill_day.py -k "open_below or exactly or tick" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G3: **The safety property.** Over a GENERATED book of fills and sessions, no session ends
      with an `OPEN` position and a null `gtt_id` once the sweep has run. A property test, not a
      spot check.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and (safety_propert or no_naked)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G4: The sweep is **idempotent and keyed on the day** — running it twice re-arms nothing
      twice and raises nothing twice.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and sweep and idempot" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G5: `TWT_POSITION_NAKED` fires on any naked line at any time, and
      `TWT_GTT_MISSING_AT_1515` on one the sweep could not fix. The runbook tells Maulik to react
      to both, so both must actually exist.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and (naked_alert or missing_at_1515)" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] G6: Both Python suites green and `make lint` clean.
  CHECK: cd decile-blueprint && uv run ruff check . 2>&1 | tail -2 && uv run mypy --strict 2>&1 | tail -2
  EXPECT: /Success|All checks passed/
  EVIDENCE: pending

<!-- A checked box whose EVIDENCE reads "pending" is UNMET. ABANDON: G<n> <reason> is the honest exit. -->
