# VB run — live status

The status page for the volume-breakout run. Updated at the end of every module, **loud about
what is NOT done**. A fresh session resumes from the first module not marked ✅.

**Run state: VB9 green — the study re-runs from the plant's own bars and says how far it landed from the published number. VB10 not started.** Started 10 Sep 2026 on branch
`developer`. The report will be `../../VB-FINAL-REPORT.md`; what needs Maulik's hands is
`../../NEEDS-MAULIK.md` § VBT.

## Module ledger

| Module | State | One line |
|---|---|---|
| VB0 — The pack | ✅ | Eight documents, the seven pre-taken decisions, the standing defaults, and the two measurements that settled how the strategy is read |
| VB1 — The pure core | ✅ | Nine modules, 43 named thresholds and **245 tests**; law 1 asserted over the source, and no rule module spells out a number that is not 0, 1, 2 or 100 |
| VB2 — Goldens: reproduce the study | ✅ | **All 761 trades, to the paisa.** 32,929 scan hits, 6,293 signals, CAGR 18.23%, drawdown −27.94%, the yearly table to one decimal, and eleven of twelve neighbourhood cases to a tenth of a point |
| VB3 — Schema and settings | ✅ | Twelve `vb_` tables in migration **`0037_vbt`**, generated from the models and verified against them by Alembic's own comparison — 0 differences, and a downgrade/upgrade round trip; four settings, three ceilings, two flags, and a sleeve seeded at ₹0 |
| VB4 — The nightly job | ✅ | `baskfy.vbt.detect` writes `vb_signal_daily` + `vb_breadth_daily`, wired in as the chain's **thirteenth** step (unable to fail the night), with a 21:10 retry that asks before it works and `make vbt DATE=…` |
| VB5 — The sleeve's cash and book | ✅ | `baskfy_core.vbt.sleeve` states the arithmetic once; `baskfy_api.vbt_sleeve` loads it from `vb_` rows and nothing else. A holding this sleeve did not buy is invisible to it, and a resting limit commits cash without spending it |
| VB6 — Desk plan and `/vbt/execute` | ✅ | The evening job writes a plan with its skips and settles the session; the desk page shows it in three panels; one click per line goes through the real gateway and **0 orders reach a broker** |
| VB7 — The working order and its expiry | ✅ | The window is a field, proven at 1/2/3/5/10 sessions over random calendars with holidays in them; four `VBT_*` alerts raised in-process at 21:30 and 21:40, runbook 8, and **not one of the four writes a row** |
| VB8 — The pages | ✅ | Six read routes and one settings write; three Next.js pages with **no server actions at all**; `GET /vbt/today` p95 **140.7 ms** against a 300 ms budget over 2,500 rows |
| VB9 — The backtest on the page | ✅ | Three books over one detection pass, appended to `vb_backtest_run` and never edited; drift flagged past one CAGR point; the engine does the whole nine years in **23 seconds** |
| VB10 — Safety, and the claims become theorems | ⬜ | |

States: ⬜ not started · 🔄 in progress · ✅ green · ⛔ blocked · 🟡 partial.

---

## VB0 — The pack ✅ (10 Sep 2026)

Everything below was **measured on this machine on 10 Sep 2026**, not recalled.

### Repository

| | |
|---|---|
| Branch | `developer`, HEAD `c68c57f` ("PF1: green — the allocation ledger learns to split a holding across portfolios") |
| Trees | `decile-blueprint/` (screener + API + worker + web), `kite-momentum-rebalancer/` (the desk), `frozen/strangle/` (untouched) |
| **Another session's staged files** | Nineteen paths of a portfolio-redesign change were **staged but not committed** before this run started (`PORTFOLIO_REDESIGN.md`, the portfolio web components, `0035_split_allocation.py`, `broker_holdings_sync.py`, …). **They are not this run's and are not committed under any VB module.** Every VB commit names its own paths explicitly (`git commit -- <paths>`), which leaves that index untouched |
| Alembic head | `0035_split_allocation` — so **`0036` is the next free number** (VB3 re-checks) |
| Core suite at baseline | `uv run pytest packages/core/tests` — **exit 0**, green before anything was written |
| Research tree | `research/volume-breakout/` was untracked; VB0 commits it minus `aws/` and the pickles (DECISIONS-VB **VB0.1**) |

