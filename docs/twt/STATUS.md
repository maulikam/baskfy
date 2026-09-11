# TW run — live status

The status page for the three-weeks-tight run. Updated at the end of every module, **loud about
what is NOT done**. A fresh session resumes from the first module not marked ✅.

**Run state: eight of eleven units green; TW5 green (the sleeve has its own money, its own book and its counter); TW4 green (the sleeve is wired); TW2 green (the engine exists and the study reproduces).** TW0 green; the pack is written. TW1 (the pure core) and
TW3 (the schema) run in parallel because they share no file — TW1 owns
`packages/core/src/baskfy_core/twt/`, TW3 owns the migration and the models.

**The local stack is up.** `make up` was run at the start of this session: Postgres is healthy on
5433, so TW3's db-marked tests actually run rather than skipping. Before that the Docker daemon
was not running at all, which is worth recording because it silently turns a db-marked suite into
a green skip. Started 11 Sep 2026 on
branch `developer`. The report will be `../../TW-FINAL-REPORT.md`; what needs Maulik's hands is
`../../NEEDS-MAULIK.md` § TWT.

## Module ledger

| Module | State | One line |
|---|---|---|
| TW0 — The pack | ✅ | Ten documents, eight pre-taken decisions, four standing defaults, the run's root gate file, and one correction pushed back into the research note |
| TW1 — The pure core | ✅ | `baskfy_core.twt` — ten modules, twelve test modules, `gates/twt-1.md` **12/12 with evidence**. Cross-checked against `tscan.py` itself: 9,600 cells, 0 mismatches |
| TW2 — Goldens: reproduce the study | ✅ | **Harness 15/15 + engine 15/15** (`gates/twt-2.md`). `baskfy_core.twt.backtest` exists and the study reproduces: **164 of 164 trades, zero missing, zero extra**, with `exit_date`, `exit_price`, `quantity`, `hold_sessions` and `reason` identical and both price legs to the paisa. All seven headline numbers inside tolerance, no tolerance moved |
| TW3 — Schema and settings | ✅ | The thirteen `tw_` tables, `0041_twt` with a round-tripped downgrade, the seed at ₹0, the four bounds (one of them a floor). `gates/twt-3.md` 10/10 |
| TW4 — The nightly job | ✅ | `baskfy.twt.detect` — the three tables, the funnel, the ratchet's arithmetic the night before, `COMPUTE_TWT` as the chain's fourteenth step (wrapped so it cannot raise), the 21:00 retry and `make twt`. `gates/twt-4.md` **10/10 with evidence**; nine decisions TW4.1–TW4.9 |
| TW5 — The sleeve's cash and book | ✅ | `baskfy_api.twt_sleeve` — the sleeve's own equity and cash, its book as `BookState`, the person's settings reaching the arithmetic, and the half-size **counter** spent once per filled entry. `gates/twt-5.md` **8/8 with evidence**; four decisions TW5.1–TW5.4 |
| TW6 — Desk plan, `/twt/execute`, the ratchet | ⬜ | |
| TW7 — The fill-day rule and the naked-line assertion | ⬜ | |
| TW8 — The pages | ✅ | All 9 gates green with evidence (`gates/twt-8.md`). Web `/twt` and `/twt/backtest`, read-only and asserted so; the desk page's shape as a component mounted on no web route (DECISIONS-TW TW8.1). Fixtures to `03`; the parent wires `/twt/today` and `/twt/backtest` when TW4 and TW5 land. Suite 165 files / 2,933 tests green, lint 0 errors |
| TW9 — The backtest on the page | ⬜ | |
| TW10 — Safety and the runbook | 🟡 | **Runbook half GREEN** (`docs/twt/FIRST-LIVE-MORNING.md`, `gates/twt-10-runbook.md` 8/8). The safety-properties half needs the whole sleeve and comes last |

States: ⬜ not started · 🔄 in progress · ✅ green · ⛔ blocked · 🟡 partial.

**A correction to this page, 11 Sep 2026.** The TW0 row above claimed *"eleven per-module gate
files"*. Two existed: `gates/twt-root.md` and `gates/twt-0.md`. `06` says the eleven are the
run's, which is true of the plan and was not true of the disk, and a status page that overstates
what is on disk is the one thing this page exists not to do. Each module's gate file is now
written **at the start of that module**, from `06`'s own AC, which is also the honest order: a
gate written before its module is a commitment, and one written at the end is a description.

---

## TW0 — The pack ✅ (11 Sep 2026)

### What the pack decided, so no later module re-opens it

