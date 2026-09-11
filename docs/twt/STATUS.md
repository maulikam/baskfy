# TW run — live status

The status page for the three-weeks-tight run. Updated at the end of every module, **loud about
what is NOT done**. A fresh session resumes from the first module not marked ✅.

**Run state: TW1 and TW3 in flight.** TW0 green; the pack is written. TW1 (the pure core) and
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
| TW1 — The pure core | 🔄 | In progress, 11 Sep 2026. Gate file `gates/twt-1.md`, twelve gates |
| TW2 — Goldens: reproduce the study | 🔄 | The harness half in progress — loader, committed fixtures, difference reporter, recall scorer. The core plugs into one marked seam |
| TW3 — Schema and settings | 🔄 | In progress, 11 Sep 2026, in parallel with TW1 — different trees. Gate file `gates/twt-3.md`, ten gates |
| TW4 — The nightly job | ⬜ | |
| TW5 — The sleeve's cash and book | ⬜ | |
| TW6 — Desk plan, `/twt/execute`, the ratchet | ⬜ | |
| TW7 — The fill-day rule and the naked-line assertion | ⬜ | |
| TW8 — The pages | 🔄 | In progress, built against `03`/`05` with fixtures ahead of its data. Gate file `gates/twt-8.md` |
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