### The data the study ran on

| | |
|---|---|
| Export | `research/volume-breakout/aws/` — the AWS Phase-A box's tables as of 10 Sep 2026, 68 MB gzipped, **gitignored** (regenerate with `export_bars_aws.sh`) |
| Panel | **4,186 instruments × 2,396 sessions**, 2017-01-02 → 2026-09-09 |
| Thin sessions dropped by the rule | **6**: 2017-10-19, 2018-11-07, 2024-01-20, 2024-03-02, 2024-05-18, 2025-02-01 |
| Raw Chartink scan | **32,929** signals |
| VBT-1 signals (six filters) | **6,293** |
| ETFs in the panel | 349, each with **7–8 bars in nine years** — none ever reaches a 200-DMA (VB0.3) |

### The two measurements that settled how the strategy is read

Both were run against the export before a line of `docs/vbt/04` was written, because both decide
what the numerical contract says.

1. **Six filters, not eight** (DECISIONS-VB **VB0.2**). The six-filter reading of STRATEGY §3
   gives **6,293 signals, 18.23% CAGR, −27.94% drawdown, 761 trades, PF 1.55** — the published
   numbers exactly. `grid2.py`'s eight-filter version gives 6,254 / 17.47% / 765 / 1.51. The note
   is the decision; the script is the stale half.
2. **Breadth is its own series** (DECISIONS-VB **VB0.3**). Recomputed from the export it matches
   `out/breadth200.csv` to **1.1e-16**; excluding ETFs explicitly changes it by **0.0000** and
   flips the 40% gate on **0 of 2,396** sessions.

### What is NOT done, and is not pretended to be

* **No code exists.** `baskfy_core/vbt/` does not exist; no migration, no task, no router, no
  page, no desk route. VB1 starts from a blank file.
* **Nothing has run against the plant's own database.** Every number above is from the research
  export. VB4 is the first module that touches `ohlcv_daily` through the worker.
* **No sleeve capital.** `vb_config` does not exist yet and will be seeded at ₹0 (`02` §3.4).
* **The 262 missing instrument-days and the pre-2024 corporate actions are still missing**
  (STRATEGY §1, §6). Every number in `01` and `04` is against the data as it stands.
* **`BASKFY_VBT_EXECUTION_ENABLED` does not exist yet, and when it does it is false.** No VBT
  order has ever been placed, simulated or otherwise.
* **0 of 20 DRY_RUN sessions** (`02` §3.1).
* Nothing is deployed. The box does not know this sleeve exists.

---

## VB1 — The pure core ✅ (10 Sep 2026)

`decile-blueprint/packages/core/src/baskfy_core/vbt/` — nine modules, DataFrames and dataclasses
in, DataFrames and dataclasses out.

| Module | What it holds |
|---|---|
| `config.py` | Eight frozen dataclasses, **43 fields**, every one with the reason for its value |
| `calendar.py` | The thin-session rule and session counting — a holiday consumes no session |
| `indicators.py` | The eleven per-bar columns, computed on a **densified** frame so a window counts sessions rather than rows |
| `signals.py` | Chartink's five lines, the six trend filters, `SIGNAL` / `SCAN_ONLY` and `failed_filters` |
| `breadth.py` | The share above the 200-day average, and the two-valued gate |
| `sizing.py` | Ten equal slots, four budgets, the cap that bound named |
| `exits.py` | The 12% stop from the fill, the 21-EMA queue, the precedence |
| `orders.py` | The limit that works three **sessions**, its expiry and its fill model |
| `plan.py` | `build_entries` in `04` §9.1's order, `exit_lines`, `assemble`, `plan_hash` |

**245 tests, all green**, in ten files. The ones that are load-bearing rather than routine:

* `test_vbt_purity.py` — law 1 over the source: no database, network, disk or clock import, no
  `baskfy_execution`, no `kite_client`, **no import of `baskfy_core.swing`**, and no setting whose
  name contains `AUTO_EXECUTE` (`02` Track C §3).
* `test_vbt_no_literals.py` — over the **syntax tree**, not the text: a rule module may spell out
  0, 1, 2 and 100 and nothing else. Every threshold is a field.