| | Decision | Where |
|---|---|---|
| TW0.1 | The look-ahead weekly reading is **not** a configuration of the sleeve; it lives in the test module only | `DECISIONS-TW.md`, `04` §3.2 |
| TW0.2 | The rank key is the **signal session's own turnover**, not the 20-day average — the research note's prose was the stale half and was corrected | `DECISIONS-TW.md`, `04` §6.3 |
| TW0.3 | The liquidity floor ships at **₹5 crore**; the research's ₹2 crore survives as one field with one caller | `DECISIONS-TW.md`, `04` §3.5 |
| TW0.4 | TWT stores **its own** breadth row and calls **VBT-1's** arithmetic with its own spelled-out thresholds | `DECISIONS-TW.md`, `04` §4.4, `03` §4 |
| TW0.5 | `trail_pct` is bounded **below** (18 %), not above — the measured cliff is in the tightening direction | `DECISIONS-TW.md`, `02` |
| TW0.6 | A fresh listing has **no** entry event; the research's counter seeding is not reproduced | `DECISIONS-TW.md`, `04` §3.4 |
| TW0.7 | A corporate action may raise a stop or raise an alert — **never lower a stop** | `DECISIONS-TW.md`, `04` §7.3 |
| TW0.8 | `TWT_STOP_BAND` is 0.5–30 %, additive; the desk's own band is untouched | `DECISIONS-TW.md`, `04` §10.7 |

### The correction pushed back into the research

`research/tight-close/STRATEGY.md` §3 said candidates are "ranked by 20-day turnover". The code that
produced every number in that note ranks by the signal day's own turnover. Per the root
`CLAUDE.md` rule of 9 Sep 2026 — when the code and a doc disagree, find out which is the later fact
and **fix the stale half** — TW0 corrected the note and cited the code. It did not change a number.

### The real-money gate, as Maulik set it on 11 Sep 2026

No paper phase. `DRY_RUN_SESSIONS_REQUIRED` is **0** and no session-count condition exists in
`02` §3. What remains are five conditions that carry evidence — TW10 green, both trees green, the
backtest on the page, the written runbook — and **his own flag flip**, plus half size for the first
ten live **entries** (not sessions, because this book enters about eighteen times a year).

### What is NOT done after TW0 — which is everything

* **No code exists.** There is no `packages/core/src/baskfy_core/twt/`, no `tw_` table, no
  migration, no task, no route, no page. Every number in `01` is the research's, measured on the
  research's panel by the research's code, and **nothing in this repository has reproduced one of
  them yet.** TW2 is where that stops being true, and until TW2 is green the pack is a plan, not a
  verified plan.
* **The sleeve has never run and has never traded.** `tw_config.sleeve_capital_inr` will be seeded
  at 0 and this run never sets it. `BASKFY_TWT_EXECUTION_ENABLED` does not exist yet and will
  default false.
* **The ratchet — the one new mechanism — has never executed anywhere**, not in a test, not in a
  dry run, not on the box. `02` §3 records the consequence of the no-paper decision: it will first
  ratchet with real money behind it.
* **The plant's own bars have not been checked for this strategy.** `01` §1 inherits VBT-1's
  finding that 832 stock-days have no bar and 602 names lack a clean 50-session volume window in
  the verification period. The live scan will therefore show **fewer names than Chartink** on some
  days. That is in `NEEDS-MAULIK.md` § TWT as a thing to expect, not a thing this run can fix.
* **`0041` is free today.** A concurrent session that lands a migration first makes TW3's number
  wrong; TW3 re-checks `alembic heads` rather than trusting `03`.
* No deploy, and none planned: the box and `tools/deploy/` are Maulik's.

### Baseline, measured on this machine on 11 Sep 2026

| | |
|---|---|
| Branch | `developer`, HEAD `fae98ac` ("PC1: green — the Portfolio Command Center replaces the repeated portfolio blocks") |
| Alembic head | `0040_vbt_scan_run` — **`0041` is the next free number** |
| Sibling sleeves | the swing book (SW0–SW14) and VBT-1 (VB0–VB13.5, deployed to the Phase A box on 11 Sep, flag false) |
| Working tree at the start | `GATES.md` and `research/volume-breakout/vbt/sim.py` modified; `docs/twt/`, `gates/twt-*`, `research/cmo-weekly/`, `research/momentum-scan/`, `research/tight-close/` and `decile-blueprint/data/` untracked. **None of these are this run's except `docs/twt/`, `gates/twt-*` and `research/tight-close/`**; every TW commit names its own paths |

### Gates

`gates/twt-root.md` (R0–R13) and `gates/twt-0.md` … `gates/twt-10.md`. A module is finished when its
own file is fully checked **with evidence**, its tests are green and its commit exists — not when
it feels done.

---

## TW3 — Schema and settings ✅ (11 Sep 2026)

`gates/twt-3.md` 10/10. Thirteen tables, one migration, one seeder, four bounds, two test modules.

