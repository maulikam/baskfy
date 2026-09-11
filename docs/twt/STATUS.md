# TW run — live status

The status page for the three-weeks-tight run. Updated at the end of every module, **loud about
what is NOT done**. A fresh session resumes from the first module not marked ✅.

**Run state: six of eleven units green; TW2's reproduction, TW4 and TW5 in flight.** TW0 green; the pack is written. TW1 (the pure core) and
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
| TW2 — Goldens: reproduce the study | 🟡 | **Harness green, 15/15** (`b299c35`) — six published numbers reproduced exactly, and it confirms TW1 matches the research stock-day for stock-day. The reproduction itself is in flight: it needs `baskfy_core.twt.backtest`, which TW1 deliberately did not build |
| TW3 — Schema and settings | ✅ | The thirteen `tw_` tables, `0041_twt` with a round-tripped downgrade, the seed at ₹0, the four bounds (one of them a floor). `gates/twt-3.md` 10/10 |
| TW4 — The nightly job | 🔄 | In flight. Gate file `gates/twt-4.md`, ten gates |
| TW5 — The sleeve's cash and book | 🔄 | In flight. Gate file `gates/twt-5.md`, eight gates |
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