* `test_vbt_docs_parity.py` — `04` §12's table is regenerated from the code and compared **both
  ways**; a field added, renamed or re-valued without a doc edit is red, and so is a row with no
  field behind it.
* `test_vbt_plan.py::test_every_skip_reason_is_reachable_from_the_plan` — a reason nothing can
  produce is a reason a page will never explain. One is deliberately unreachable from a signal
  (`STOP_NOT_BELOW_ENTRY`) and the test says why in its own message.

### Two things settled while writing it

* **`# type: ignore` is not available** (house rule 3, enforced by `test_no_escape_hatches.py`
  across the tree). Two helpers exist because of it: `number()` in the signal tests and `at()` in
  the indicator tests, each of which asserts the cell's type rather than silencing the checker.
* **A bar exactly on 6.5% is a floating-point boundary and is deliberately not pinned.**
  `(95.85 / 90 - 1) x 100` is `6.499999999999995` in IEEE 754 — the research's arithmetic produces
  the same value, so the two agree about the edge whichever way it falls. What the test pins is
  the **sense** of the comparison.

### What is NOT done at VB1

* **Nothing has run against real bars.** The next module is the one that matters: VB2 reproduces
  the study's 761 trades and 18.2% CAGR, or explains why it cannot.
* No `backtest.py` yet — the engine of `04` §11 is VB2's.
* No migration, no task, no router, no page, no desk route. `vb_config` does not exist.
* The mutation harness (`make mutants`) does not yet include `baskfy_core.vbt`.

---

## VB2 — The goldens ✅ (10 Sep 2026)

**The core is the study.** `baskfy_core.vbt.backtest` runs `04` §11's sequencing over the VB1
functions and, against `research/volume-breakout/aws/`, produces:

| | The study | The core |
|---|---|---|
| raw Chartink scan | 32,929 | **32,929** |
| VBT-1 signals | 6,293 | **6,293** |
| trades | 761 | **761**, every one matching on symbol, entry, exit, quantity and reason |
| worst price gap | | **₹0.0000** |
| CAGR | 18.23% | **18.23%** |
| max drawdown | −27.94% | **−27.94%** |
| win rate / profit factor | 37.8% / 1.55 | **37.84% / 1.55** |
| exits | 688 EMA · 62 stop · 1 no-bar · 10 end-of-run | **identical** |
| yearly table (`01` §4) | +17.3 −19.3 −3.8 +20.4 +45.6 +16.1 +64.7 +35.1 +1.2 +6.3 | **identical to one decimal** |

The neighbourhood reproduces too: entry window 2/3/5 → 11.4 / 18.2 / 17.1; stop 10/12/15 →
16.4 / 18.2 / 16.6; slots 8/10/15 → 15.5 / 18.2 / 12.9; gate 30/40/45/50 → 15.6 / 18.2 / 15.4 /
10.9; no gate → 18.5% at −48.8%; the raw scan traded the same way → 0.8%. **The one case that
does not is a 35% gate, by 0.2 points**, and DECISIONS-VB VB2.2 measures exactly why.

### What was written

| | |
|---|---|
| `packages/core/src/baskfy_core/vbt/backtest.py` | the `Panel`, the sequencing, the statistics, the yearly table |
| `tools/vbt/research_panel.py` | the export loader — **the same one VB9's plant run will use**, so a difference between the two runs is a difference in the data and never in the loader |
| `tools/vbt/reproduce.py` | the comparison as a command; exits 0 on PASS, and prints where it does not |
| `packages/core/tests/vbt_backtest_fixtures.py` | a planted trade with its arithmetic worked by hand in the docstring |
| `packages/core/tests/test_vbt_backtest.py` | 22 tests over that trade — runs everywhere, no export needed |
| `packages/core/tests/test_vbt_goldens.py` | 21 tests over the real bars; **skips loudly** when the 68 MB export is absent |

### The one thing that had to be found

The first full run matched 172 of 761 trades. The cause was `limit × 1` in the fill test: a bar
price from a `float` carries ~50 significant digits, Decimal rounds a product to 28, and a limit
that a low touched *exactly* came back a hair above it. Yesterday's close being today's low is
what a pullback looks like, so the case is common rather than exotic. One branch fixed it
(DECISIONS-VB **VB2.1**), and the lesson is general: an exact comparison against a tape price
must not pass through Decimal arithmetic first.