### What exists now that did not before

| | |
|---|---|
| `services/api/alembic/versions/0041_twt.py` | Thirteen tables, revising `0040_vbt_scan_run`. **The downgrade was round-tripped for real** — upgrade → downgrade → upgrade left 193 columns, 103 constraints and 28 indexes byte-identical, compared from `information_schema` rather than asserted |
| `packages/core/src/baskfy_core/models/twt.py` | The same thirteen in `models/base.py`'s types. Every rupee is `INR` (12,2), every level `PRICE` (18,2), every exchange print `PRICE_RAW` (18,4) — `03`'s own conventions paragraph, which does not name `MONEY` |
| `baskfy_api.seed.seed_twt_config` + `baskfy-seed twt` | One row per sole user, **`sleeve_capital_inr` written explicitly as 0**, `ON CONFLICT DO NOTHING` so a re-deploy never resets a capital, a stop or a trail a person chose |
| `baskfy_api.twt_settings` | Five editable fields, three ceilings, **one floor**, two job-owned fields a patch cannot name, and the audit row that answers "what was `trail_pct` on the morning that stop was armed" |
| `.env.example` × 2 | `BASKFY_TWT_EXECUTION_ENABLED=false`, `NIGHTLY=true`, the three `_MAX` and the one `_MIN`, in both the monorepo root's file and `decile-blueprint`'s |
| `packages/core/tests/test_twt_schema.py`, `test_twt_ceilings.py` | 119 + 53 tests. The db half runs against a real Postgres and **never commits**: every row goes in a transaction that is rolled back |

### The floor, since it is the thing most likely to be undone

`trail_pct` is bounded **below** at 18.00 and has no ceiling (TW0.5). Three tests exist because
one would not have been enough: below refuses, *at* is allowed, and **above is allowed too** — a
30 % or 40 % trail passes, because widening the only exit is conservative and tightening it is the
failure mode. The refusal has its own problem type (`setting-below-floor`), its own env suffix
(`_MIN`), and `to_view` hands the form a separate `floors` map so a page can never render
"max 18".

### Three decisions recorded

`TW3.1` the vocabulary tuples are transcribed from `03`/`04` and pinned to them rather than
imported from `baskfy_core.twt`, which TW1 was writing in a parallel session — reversible in TW4,
one import at a time. `TW3.2` `tw_position.entry_adj_factor` exists although `03` §5's table omits
it, because `04` §7.3 and TW0.7 read it by name and the alternative is a migration on the morning
of a split. `TW3.3` a below-floor refusal is its own problem type — **and its position in the
enum is load-bearing**: declared after the ceiling's it silently rewrote the 422 description on
every route in the published OpenAPI document, which `test_api_artifacts.py` caught and
`test_twt_ceilings.py` now pins.

### What is NOT done after TW3

* **Nothing has ever been written to any of these tables.** Thirteen empty tables and one config
  row that does not exist yet either: the seeder returns 0 until an `app_user` exists, which on a
  freshly migrated database it does not. TW4 is the first module that writes a row.
* **`tw_config.sleeve_capital_inr` is 0 and stays 0.** No module of this run sets it;
  `NEEDS-MAULIK.md` § TWT T1 is where it is asked for.
* **`BASKFY_TWT_EXECUTION_ENABLED` is false in every file that mentions it**, and a test scans
  every committed `.env*` under the monorepo to keep it that way.
* **No route, no page, no task reads any of this yet.** `baskfy_api.twt_settings` has no router;
  TW6 mounts one.
* **Two pre-existing lint failures block `make lint` for the whole tree, and neither is TW3's.**
  `services/worker/src/baskfy_worker/tasks/vbt_rescan.py:81` is 101 characters (committed at
  `29ce944`, VB13.4) and `apps/web/src/components/portfolio/__tests__/no-internals.test.tsx:68`
  fails `tsc` (committed at `dd9cc73`, PC-INT). Both files are untouched in the working tree. TW3
  left them alone per its own brief and ran the four lint steps individually instead: ruff clean
  but for that one line, `ruff format` clean over 725 files, `mypy --strict` Success over 621.

---

## TW1 — The pure core ✅ (11 Sep 2026)

`packages/core/src/baskfy_core/twt/` — ten modules, `04`'s arithmetic, and **nothing else**:
DataFrames and dataclasses in, DataFrames and dataclasses out. Law 1 is asserted over the source
(`test_twt_purity.py`), not trusted. `gates/twt-1.md` is **12 of 12 with evidence**.

