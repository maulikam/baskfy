# TW7 — the fill-day rule, and the assertion that no line sleeps unprotected

**Plan:** `docs/twt/06-module-plan.md` § TW7. **Spec:** `04` §7.4.

**Goal:** a line is never unprotected for a session, and the backtest's `stop_day0` is the live
book's same-session GTT. These are the same rule measured two ways.

- [x] G1: In the backtest — a fill whose session low breaches the initial stop closes **that
      session**, reason `STOP_DAY0`, at the stop.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_twt_fill_day.py 2>&1 | tail -3
  EVIDENCE: `24 passed in 0.40s`. `TestTheBacktestReadsTheSameRule` runs the engine over a
  generated one-name panel: `low={1: 75.0}` → one trade, `reason is ExitReason.STOP_DAY0`,
  `exit_price == Decimal("80.00")`, `entry_date == exit_date`, `hold_sessions == 0`. The stop
  and never the low it traded through is pinned by running the same panel at `low=1.0` and
  `low=79.95` and asserting both exit at 80.00. `TestTheRuleAndTheGttAreOne` closes the module's
  own claim: over four fills, the price a `STOP_DAY0` fills at **is** `exits.initial_stop(fill)`,
  the trigger the live book's same-session GTT is armed at.

- [x] G2: The three boundary cases, because this is where an off-by-one costs money: an **open
      already below** the stop fills at the open; a bar that **touches exactly** the stop fills at
      the stop; a bar that misses by a tick does not close.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_twt_fill_day.py -k "open_below or exactly or tick" 2>&1 | tail -3
  EVIDENCE: `12 passed, 12 deselected in 0.49s`. Each case has its own fixture and its own fill
  price, and the fill is deliberately awkward (₹247.35 → a stop of ₹197.85, three paisa below
  `fill × 0.8`) so every boundary is a statement about the **tick-floored** level rather than
  about the multiplication. Open below → fills at the open (₹180.00, worse than the stop); open
  exactly at the stop → also the open (`<=`, not `<`); open one tick above → the stop. Low exactly
  at the stop → out, even when the session closes back at the fill. Low one tick above → `HOLD`,
  asserted twice: once directly and once as the only difference between two otherwise identical
  bars. The engine's own one-tick edge is pinned too (`low=80.05` → `END_OF_RUN`).

- [x] G3: **The safety property.** Over a GENERATED book of fills and sessions, no session ends
      with an `OPEN` position and a null `gtt_id` once the sweep has run. A property test, not a
      spot check.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and (safety_propert or no_naked)" 2>&1 | tail -3
  EVIDENCE: `9 passed, 7633 deselected in 5.12s`.
  `services/worker/tests/test_twt_safety_property.py` generates sessions of events — `FILL`
  (§9.1 case 1), `RATCHET_CANCELLED` (§9.1 case 2, the likely one), `EXIT`, `QUIET` — over a
  mutable book, sweeping at 15:15 of each, 150 Hypothesis examples per property.
  `test_no_naked_line_survives_a_session_the_sweep_ran`: with a desk that arms,
  `naked_after == ((),) * n` and `naked_lines(book) == ()` after every session.
  `test_a_refusal_is_always_reported_and_no_naked_line_is_ever_silently_dropped`: with a desk that
  refuses every n-th line, the set of positions ever left naked **equals** the set named in a
  `TWT_GTT_MISSING_AT_1515` page — a sweep may fail, it may not fail quietly.
  `TestTheGeneratorActuallyGeneratesTheFailure` is four deterministic tests proving the generator
  produces naked lines, refusals and exits, so none of the properties passes vacuously.

- [x] G4: The sweep is **idempotent and keyed on the day** — running it twice re-arms nothing
      twice and raises nothing twice.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and sweep and idempot" 2>&1 | tail -3
  EVIDENCE: `6 passed, 7636 deselected in 3.42s`. The two halves are idempotent for different
  reasons and both are asserted. **Re-arm**, by construction: the fake desk arms into its own book
  exactly as TW6's will write `tw_position`, so the second run reads a book with nothing naked —
  `first.rearmed == (1, 2)`, `second.rearmed == ()`, `desk.calls == [1, 2]`. **Alerts**, by a
  journal keyed on `(day, alert, position)` — `second.alerts == ()` and
  `second.suppressed == (TWT_POSITION_NAKED, TWT_GTT_MISSING_AT_1515)` while `second.naked == 1`,
  because suppressing the page never suppresses the fact. Keyed on the **day**: tomorrow pages
  again. Keyed per **position**: a line that goes naked again at 15:25 pages again, which is
  `FIRST-LIVE-MORNING` §9.1 case 2 and the failure a day-only key would swallow. `FileJournal`
  makes a second *shell* idempotent too (`data/twt/sweep/<date>.json`, untracked) — DECISIONS-TW
  **TW7.2** records why that is a file and not a `tw_session` column.