### What is NOT done at VB2

* **Nothing has touched the plant's own database.** Every number above is from the research
  export, which is gitignored; on a machine without it `test_vbt_goldens.py` skips.
* The reproduction takes **76 seconds** and about 2 GB of memory (a 3,837 × 2,396 dense panel).
  VB9 will need the same on the plant's bars, where the universe is larger.
* No migration, no task, no router, no page, no desk route. `vb_config` still does not exist.
* The **262 missing instrument-days** and the sparse pre-2024 corporate actions are unchanged
  (NEEDS-MAULIK § VBT, V2). Reproducing the study exactly reproduces its data gaps exactly.

---

## VB3 — Schema and settings ✅ (10 Sep 2026)

| | |
|---|---|
| Migration | **`0037_vbt.py`**, revising `0036_check_name` — **not** `0036` as the pack said; a concurrent session landed a migration mid-run and `alembic heads` reported two (DECISIONS-VB **VB3.2**) |
| Tables | Twelve: `vb_config`, `vb_config_audit`, `vb_signal_daily`, `vb_breadth_daily`, `vb_position`, `vb_order`, `vb_fill`, `vb_plan`, `vb_plan_line`, `vb_plan_skip`, `vb_session`, `vb_backtest_run` |
| Models | `packages/core/src/baskfy_core/models/vbt.py`, every state string **imported from** `baskfy_core.vbt` rather than retyped |
| Verified | Alembic's own `compare_metadata` over a scratch database: **0 `vb_` differences**; downgrade drops all twelve and upgrade restores them with the comparison still clean |
| Settings | `vbt_settings.py` — four editable fields, three ceilings, two system-owned, one audit row per field that moves |
| Flags | `BASKFY_VBT_EXECUTION_ENABLED=false`, `BASKFY_VBT_NIGHTLY_ENABLED=true`, in `.env.example`, the API, the worker and the desk's `config.py` |
| Tests | `services/api/tests/test_vbt_schema_and_settings.py` — **75 passed** (structural + database) |

**The safety property of this module**: `vb_config` is seeded with `sleeve_capital_inr = 0`, and
a test proves what that means — `size_entry` against a freshly seeded row returns quantity 0 with
`NO_SLEEVE_CAPITAL`. A newly seeded system detects, ranks, stores and plans **nothing to buy**
until a person writes the number (`02` §3.4).

**And what is deliberately absent**: `test_no_auto_execute_setting_exists_anywhere` scans both
settings classes for a `vbt_*auto*` field and finds none, and the desk's `config.py` carries the
reason in a comment above `VBT_EXECUTION_ENABLED`.

### What is NOT done at VB3

* **Nothing writes to these tables yet.** VB4 is the first module that inserts a row.
* Nothing is deployed and no box has run `0037`.
* The `vb_config` row exists only where `make seed` has run — the private `baskfy_vb_test`
  database this module's tests used, and nowhere else.
* `baskfy_vb3_check` and `baskfy_vb_test` are scratch databases in the local dev Postgres. They
  are this run's, and dropping them costs nothing.

---

## VB4 — The nightly job ✅ (10 Sep 2026)

`baskfy.vbt.detect(trade_date)` loads 260 sessions of the `04` §1 universe, drops the thin
sessions, computes the indicators, runs the scan and the six filters, measures the breadth, and
upserts one `vb_signal_daily` row per scan hit plus one `vb_breadth_daily` row per session.

| | |
|---|---|
| The step | `PipelineStep.COMPUTE_VBT`, **thirteenth**, in `POST_PUBLISH_STEPS`, wrapped by `run_compute_vbt_step` which cannot raise (DECISIONS-VB **VB4.1**) |
| The retry | Beat `vbt-detect` at **21:10 IST** weekdays — after the swing detect (21:00) and its plan (21:05). It counts the session's rows first and does nothing when the chain already wrote them (**VB4.2**) |
| The CLI | `make vbt DATE=2026-09-09 [SESSIONS=5] [FORCE=1]` — it prints the funnel, because "0 signals" and "0 signals out of 1,412 names with a 200-day average" are different answers |
| Storage precision | `COLUMN_PRECISION` extended: levels 2 dp, `close_position` and `pct_above_dma` **4** dp — rounding a number to the precision of its own threshold is how a rule starts disagreeing with the row that recorded it |
| Tests | `services/worker/tests/test_vbt_detect.py` — **22 passed** against a real database |