| Module | What it is |
|---|---|
| `config` | 47 fields across eight frozen dataclasses, every one of `04`'s numbers with the clause it comes from. Money, prices and every multiplier that lands on a price are `Decimal` |
| `calendar` | §2.1's thin sessions — **VBT-1's implementation called**, TWT's own thresholds written out field by field |
| `indicators` | `vol_sma`, `turnover_inr`, `turnover_avg_20`, `sma_dma`, `limit_locked`, and the ISO-week / calendar-month bucket keys §3.2 and §3.3 read |
| `signals` | `weekly_closes`, `month_low_back`, `tight_state`, `entry_events`, `with_twt_columns`, `detect_signals` — the point-in-time reading, and no switch for the other one |
| `breadth` | §4.4's delegation to `baskfy_core.vbt.breadth`, with TWT's own 200 / 40.0 spelled out |
| `sizing` | §6.2's four caps **in order**, the cap that bound named, §6.4's half size applied before every cap |
| `exits` | the 20 % stop, the ratchet with both clamp branches, §7.3's split refusal, §7.4's fill-day rule, §7.5's write-off |
| `sleeve` | §9's equity and cash — its own rows, never the account's |
| `plan` | §10.1's skips in order, §10.2's two exit kinds and no third, §10.3's hash |

### What is asserted, beyond "the tests pass"

* **The year boundary.** The week key is built from the **ISO year**; a calendar-year key numbers
  2024-12-30 as 202401 and is not monotone, which would hand the last week of December the previous
  January's closes. `test_twt_signals.py::TestTheYearBoundary` asserts the key list is sorted and
  that 2024-12-31 reads w1 = 2024-12-27, w2 = 2024-12-20.
* **The reproduction, before TW2 needs it.** `weekly_closes` and `month_low_back` were compared out
  of band against a direct transcription of `research/tight-close/tscan.py` over a 48-name ×
  300-session panel spanning ISO 202409 → 202517 with 6 % random missing bars: **9,600 cells, 0
  mismatches**. `entry_events` against a brute-force reading of §3.4 + TW0.6 over 400 random state
  patterns: **0 mismatches**.
* **No look-ahead.** Truncating the panel at session *t* changes no answer at or before *t*
  (`TestNoLookAhead`), which is house rule 5 at the only level a pure package can state it.
* **Six new decisions**, TW1.1–TW1.6 in `DECISIONS-TW.md`, all ⚠ UNREVIEWED and all cheap to
  reverse. The two a later module has to know about: `RESEARCH_TICK_INR` [₹0.01] exists for TW2
  alone (the study's stops sit on paise, the exchange's on five), and `on_adjustment` derives its
  trigger **without** the ratchet's `max`, because otherwise §7.3's alert branch is unreachable.

### What is NOT done

* **No backtest.** `04` §12's parameters are named in `BacktestConfig` and nothing runs them. TW2
  owns `baskfy_core.twt.backtest`; the gate file for TW1 never asked for it and the module does not
  pretend to have it.
* **Nothing is wired.** No task, no route, no page imports `baskfy_core.twt`. TW4 is the first
  caller.
* **`tight_state_lookahead` does not exist anywhere yet.** TW0.1 puts it in TW2's test module; TW1
  only asserts that the package cannot reach it.
* **`make lint` is red for the whole tree on three errors in two files TW1 did not touch** — the
  same `vbt_rescan.py:81` line TW3 records above, plus `services/api/tests/test_portfolio_write.py`
  (I001 at :40, E402 at :757), which the parallel PC run has open in the working tree. Ruff over
  TW1's own files is clean, `ruff format --check .` is clean over 728 files, and `mypy` is Success
  over 620.


---

## TW2 — The engine, and the study reproduced ✅ (11 Sep 2026)

`gates/twt-2.md` **15/15 with evidence**. The harness leaf built everything around the engine and
left one named seam; this one closed it. `packages/core/src/baskfy_core/twt/backtest.py` is the
sequencing of `04` §11 — and it *sequences* rather than restates: the exits are
`exits.stop_fill`, `exits.fill_day_stop` and `exits.ratchet`, the size is `sizing.size_entry`, the
initial stop is `exits.initial_stop`, the gate is `breadth.breadth_series`. The engine adds the
order the session happens in and nothing else, so it cannot drift from the book.

### The reproduction, in numbers

* **164 of 164 golden trades matched** on `(symbol, entry_date)`; **zero missing, zero extra**.
* **Five of the eight compared fields are identical on every trade** — `exit_date`, `exit_price`,
  `quantity`, `hold_sessions`, `reason`. Entry and exit prices agree **to the paisa on both legs
  of all 164**. `r_mult` agrees to 1e-9.
* Exit labels: `STOP_HIT` 137, `STOP_GAP` 15, `STOP_DAY0` 2, `END_OF_RUN` 10 — the goldens' own
  four counts.