- [x] G5: `TWT_POSITION_NAKED` fires on any naked line at any time, and
      `TWT_GTT_MISSING_AT_1515` on one the sweep could not fix. The runbook tells Maulik to react
      to both, so both must actually exist.
  CHECK: cd decile-blueprint && uv run pytest -k "twt and (naked_alert or missing_at_1515)" 2>&1 | tail -3
  EVIDENCE: `6 passed, 7636 deselected in 3.84s`. `naked_alert` is a pure function of the book
  plus an `at`, so it fires at 09:20, 11:05, 15:15 and 21:40 alike — `FIRST-LIVE-MORNING` §8's
  bolded "at any time of day" — and is silent over a protected book. It names the line
  (`NAME4`, `#4`) and carries `quantity_open` and `stop_price`, because §9.2 step 3 has a person
  typing that number into Kite. `missing_at_1515_alert` is silent when the sweep fixed everything
  and otherwise says which side of the close it is on ("before the 15:30 close" /
  "already past the 15:30 close"), because §9.2's answer differs. Both `AlertName` members and the
  runbook they name (`docs/runbooks/09-twt-morning.md`) are **TW6's**, added in the parallel
  session; TW7 reads them out of `baskfy_worker.alerts` and `ops.RUNBOOKS` so the two cannot
  disagree, and asserts the file is on disk. DECISIONS-TW **TW7.4**.

- [x] G6: Both Python suites green and `make lint` clean.
  CHECK: cd decile-blueprint && uv run ruff check . 2>&1 | tail -2 && uv run mypy --strict 2>&1 | tail -2
  EVIDENCE: `All checks passed!` / `Success: no issues found in 644 source files` — `sweep.py`
  lives at `tools/twt/` (outside decile-blueprint, where the runbook and `06` put it) and is
  reached by mypy through `mypy_path`'s `../tools/twt`, so the 644 includes it;
  `uv run ruff check ../tools/twt/sweep.py` is clean too, and `ruff format --check` leaves all
  four TW7 files unchanged.
  **Suite: 5773 passed, 1857 skipped, 4 failed — and none of the four is TW7's.** All four are in
  files a sibling session created between 22:11 and 22:16 while this module was being written:
  `services/worker/tests/test_twt_backtest_job.py` (three `# type: ignore` comments and one
  `dict[str, Any]`, which `test_no_escape_hatches.py` correctly refuses — house rule 3) and
  `packages/core/tests/test_twt_published.py` (two `01` §7 row assertions). Both are TW9/TW8's.
  Every TW7 file and every file TW7 touched is green: `test_twt_fill_day.py` 24, `test_twt_sweep.py`
  34, `test_twt_safety_property.py` 9, `test_twt_goldens.py` 52 passed / 1 skipped; the six TWT core/worker suites together `210 passed, 1 skipped`.
  Re-run with the sibling's two files deselected: `2 failed, 5752 passed, 1830 skipped` — the two
  that remain are `test_no_escape_hatches.py`, which scans the **source tree** and so still reads
  `test_twt_backtest_job.py` whether or not it is collected. Its offender list names only that file.
  The DB-backed tests skip (`BASKFY_TEST_DATABASE_URL` unset); no TW7 test needs a database, so
  the scratch-database dance was not needed and nothing was reported as red for contention.

<!-- A checked box whose EVIDENCE reads "pending" is UNMET. ABANDON: G<n> <reason> is the honest exit. -->

## What TW7 shipped

| What | Where |
|---|---|
| The three boundary cases, each with its own fixture | `decile-blueprint/packages/core/tests/test_twt_fill_day.py` |
| The 15:15 chore | `tools/twt/sweep.py` |
| The sweep's behaviour, the two alerts, the deadline, the TW6 seam | `decile-blueprint/services/worker/tests/test_twt_sweep.py` |
| The generated safety property | `decile-blueprint/services/worker/tests/test_twt_safety_property.py` |
| The harness scan, scoped to the directory's real contents | `decile-blueprint/packages/core/tests/test_twt_goldens.py` (TW7.1) |
| Four decisions | `docs/twt/DECISIONS-TW.md` TW7.1 – TW7.4, all ⚠ UNREVIEWED |

**Were the three boundary cases already pinned?** Partly, and the gap was the expensive one.
`test_twt_exits.py::TestTheFillDayRule` already had "a low through the stop fills at the stop",
"an open already below fills at the open" and "a low exactly at the stop is out". What it did
**not** have was the *tick*: its negative case is a low of ₹81 against a stop of ₹80 — a rupee
out, which proves nothing about a boundary that is five paisa wide — and every case used a round
₹100 fill whose 20 % stop needs no flooring, so the level being tested was the multiplication and
not the level the exchange would hold. TW7's file re-reads all three at a tick's resolution
against a fill whose stop *is* floored, adds the miss-by-one-tick case in both the pure rule and
the engine, and adds the equivalence class that is the module's actual claim.

## The seam the parent has to wire

```python
Rearm = Callable[[PositionId], Awaitable[RearmOutcome]]   # PositionId = int, a tw_position.id
```

TW6 supplies one coroutine taking a single `tw_position.id` and returning
`RearmOutcome(position_id, armed: bool, gtt_id: str | None, reason: str)` — `armed=True` **with**
a non-null `gtt_id` is the only thing the sweep counts as protection, and `reason` is surfaced
verbatim to the person doing `FIRST-LIVE-MORNING` §9.2. The swap is the body of
`tools/twt/sweep.py::build_rearm`, which today returns `unavailable_rearm`. If TW6's callable has
a different shape (a session argument, a user id, a plan line), adapt it in `build_rearm` with a
closure — `sweep()` itself must not learn about the desk.