### The four claims the tests make

* **Twice changes nothing** (house rule 7), over the whole job rather than one insert.
* **A date with no bars writes nothing and says so** — `SKIPPED` with a reason, because "no
  signals" and "no data" look identical on a page and are opposite problems.
* **A split stores the exchange price**: an instrument whose stored series is twice what the
  exchange printed yields `limit_price` ₹48.00, `stop_price` ₹42.20 and a 200-day average that is
  also in today's money (**VB4.4**).
* **The step's failure leaves the run SUCCEEDED**, proven by making the detector raise.

Plus: a shut gate still writes the signals (the gate refuses *entries*; a gate that stopped the
detector would be measuring itself), a rejected scan hit is stored with the letters that failed
(`["E"]` for a 22% day), an ETF is not in the universe while GOLDIAM is, and the sleeve writes no
`sw_` row.

### What is NOT done at VB4

* **Nothing plans anything.** There is no `vb_plan`, no `vb_order`, no `vb_position` row and no
  way to make one. VB6 is the first module that builds a plan; VB7 is the first that expires an
  order.
* The job has never run against the plant's real bars — only against synthetic ones. The dev
  database holds ten sessions of history (`docs/swing/STATUS.md` SW0), so `make vbt` there
  detects nothing and says so.
* No page reads these rows.
* `vb_signal_daily.bars_in_window` is written as null: the count is computable and nothing reads
  it yet, and a column filled with a number no surface shows is a column that quietly rots.

---

## VB5 — The sleeve's cash and book ✅ (10 Sep 2026)

| | |
|---|---|
| The arithmetic | `packages/core/src/baskfy_core/vbt/sleeve.py` — pure, stated once, so the evening job, the desk page and the confirm path cannot each derive a slightly different equity |
| The loading | `services/api/src/baskfy_api/vbt_sleeve.py` — reads `vb_config`, `vb_position`, `vb_order` and `ohlcv_daily`, and **nothing else** |
| Tests | 16 without a database (`test_vbt_sleeve.py`) + 9 against one (`test_vbt_sleeve_db.py`) — **25 passed** |

```
cash            = capital + realised - cost of open
cash available  = cash - committed          ← what a new line may spend
equity          = cash + value of open      ← the slot is a tenth of this
open exposure   = value of open + committed
```

**The two claims the tests make.** A resting limit commits cash without spending it
(DECISIONS-VB **VB5.1**) — three limits at ₹1 lakh look like ₹10 lakh of cash to a fourth line
unless something subtracts them. And a holding the sleeve did not buy is invisible: the test
writes an instrument, a bar and a price of ₹500 for a name with no `vb_position` row, and the
sleeve's equity does not move.

### What is NOT done at VB5

* **Nothing builds a plan yet.** `load_sleeve` has no caller outside its tests; VB6 is the first.
* No position or order has ever been written by code — only by test fixtures.
* The **API tree's own database fixture is broken by another session's in-flight migration**
  (`0039_merge_duplicate_instruments` drops a unique constraint `seed_reference_fixture` upserts
  on). It is not this run's, and it is why VB5's database tests are in the worker tree
  (DECISIONS-VB **VB5.3**). A fresh session seeing `there is no unique or exclusion constraint
  matching the ON CONFLICT specification` should look there rather than at the `vb_` schema.

---

## VB6 — The desk plan and its confirm ✅ (10 Sep 2026)

Two halves, and neither can place anything on its own.

### The worker half — `baskfy.vbt.evening` and `baskfy.vbt.morning`