* Headline: CAGR **20.9223** (stored 20.92), max drawdown **-24.70006** (stored -24.7), 164
  trades, win rate **40.8537** (stored 40.9), profit factor **2.7072** (stored 2.71), average hold
  **104.6098** (stored 104.6), exposure **77.99** (stored 78.0), final equity **₹5,413,122.91**
  (stored 5,413,123). Every delta is the study's own rounding.

### What still differs, and why it is not a TW9 blocker

Three fields differ on some trades, all by the same fact: this engine is exact decimal and the
study is `float64`. `entry_price` on 44 trades by at most **5E-13** (one ulp — the study rounding a
single multiplication); `pnl_inr` on 153 by at most **2E-10** and `return_pct` on 164 by at most
**8.3E-14**, both from cancellation when two numbers around ₹1e5 are subtracted to get one around
₹1e2. Four to eleven orders of magnitude inside tolerances the harness wrote *before* this engine
existed — **and `git diff tools/twt/` is empty**, so none was moved. `06` TW2 says an unexplained
difference blocks TW9; there is none.

### The one choice that made it exact

A bar becomes a `Decimal` through **its shortest repr** (`Decimal(str(x))`), never `Decimal(x)`.
`baskfy_core.vbt.backtest` does the opposite and is right to; here it would have been a bug. The
research floors levels with `floor(x * 100 + 1e-9)`, and the `1e-9` is there because a double that
means ₹100.05 is `100.04999…`, so `x × 0.8` falls a hair under an exact paisa — which for a 20 %
stop is about one entry in five. Reading the float as the decimal it prints as is the same
correction made honestly, and it means no epsilon has to go anywhere near a live GTT level.
DECISIONS-TW **TW2.9**.

### Five decisions, TW2.9–TW2.13

| | |
|---|---|
| TW2.9 | The shortest-repr conversion, and why `Decimal(x)` is right for VBT-1 and wrong here |
| TW2.10 | The three float-epsilon fields, each sized, and the statement that none blocks TW9 |
| TW2.11 | The trail exit is reported `STOP_HIT` — TW2.4's limit accepted, not worked around; a `TRAIL_HIT` member is the thing that would break the goldens |
| TW2.12 | The backtest writes `next_trigger` (which *can* lower a stop, as the research does) and **counts** it; the plan refuses it. Measured at **zero** occurrences over the whole run |
| TW2.13 | The five steps TW9 needs, and the four things TW2 cannot tell it |

### What is NOT done after TW2

* **TW9 has not run.** The engine is parameterised for it — `PANEL_COLUMNS` is asserted to be a
  subset of what `with_twt_columns` produces, and the **defaults** are the shipped ₹0.05 tick,
  ₹5 crore floor and `BacktestConfig` capital — but no run over `ohlcv_daily` exists, and
  `tw_backtest_run` has no row from either source.
* **Four numbers TW2 measured are true of the study's panel only**, and TW9 must not inherit them:
  the ETF breadth denominator (TW2.2), `clamped_below_stop` (TW2.12 — zero here), `adj_factor`
  (1 everywhere in the research panel, so `04` §7.3's corporate-action branch has **never been
  exercised inside a hold**), and the tick itself (₹0.01 there, ₹0.05 on the plant — every stop
  moves by up to four paise, which moves which sessions stop out).
* **The goldens cannot grade a trail-versus-disaster split** and TW2 did not invent one (TW2.4,
  TW2.11). It is derivable from `initial_stop` against `exit_price` whenever a page wants it.
* **`twt_goldens.py` exits 1**, correctly: it exits 0 only on `clean` (no difference of any kind),
  and `clean` is a stronger claim than the goldens can support. `passes` — every difference inside
  a written-down tolerance — is the property `06` TW2's AC asks for, and it is True.

## TW4 — The nightly job ✅ (11 Sep 2026)

`gates/twt-4.md` is **10 of 10 with evidence**. The sleeve is wired for the first time: TW1's core
and TW3's schema now have a caller.

| What | Where |
|---|---|
| `baskfy.twt.detect(trade_date)` | `services/worker/src/baskfy_worker/tasks/twt.py` — the universe, the bars, TW1's columns, the state, the entry events, the breadth reading, the funnel, and the ratchet |
| `PipelineStep.COMPUTE_TWT` | `steps.py` (fourteenth, in `POST_PUBLISH_STEPS`) and `orchestrator.run_compute_twt_step`, wrapped so a detector that raises leaves the run SUCCEEDED |
| The 21:00 Beat retry | `celery_app.BEAT_SCHEDULE["twt-detect"]` + `tasks/celery_tasks.twt_detect_task`, a no-op when the session already has a breadth row |
| `make twt DATE=… [SESSIONS=N] [FORCE=1]` | `services/worker/src/baskfy_worker/twt_cli.py` |
| Storage precision | `baskfy_core.precision.COLUMN_PRECISION` gains eleven `tw_` columns; `week_range_pct` and `month_low_ratio` keep four places because they are the exact quantities `04` §3.1's lines 2 and 3 compare against |
| The tests | `services/api/tests/test_twt_detect.py` (24, db-marked, every one inside a transaction that is rolled back) and `services/worker/tests/test_twt_step.py` (10) |