`services/worker/src/baskfy_worker/tasks/vbt_evening.py`. In order: expire the working limits
(VB7's sweep), manage the book with the session's bar and its 21-day EMA, list the naked stops,
then build the entries from the session's `SIGNAL` rows against the gate, the sleeve's own money
and the book — every name passed over recorded as a `vb_plan_skip`. Then the session row.

| | |
|---|---|
| Beat | `vbt-evening` **21:15 IST** (after the 21:10 detect), `vbt-morning` **09:00** |
| CLI | `make vbt-plan DATE=2026-09-09 [SOURCE=MORNING]` — it places nothing |
| Tests | `services/worker/tests/test_vbt_evening.py` — **26 passed** |

**Every line it writes is `PROPOSED`.** A test asserts that and that `vb_order` is still empty
after an evening: the job writes intentions, and an order needs a person.

### The desk half — `/vbt`, `/vbt/data`, `POST /vbt/execute`

`kite-momentum-rebalancer/app/vbt_execute.py` (the confirm) and `app/vbt_desk.py` (the store,
the view and the route), with `app/templates/vbt.html`.

| | |
|---|---|
| Tests | `tests/test_vbt_execute.py` **22 passed**, `tests/test_vbt_desk.py` **21 passed** |
| The gateway | Its own instance, its own journal (`vbt_orders_journal.jsonl`), its own band (0.5–15%), sharing the desk's one risk manager so this sleeve cannot spend a limit the weekly book already used |
| The evidence | Every execute test runs the **real** gateway over a broker client whose every method raises. **0 orders reach a broker.** |

The four refusals are tested through the route's own vocabulary: no confirm → **400**, unknown
plan → **404**, past thirty minutes → **410**, a line confirmed twice → **409**. And the two
Track C claims: a `SELL_AT_OPEN` for a name the sleeve does not own is `BLOCKED` with "did not
buy", and one for more than it owns is `BLOCKED` with the two numbers.

### Five decisions worth reading

`VB6.1` session counts are derived, not incremented — a test runs the evening three times and the
count is still two. `VB6.2` what counts as one of `02` §3.1's twenty DRY_RUN sessions.
`VB6.3` a sell without a live price goes and a buy does not. `VB6.4` the sweep produces a cancel
*line* and never cancels. `VB6.5` the page does not poll.

### What is NOT done at VB6

* **No order has ever been placed, simulated or otherwise, outside a test.** The desk page has
  never been opened against a database with `vb_` rows in it.
* The evening job has never run against the plant's real bars.
* `VBT_ORDER_PAST_EXPIRY` and the rest of `05` §4's alerts do not exist yet (VB7, VB8). *(Done at VB7 — see below.)*
* No web page reads any of this (VB8).
* The desk's `vbt.html` renders in the test suite's shape but has not been seen in a browser.

### Resume instructions for a fresh session

Read `/CLAUDE.md` → `docs/README.md` → `research/volume-breakout/STRATEGY.md` → `docs/vbt/README.md`
→ `02` → `06`, then start at VB1. `docs/vbt/04-business-rules.md` is the contract the tests
assert; `DECISIONS-VB.md` VB0.2 is the one thing that will otherwise be re-derived from scratch.

---

## VB7 — The working order and its expiry ✅ (10 Sep 2026)

The sweep itself shipped inside VB6's evening job (`sweep_expired_orders`), because a cancel line
that is not in the plan a person confirms is not a cancel. What VB7 adds is **the proof and the
alarm**: a property test that the window behaves, and four checks that say so when it did not.

### The proof

`packages/core/tests/test_vbt_expiry_property.py` — **9 tests**, hypothesis over random session
calendars with gaps of one to twelve days, so holidays and long weekends are in every case rather
than in a fixture someone chose:

* nothing fills on the **fourth** session, and nothing expires before its **third** has closed;
* a holiday consumes no session — the count is over published sessions, never over dates;
* a `SENT` or `PARTIAL` order ends `CANCELLED` (it is live at a broker) while a `PROPOSED` or
  `CONFIRMED` one ends `EXPIRED` (it never got there);
* the window is **parametrised over 1, 2, 3, 5 and 10**, so nothing in the suite compares against
  a literal three (DECISIONS-VB VB7.1). `04` §7.2 is the parameter with a cliff — two sessions
  returns 11.4% a year where three returns 18.2% — which is exactly why it is worth proving
  rather than sampling.

### The alarm

`services/worker/src/baskfy_worker/tasks/vbt_ops.py` — four checks, four Beat entries, one
runbook ([`docs/runbooks/08-vbt-evening.md`](../runbooks/08-vbt-evening.md)):

| Alert | When | Fires on |
|---|---|---|
| `VBT_DETECT_STALE` | 21:30 | no `vb_breadth_daily` row in four days — the detector did not run |
| `VBT_ORDER_PAST_EXPIRY` | 21:40 | a live limit past its `expires_after_session` — nobody confirmed the cancel |
| `VBT_POSITION_NAKED` | 21:40 | shares open, no `gtt_id` — the one state the method forbids |
| `VBT_POSITION_NO_BAR` | 21:40 | a held name silent for more than seven days (`04` §6.5) |

`services/worker/tests/test_vbt_ops.py` — **31 passed**. Every live order state can be late and
no terminal one ever is; another user's late order is not this sleeve's; a null window is not a
late order; each check is silent on a Saturday; and the last test runs all four against a book in
its worst state and asserts **the row counts do not move**. The checks tell; the desk fixes.

### What is NOT done at VB7

* **`AlertName.VBT_EVENING` — the nightly digest email `05` §4 describes — does not exist.** The
  four condition alerts do. Nobody gets a summary of a quiet, correct evening, and for the paper
  run that is arguably right; it is still a gap against the spec, recorded here rather than
  quietly dropped.
* No alert has ever been delivered outside a test. No sink is configured on any box this run
  touched, and `dispatch` says so once per process when none is.
* Runbook 8 carries `**Verified against:** NOT YET`, like the seven before it. Every command in
  it is written from the code it drives and should be assumed wrong in some detail.
* **A VB3 miss, found at VB7 and fixed here:** `packages/core/tests/test_schema_matches_docs.py`
  keeps a hand-written list of every table in the model, and the twelve `vb_` tables were never
  added to it — so `test_no_undocumented_tables` had been red since VB3 in the *full* core run
  (the VB modules ran their own files). The twelve are now listed with their primary keys, and a
  `test_vbt_tables_are_recorded_in_docs` asserts `03` names each one, matching the swing book's.
  The lesson is the boring one: run the whole suite, not the files you touched.
* The sweep has still never run against real bars, so the expiry path's evidence is entirely
  synthetic — 761 reproduced trades' worth of it in the backtest, and none of it from the desk.

---

## VB8 — The pages ✅ (10 Sep 2026)

### The API

`services/api/src/baskfy_api/vbt.py` (the read layer) and `routers/vbt.py` (six paths). Every
route resolves the caller through `scoped_sole_user_id`, so a principal who is not the sole
tenant is **refused** rather than served somebody else's positions.

| | |
|---|---|
| Tests | `test_api_vbt.py` **21 passed**, `test_vbt_readonly.py` **11 passed**, `test_api_vbt_benchmark.py` **1 passed** |
| Budget | `GET /vbt/today` p95 **140.7 ms** (median 92 ms) over 2,500 instruments each with a signal row — the route's worst case, not a typical evening. Budget is 300 ms |
| The one write | `PATCH /vbt/config`, four numbers, bounded by server ceilings. It cannot name `dry_run_sessions` or `first_live_sessions_left`, and an unknown field is **refused**, not ignored |

### The pages

`apps/web/src/app/(app)/vbt/` — Today, Positions and Backtest, plus `lib/vbt/fetch.ts`.
**20 tests**, and the read-only assertion is stronger than the swing hub's: this tree has *no*
`use server` module, renders *no* form, and names no execution package, broker or order-shaped
verb. There was nothing to make read-only, because nothing here writes (DECISIONS-VB VB8.4).

The four things the acceptance criteria asked a rendered DOM to prove are each a test: the funnel
line at zero candidates, the gate badge in both states with the SHUT copy saying the positions
are still managed, the caveats **above** the numbers (asserted by document order, because the
placement is the claim), and the fill-rate line.

### The number that changed

**`04` §7.3 had carried an invented 91% modelled fill rate since VB0.** VB8 measured it:
`BacktestResult.orders_offered` counts the orders that actually reached a fill test, and 761 of
908 filled — **83.8%**. The document is corrected, `baskfy_core.vbt.published` carries the study's
numbers as a checked transcription, and `test_vbt_goldens.py` asserts every one of them against
`out/final_metrics.json`. DECISIONS-VB VB8.1 explains why the denominator is offers and not
signals (over all 5,954 working orders the rate is 12.8%, which measures the slot count rather
than the market).

### Two red tests that were not this run's, fixed here

* `test_api_artifacts.py::test_nothing_undocumented_is_exposed` had been red since SC6 shipped
  `/explore/{slug}/constituents` (9 Sep) without a `docs/07` entry. Added.
* `test_schema_matches_docs.py::test_no_undocumented_tables` was VB3's own miss, found at VB7.

### What is NOT done at VB8

* **The pages have never been rendered in a browser.** They pass in jsdom against mocked reads;
  no `pnpm dev` was run, no screenshot was taken, and nobody has looked at them.
* **The per-row Dismiss note of `05` §2 does not exist** and no table backs it (VB8.4).
* `distance_to_ema_pct` and `ema_21` on an open position come back **null**: the read layer does
  not recompute indicators, and `vb_position` does not store them. The column renders an em dash.
  `05` §2 asks for "the 21-EMA and today's distance to it"; that is a gap, not an oversight, and
  VB9's re-run is the natural place to fill it.
* The backtest page has **no equity curve and no yearly table** — `05` §2 asks for both. They
  need `vb_backtest_run.stats` to carry the series, which VB9 writes. The page renders the
  headline comparison, the drift banner and the two halves.
* The generated API artefacts carry a concurrent session's field changes (VB8.3).

---

## VB9 — The backtest on the page ✅ (10 Sep 2026)

### What it does

`baskfy_worker.tasks.vbt_backtest` loads the universe and its bars from the plant, detects once,
and runs `04` §11's engine three ways — `full`, `gate_off`, `raw_scan` — appending **one**
`vb_backtest_run` row with `params` written on the way in and `stats`, `drift` or `error` on the
way out. `tools/vbt/backtest.py` does the same over the research export without a database, for
the moments the question is "did my change move a number".

| | |
|---|---|
| Tests | `services/worker/tests/test_vbt_backtest.py` **12 passed**, plus 22 in the API's |
| The planted year | The same trade `test_vbt_backtest.py` works by hand, driven through the loader, the indicators, the detector and the job — its return reproduces to **2 dp** |
| Append-only | A second run appends and edits nothing; a run that raises stores its `error`, sets `finished_at`, and the page still reads the last good number |

### The three books, measured on the export (10 Sep 2026)

| book | CAGR | max drawdown | trades | fill rate |
|---|---|---|---|---|
| `full` | 18.23% | −27.94% | 761 | 83.8% |
| `gate_off` | 18.48% | −48.78% | 1,037 | 84.2% |
| `raw_scan` | 0.76% | −48.10% | 958 | 84.5% |

**The gate costs a quarter of a CAGR point and buys 21 points of drawdown.** That is the whole
argument for it, and it is invisible in the CAGR column — which is why `/vbt/backtest` prints the
contributions with a sentence saying to read the drawdowns before reading either number.

### A VB8 bug found by writing VB9's failure test first

`baskfy_api.vbt.backtests` filtered on `finished_at IS NOT NULL` alone. A **failed** run sets
`finished_at` too (`03` §8), so a re-run that raised would have replaced a real result with a
card of blanks — the exact thing an append-only table exists to prevent. Both the API query and
the worker's `latest_finished` now require `stats IS NOT NULL`, with a test that seeds one good
run and one failed one. VB8 shipped it because its fixtures only ever contained runs that worked
(DECISIONS-VB VB9.3).

### What is NOT done at VB9

* **The full-history run has never been executed against the plant.** The engine does all three
  books in 23 seconds from the export; the `ohlcv_daily` read for 4,186 names over nine years is
  unmeasured, because no database this run could reach holds that history. VB9's "< 30 minutes"
  acceptance is therefore **not met, only made likely** — `NEEDS-MAULIK.md` V6 is the one command
  that settles it (VB9.4).
* No `vb_backtest_run` row exists anywhere outside a test database.
* The backtest task is **on demand only** — no Beat entry. The answer moves only when the bars or
  the code do, and a multi-minute compute job in the nightly window would compete with the chain.
* The monthly table of `04` §11 is not stored. The yearly table and the equity curve are.