### The two measurements that matter

* **The look-ahead pair.** A panel drawn backwards from its own as-of session is tight on
  **exactly one day**, so "shift it by one session" is a measurement rather than a coincidence:
  the signal, the gate and the trigger all move by one, and the shifted panel is **silent** on the
  session before — which a detector that had read the next bar would not be. The other half is
  stated the opposite way: a panel carrying one extra session after the as-of, closing at 500 on
  99 million shares, produces a **byte-identical** snapshot for the as-of session.
* **The split.** A 1:2 split stores `entry_reference_close = 100.00` against a `close` of 50.00 —
  the conversion of `03` §10, proved rather than asserted — and re-derives a trail of 80.00
  against a stop resting at 144.00. It is **not written**. `next_trigger` stays null and the
  alert is what happens instead (TW0.7). The opposite case is tested too, so the refusal is a
  rule and not a dead branch.

### Nine decisions, TW4.1–TW4.9

The three a later module has to know about: **TW4.3** the retry asks `tw_breadth_daily` and not
`tw_signal_daily`, because this book signals eighteen times a year and "no signals" is what a
good session looks like; **TW4.6** `entry_adj_factor` is never rewritten once an action has been
handled, so a split leaves the position frozen at its pre-split stop until a person re-arms it;
**TW4.8** the ratchet trails against `max(stop_price, gtt_trigger)`, so it never proposes a raise
the desk is guaranteed to block.

### What TW6 needs to know, since it is the module that reads `next_trigger`

* **The trigger is already computed and already stored.** `exit_lines` does no arithmetic: it
  emits `RAISE_GTT_STOP` when `next_trigger > gtt_trigger` **and** `next_trigger_for` is the
  session just closed (`04` §10.2). Both columns are written every evening the book has an open
  line, and `next_trigger_for` is set even on the evenings `next_trigger` is null — so
  "`next_trigger_for` is stale" means the nightly did not run, not that nothing moved.
* **A null `next_trigger` has two different meanings and the row says which.** Either the trail
  did not beat the stop in force (ordinary; most evenings on most lines), or a corporate action
  re-derived a *lower* trigger and TW0.7 refused to emit it. The second is distinguishable
  because `adj_factor` on the as-of `ohlcv_daily` row differs from `tw_position.entry_adj_factor`,
  and the step's `detail` carries `adjusted` and `adjustment_alerts` counts for the session.
  **`TWT_ADJUSTMENT_RESET` is TW6's to raise** — TW4 counts it and does not alert.
* **`stop_price` is never moved by the nightly.** It is TW6's to move, when the desk has actually
  replaced the resting GTT. The nightly writes `high_since`, `high_since_date`, `next_trigger`
  and `next_trigger_for`, and nothing else on `tw_position`.
* **The ratchet reads `max(stop_price, gtt_trigger)`** as the stop in force (TW4.8), so a
  proposal is never one the desk's own "a stop never falls" refusal would block.
* **The gate and the signals a plan reads are the rows of the signal session.** `tw_breadth_daily`
  has exactly one row per session the job saw, including thin ones (gate `SHUT`, null
  percentages), so it is also the honest answer to "did the nightly run for this date".

### The one thing this module could not measure, and it is the environment

**No clean full-suite run exists for this module's session.** Three agents shared one Postgres
throughout TW4 (TW2's reproduction and TW5's sleeve ran their own `pytest services/worker/tests`
repeatedly), and that suite `TRUNCATE … CASCADE`s the pipeline tables while the api tree's
fixtures `DROP SCHEMA public CASCADE`. Under that, `services/api` + `services/worker` in one
process reported `308 failed, 2315 passed, 240 errors`, and `services/worker` alone
`19 failed, 936 passed, 41 errors`, spread across `test_public_api`, `test_seed`,
`test_swing_schema_and_settings`, `test_backtest_job`, `test_holdings_sync`, `test_vbt_detect`
and others — a spread no single change produces. Sampled, **every** failure of the selection
gates is `asyncpg.exceptions.DeadlockDetectedError`.

Two checks pin it as the arrangement rather than the change: (1) `git checkout` of
`services/worker/tests/conftest.py`, the only shared fixture file TW4 edited, leaves the one
non-deadlock failure — `test_pipeline_self_sufficiency.py`'s
`test_it_writes_through_an_uncommitted_transaction_without_blocking`, which reports
`trading_days=0` because somebody truncated `trading_day` underneath it — failing **identically**;
(2) every module that failed passes on its own in a quiet window. The fix TW4 took for its own
part was to stop its fixture writing to a shared table at all: `test_twt_detect.py` no longer
inserts `trading_day` rows.

**What a fresh session should do:** run `make test-db` with nothing else against port 5433. That
is the arrangement the fixtures are written for and the one number this page cannot give.

### What is NOT done after TW4

* **Nothing has run against real bars.** Every row above was written against a synthetic panel.
  The first `make twt DATE=…` on the plant's own 4,800-name universe has not happened, and the
  funnel's shape at that size — how many names hold the state on an ordinary day — is a number
  this run does not have.
* **No plan, no sizing, no equity.** `run_detect_twt` writes state, signals, breadth and the four
  ratchet columns. `tw_session`, `tw_plan`, `tw_order` and `tw_position`'s *creation* are TW5's
  and TW6's; nothing here has ever inserted a `tw_position` row, only updated one.
* **`TWT_ADJUSTMENT_RESET` is counted, not raised.** The split branch records the alert on the
  step's `detail` (`adjustment_alerts`); the `AlertName` and the email are `05` §4's and belong to
  TW6/TW7. A split today would be visible at `/admin/pipeline` and nowhere else.
* **The thin-session path has been driven only by a synthetic thin session.** One name printing
  out of eight crosses `04` §2.1's quarter-of-the-median line and the test asserts the row and the
  frozen trail — but `04` §2.1's six real dates (`2017-10-19`, `2018-11-07`, `2024-01-20`,
  `2024-03-02`, `2024-05-18`, `2025-02-01`) are TW1's test's, on the research panel, and this
  module has never run over a session the exchange actually printed thin.
* **`bars_in_window` is a rolling count of printed bars, not of the window the average used.**
  They agree under `04` §2.2 for every name with a full window; for a name whose 200-day average
  is still warming up the column says how many bars exist rather than how many the mean consumed.

## TW5 — The sleeve's cash and book ✅ (11 Sep 2026)

`gates/twt-5.md` is **8 of 8 with evidence**. TW1's arithmetic now has money behind it: TW4 wired
the *detector*, and this is the half that answers "how much may this sleeve spend, and on what".

| What | Where |
|---|---|
| `sleeve_equity(session, user_id, as_of)`, `cash_available(…)`, `load_sleeve(…)` | `services/api/src/baskfy_api/twt_sleeve.py` — `04` §9.1-9.2 over TW1's pure `sleeve_value`, never a second arithmetic |
| `book_state(session, *, user_id, as_of, signal_date=None)` | The `BookState` `04` §10's `build_entries` and `exit_lines` read: what is held, what is naked of a GTT, what the evening's ratchet made due, and how many entries the session has already spent |
| `config_for(row)` | The person's `tw_config` — `max_position_pct`, `stop_pct`, `trail_pct` — written into the engine's `TwtConfig`. The form's number reaching the caps is the whole of "§6.2 wired" |
| `slot_multiplier(session, *, user_id, execution_enabled)` | `04` §6.4's half, read from the sleeve's own countdown. **What TW6 calls to size a line** |
| `count_first_live_entry(session, *, user_id, position_id, session_date, now, execution_enabled)` | The countdown's other half. **What TW6 calls on a fill** |
| The tests | `services/worker/tests/test_twt_sleeve_db.py` — 33, db-marked, against a live Postgres |

### The three prohibitions, and none of them is remembered

* **A proposed line cannot count an entry.** The parameter is a `position_id`, and a plan line
  nobody confirmed has no `tw_position` row to have one. A caller who invents an id is answered
  `LookupError`; another tenant's id is the same refusal (P4.1).
* **A `DRY_RUN` plan cannot count one.** A `simulated` fill counts nothing and is not marked
  `half_size` — which is the same sentence as `04` §6.4's "a `DRY_RUN` plan is full size".
* **One fill cannot count twice.** `tw_position.half_size` is the idempotency key as well as the
  record, so a partial fill that completes later, a retried confirm and a re-run of the morning
  all land on the same number (house rule 7). `tw_session.first_live_entries_counted` is
  incremented by an upsert that adds **in the database**, so the evening job and the fill can
  write the row in either order.

### The measurement that is `04` §3.5's argument rather than a number

At ₹25 lakh the slot is ₹2.5 lakh and 1 % of a ₹2 crore day is ₹2 lakh, so on such a name the cap
decides the size — and the shipped sleeve **never lines it**, because `min_turnover_inr` is ₹5
crore and `build_entries` answers `BELOW_LIQUIDITY_FLOOR`. Both halves are asserted, because
together they are the reason the floor is ₹5 crore and not the research's ₹2 crore (TW0.3). On a
₹50 crore name nothing binds and the line is the slot.

### Four decisions, TW5.1–TW5.4

**TW5.1** the loader answers ₹0 for an unseeded sleeve although `read_config` raises, so "nobody
ran `make seed`" and "seeded at ₹0" produce the same plan; **TW5.2** the sleeve is a
`MY_STRATEGY` capital portfolio by declaration and by test, following VBT-1 rather than the swing
book's `portfolio` row — filing belongs beside the page that shows it; **TW5.3** the countdown is
spent by a position id and `half_size` is both record and key; **TW5.4** `04` §6.3's session cap
counts `tw_order` rows by `signal_date`, the same key `uq_tw_order_one_per_signal` uses.

### What is NOT done after TW5

* **No `tw_position` row has ever been created by this product.** This module *reads* the book and
  *marks* one row on a fill; the row itself is written by the confirm path, which is TW6's. Every
  position in every test above was inserted by a fixture.
* **The counter has never counted a real entry**, because `BASKFY_TWT_EXECUTION_ENABLED` is false
  and there is no execute route yet. Every assertion above passes `execution_enabled=True` as an
  argument; no code in this repository passes it a true value.
* **No plan is stored.** `book_state` feeds TW1's pure `build_entries`, and nothing writes
  `tw_plan`, `tw_plan_line` or `tw_plan_skip`. The equity a page would show has no row.
* **Nothing files into the portfolio forest.** TW5.2: the sleeve declares its kind and source and
  no `portfolio` row exists for it, so `/portfolio` will not show a "Three weeks tight" group
  until somebody writes one.
* **The marks are adjusted closes**, per `04` §9.1 as written. It agrees with the exchange print
  on the as-of session, whose `adj_factor` is 1; a sleeve valued at an as-of date *before* a
  corporate action would mark at the re-adjusted series, and no test pins that case because no
  surface asks for it yet.
* **`cash_available` has never met a working order**, because this sleeve has none (`03` §6). If
  TW6 ever introduces one, this loader is where the reservation would have to appear.

---

## What the run has found in other people's code (11 Sep 2026)

Recorded here because each was red or wrong **before** this run started, and each would have been
inherited silently by every module after it.

| | What | Where |
|---|---|---|
| 1 | **Two safety guards had stopped guarding.** Both assert the portfolio API is not an order path; both did it by counting `@router.post(` and requiring exactly 2. PF9 legitimately added a third and both had been red since. A red safety test that fails for a boring reason is the worst kind — people learn to scroll past it. Rewritten to assert a named allowlist where each entry says why it is bookkeeping | `a72bbfb` |
| 2 | **A lint error failing the whole tree** since VB13.4 — a 101-character line. Every module's "make lint clean" gate would have inherited it | `a72bbfb` |
| 3 | **A VBT guard pinned to a stale count**, red since VB12 added a thirteenth table, though its own docstring says it exists to catch the list going EMPTY | `a72bbfb` |
| 4 | **TW0 was marked ✅ here and had never been committed** — ten documents, both gate files, and the research note the whole run reads as its spec, including the answer-key CSV the goldens score against | `5056aa1` |
| 5 | **This page claimed "eleven per-module gate files"** and two existed | corrected above |
| 6 | **The root `CLAUDE.md` said the web app's portfolio marks were live Kite quotes.** They never have been: `git log -S "get_ltp" -- services/api` is empty across every commit, and `_Prices` reads the newest two closes out of `ohlcv_daily`. An agent believed the table and told Maulik his portfolio was live | `3c00393` |

Number 6 is the one worth remembering. A working agreement that misstates where a money figure
comes from is worse than one that says nothing, because it is believed.


---

## A defect in this run's own gate files, found by TW5 (11 Sep 2026)

**Thirty-one `CHECK:` lines could never have passed.** Every one ran `uv run pytest … -q`, and
`pyproject.toml` already sets `addopts = "-q …"`. Two `-q` make `-qq`, which suppresses the
`N passed` summary line entirely — so `EXPECT: /passed/` was unsatisfiable against any result,
green or red.

TW1's agent noticed and quietly ran the command without the flag, recording the true count
(`18 passed in 0.09s`). That is the right instinct and it hid the fault: the file still said
something unrunnable, and the next reader would have inherited it.

Fixed across every `twt-*` gate file and `backfill-compression.md`; verified by running a repaired
check and matching its own EXPECT. **A gate that cannot fail is not a gate — and one that cannot
pass is worse, because it teaches whoever meets it that the ledger is decoration.**

Thirty-one gate files from earlier runs carry the same latent fault. They are closed records and
are deliberately left alone rather than rewritten; this note is here so the next run does not
repeat the pattern.
