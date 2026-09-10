# 06 — Module plan: VB0–VB10

One commit per module, `VB<N>: green — <one line>`. A module is green when its acceptance
criteria pass **as tests**, `make lint` is clean in the touched trees, both existing suites still
pass, and `STATUS.md` + (if judgement was exercised) `DECISIONS-VB.md` are updated. Criteria are
proxies for Goals — the charter's precedence order applies, and a criterion that over-reaches is
scoped with a recorded reason rather than obeyed literally.

**Dependencies:** VB0 → VB1 → VB2 → VB3 → VB4 → VB5 → VB6 → VB7 → VB8 → VB9 → VB10.
VB2 needs only VB1. VB9 needs VB3 and VB2's engine.

**Standing rules for every module of this run**

* `DRY_RUN=true` in every environment the run creates; `BASKFY_VBT_EXECUTION_ENABLED` stays
  false and is never flipped by an agent.
* Every threshold is a field of `baskfy_core.vbt.config` — never a literal in a detector, a task,
  a router or a page. A test scans for literals.
* Tests assert `04`, never current behaviour. **Never weaken a test to make a module pass.**
* The desk must be able to rebalance on any Friday, and the swing book must still run on any
  morning. No module ends with either tree's suite broken.
* Deploy nothing.

---

### VB0 — The pack ✅

**Goal:** a fresh session can build this sleeve without reading the research code, and cannot
break either existing product while doing it.

- `docs/vbt/` written: this file and the seven beside it.
- `NEEDS-MAULIK.md` gains a **VBT** heading with the two things known at write time: the plant's
  262 missing instrument-days and six thin sessions, and the sleeve's capital and risk.
- `QUESTIONS.md` carries a standing default for everything only Maulik can answer, so no module
  waits.
- **AC:** a reader with no other context can name the tables, the thresholds, the tracks and the
  gate from this folder alone; every threshold in `04` appears with its value.

---

### VB1 — The pure core

**Goal:** the arithmetic of VBT-1 exists as DataFrames-in / DataFrames-out, and touches nothing.

`packages/core/src/baskfy_core/vbt/`:

| Module | What |
|---|---|
| `config.py` | `ScanConfig`, `TrendConfig`, `BreadthConfig`, `EntryConfig`, `SizingConfig`, `ExitConfig`, `CostConfig`, `DataConfig`, `VbtConfig`, `DEFAULT_VBT_CONFIG` — every field of `04` with its default and a docstring saying why |
| `calendar.py` | `drop_thin_sessions`, `rolling_min_periods` (`04` §2) |
| `indicators.py` | `with_vbt_indicators`, `REQUIRED_COLUMNS`, `require_columns` (`04` §1) — Polars, `.over("instrument_id")`, the `baskfy_core.factors` shape |
| `signals.py` | `chartink_scan`, `trend_filters`, `detect_signals` → the `SIGNAL` / `SCAN_ONLY` frame with `failed_filters` (`04` §3) |
| `breadth.py` | `breadth_above_dma` → `(universe_count, measured_count, above_count, pct_above_dma, gate)` (`04` §4) |
| `sizing.py` | `size_position` with named caps and refusals; `Sized` / `Refused` results (`04` §5) |
| `exits.py` | `initial_stop`, `apply_stop`, `manage` → `Hold` / `QueueEmaExit` / `StopHit` / `NoBar` (`04` §6) |
| `orders.py` | `WorkingOrder`, `place_order`, `advance_session`, `expire_orders`, `fill_if_touched` (`04` §7) |
| `plan.py` | `build_entries`, `exit_lines`, `assemble`, `VbtPlan.plan_hash()` (`04` §9) |

Money is `Decimal` throughout; prices are quantized at the tick (₹0.05 for levels the desk sends,
₹0.01 where an exchange print is stored) at the moment they leave a function, not before.

- **AC:**
  1. `packages/core/tests/test_vbt_purity.py` green — no `sqlalchemy` / `httpx` / `requests` /
     `redis` / `celery` / `kiteconnect` / `os` / `pathlib` / `socket` / `subprocess` / `asyncio`
     import, no `.now()` / `.today()` / `.utcnow()`, no `open("…")`, no `baskfy_execution`, no
     `kite_client` anywhere in the package (the `test_swing_purity.py` scan, extended).
  2. `test_vbt_no_literals.py`: no numeric literal from `04` appears in `signals.py`,
     `breadth.py`, `sizing.py`, `exits.py`, `orders.py` or `plan.py` — they read `config`.
  3. `test_vbt_docs_parity.py`: every field name in `baskfy_core.vbt.config` appears in
     `docs/vbt/04-business-rules.md`, and every `**bold**` value in `04`'s tables matches the
     field's default. A renamed field or a changed number without a doc edit fails.
  4. `test_no_escape_hatches.py` still green (no `# type: ignore`, no `Any`, no swallowed
     exception), `ruff` clean, `mypy --strict` clean.
  5. Unit tests per rule: the five lines' comparison senses, A–F including the null-is-a-fail
     rule, `close_position` at `high == low`, the thin-session rule on a synthetic panel, the
     10% tolerance at exactly 45 of 50 bars, the sizing caps one at a time with `cap` naming the
     binder, `manage`'s precedence, and `expire_orders` counting sessions and not days.

---

### VB2 — The goldens: reproduce the study

**Goal:** the core is the study. Where it is not, the difference is named and explained, never
papered over.

- `baskfy_core/vbt/backtest.py`: the engine of `04` §11, walking the VB1 functions.
- `tools/vbt/reproduce.py`: loads `research/volume-breakout/aws/*.csv.gz` (read-only; the export
  is not re-generated), builds the panel through the core's own universe and calendar rules, runs
  the engine, and compares against `research/volume-breakout/out/final_trades.csv` and
  `out/final_metrics.json`.
- The comparison is a **test**, `packages/core/tests/test_vbt_goldens.py`, skipped with a loud
  reason when the export is absent (it is gitignored) and run in full when it is present.
- A **small committed golden**: the first 40 and last 40 trades of `final_trades.csv` plus the
  metrics JSON, checked into `packages/core/tests/data/vbt/`, so the module has a regression test
  that runs everywhere.

- **AC:**
  1. Signal count over 2017-01-02 → 2026-09-09 is **6,293**, and the raw scan is **32,929**.
  2. The breadth series agrees with `out/breadth200.csv` to within one name in the numerator, and
     **the gate's verdict at 40% is identical on every one of the 2,396 sessions**. (The first
     draft of this criterion asked for 1e-12 on the values; the measurement in `04` §4.6 shows
     why that is the wrong question and what the right one is.)
  3. `drop_thin_sessions` finds **exactly the six dates** of `04` §2.1 over the plant's history —
     found by the rule, not by a list.
  4. Every one of the **761** trades matches on `(symbol, entry_date, exit_date, quantity,
     reason)`, and entry and exit prices agree to **₹0.01**.
  5. The metrics agree: CAGR **18.2%**, max drawdown **−27.9%**, trades **761**, win rate
     **37.8%**, profit factor **1.55**, average hold **18.3** sessions, exposure **63.2%** — each
     to the precision the study stored it at — and the **whole yearly table** of `01` §4
     reproduces to one decimal.
  6. The sensitivity neighbourhood reproduces to a tenth of a point: entry window 2 / 3 / 5
     sessions → **11.4 / 18.2 / 17.1%**; stop 10 / 12 / 15 → **16.4 / 18.2 / 16.6%**; slots
     8 / 10 / 15 → **15.5 / 18.2 / 12.9%**; gate 30 / 40 / 45 / 50 → **15.6 / 18.2 / 15.4 /
     10.9%**; no gate → **18.5% at −48.8%**; raw scan same execution → **0.8%**. The gate at 35%
     is the one case that does not, by 0.2 points, for the reason `04` §4.6 measures.
  7. Any residual difference is a numbered `DECISIONS-VB.md` entry stating the size of the
     difference, its cause, and whether it changes a decision.

---

### VB3 — Schema and settings

**Goal:** the `vb_` schema of `03`, migrated, seeded, idempotent, with the ceiling boundary intact.

- Migration `0036_vbt.py` (re-check the head first): every table in `03` §1–§8, indexes, FKs,
  `user_id` everywhere, the `SkipReason` and state check-constraints.
- Models in `packages/core/src/baskfy_core/models/vbt.py`; `vb_config` seeded for
  `BASKFY_SOLE_USER_ID` with `sleeve_capital_inr = 0` and the `04` defaults.
- `BASKFY_VBT_EXECUTION_ENABLED`, `BASKFY_VBT_NIGHTLY_ENABLED`,
  `BASKFY_VBT_MAX_OPEN_POSITIONS_MAX`, `BASKFY_VBT_MAX_POSITION_PCT_MAX`,
  `BASKFY_VBT_STOP_PCT_MAX` in `.env.example` as **system-only**, read by
  `baskfy_worker.settings`, `baskfy_api.settings` and the desk's `config.py`.
- A `VbtSettings` Pydantic spec in the desk's `analytics/settings.py` pattern: validation against
  the ceilings, `vb_config_audit` written in the same transaction.

- **AC:** migrate → seed → migrate again is a no-op; downgrade to `0035` and back leaves the same
  schema; a `max_position_pct` above the ceiling is a **422 naming the ceiling**; every `vb_`
  table has `user_id`; a ceiling can never become a form field (the
  `test_settings_boundary.py` pattern, extended); `vb_backtest_run` refuses an UPDATE in a test.

---

### VB4 — The nightly job

**Goal:** every trading session, `vb_signal_daily` and `vb_breadth_daily` are written from the
published bars, and the numbers on them are exchange prices.

- Worker task `baskfy.vbt.detect(trade_date)`: load `bars_required` sessions of `ohlcv_daily` ⋈
  the `04` §1 universe, drop thin sessions, `with_vbt_indicators`, `detect_signals`,
  `breadth_above_dma`, convert levels by `adj_factor`, round with `apply_storage_precision`
  (extend `COLUMN_PRECISION` for the new columns), upsert idempotently, write the funnel into
  `vb_breadth_daily.detail`.
- A new `PipelineStep.COMPUTE_VBT` **after `COMPUTE_SWING`**, added to `POST_PUBLISH_STEPS` and
  wrapped by `run_compute_vbt_step`, which — like `run_compute_swing_step` — **cannot raise**.
- A **21:00 IST retry** Beat entry (`vbt-detect`), for the night the chain has not published by
  the time it runs; it is a no-op when the session already has rows.
- CLI `make vbt DATE=…`.

- **AC:** running the task twice for a date changes no rows; a date with no published bars writes
  nothing and says so in the step's `detail`; a synthetic split (`adj_factor ≠ 1`) yields a stored
  `limit_price` equal to the raw price; **the step's failure leaves the run `SUCCEEDED`** (a test
  that makes the detector raise); the 21:00 retry after a successful nightly writes nothing new;
  a look-ahead test shifting the breadth series by one session changes the gate by exactly one
  session; neither `compute_swing` nor any earlier step is touched (a test asserts the chain's
  order and that `COMPUTE_SWING` still precedes).

---

### VB5 — The sleeve's cash and book

**Goal:** the sleeve sizes against **its own** money and owns **only what it bought**.

- `baskfy_api` / `baskfy_worker` service `vbt_sleeve.py`: `sleeve_equity(user_id, as_of)` =
  `vb_config.sleeve_capital_inr` + realised P&L of `CLOSED` positions + marked value of `OPEN`
  ones (the mark is the latest `ohlcv_daily.close` on or before the session), and
  `cash_available` = equity − the value of open positions and working orders at their limits.
- `04` §5's caps wired: the 1%-of-turnover cap and the ₹10,000 floor.
- The sleeve is a `MY_STRATEGY` capital portfolio in the M34 / PORTFOLIO_REDESIGN sense, exactly
  as the swing sleeve is; it never reads the account's total holdings.

- **AC:** a fixture account holding a name the sleeve never bought produces **no** VBT sell line
  for it and the name is invisible to `sleeve_equity`; a sleeve at ₹0 plans nothing and every
  signal is skipped `NO_SLEEVE_CAPITAL`; the turnover cap binds at ₹1 crore in a fixture and does
  not at ₹10 lakh (STRATEGY §3's claim, as a test); a position marked on a session with no bar
  falls back to the last close and says so.

---

### VB6 — The desk plan and `/vbt/execute` (DRY_RUN)

**Goal:** one click confirms; the GTT is armed; nothing fires without the click.

- `baskfy.vbt.evening(trade_date)`: `manage` over open positions → `SELL_AT_OPEN`;
  `expire_orders` → `CANCEL_LIMIT`; `build_entries` → `PLACE_LIMIT`; write `vb_plan` (source
  `EVENING`) with its lines and skips; `AlertName.VBT_EVENING` email per `05` §4; close the
  `vb_session` row and advance `dry_run_sessions` / `first_live_sessions_left`.
- `baskfy.vbt.morning`: rebuild the same plan as `MORNING` before the open, re-sized.
- Desk route `GET /vbt` per `05` §3 and `POST /vbt/execute {plan_id, line_id, confirm}`:
  validate `confirm=true` and the 30-minute expiry; re-derive and re-size under a row lock on the
  day's `vb_session`; then `PLACE_LIMIT` → `OrderGateway.place(BUY, CNC, LIMIT, price)`;
  `SELL_AT_OPEN` → `place(SELL, CNC, MARKET)` for at most `quantity_open`; `CANCEL_LIMIT` →
  the guarded `cancel_order`; `ARM_GTT` → `place_gtt_stop` with `StopBand(0.005, 0.15)` and
  `limit_fraction=VBT_GTT_LIMIT_FRACTION` [0.97].
- Fills come back through the desk's existing `on_order_update` handler into `vb_fill`,
  `vb_position` and the GTT.

- **AC:** the desk suite's non-negotiable tests still green; new tests: an expired plan → **410**,
  a missing `confirm` → **400**, a SELL for more than `quantity_open` → `BLOCKED`, a SELL for a
  symbol not in `vb_position` → `BLOCKED`, a second confirm of the same line → the idempotency
  key refuses a double-send, a fourth `PLACE_LIMIT` confirm in one session → `BLOCKED
  (SESSION_CAP)`, every fill arms a GTT in the same request, and with the flag false **`place` is
  called only on the dry-run adapter** (asserted by a spy) — **0 orders reach a broker** in the
  whole suite.

---

### VB7 — The working order and its expiry

**Goal:** the one thing the desk lacks today: a limit that stops being an order after three
sessions, and does so at the exchange, not just in the database.

Modelled on the swing book's pending-entry machinery (`pending_cutoff_at`, the 10:45 sweep), but
its own: this one counts sessions, not minutes.

- `baskfy.vbt.expire_orders(trade_date)`, run **inside the evening job**, before the plan is
  built, so the session's `CANCEL_LIMIT` lines are in the plan a person confirms.
- `advance_session` increments `vb_order.sessions_worked` once per **published** session
  (idempotent on the date), so a re-run, a holiday and a restart change nothing.
- A `SENT` order at the end of its third session produces a `CANCEL_LIMIT` line; on confirm the
  gateway's `cancel_order` runs and the row goes `CANCELLED (EXPIRY_SWEEP)`. A `PROPOSED` or
  `CONFIRMED` order that never reached the broker goes straight to `EXPIRED`.
- A partially filled order is cancelled for its remainder and its position stands (`04` §7.5).
- `VBT_ORDER_PAST_EXPIRY` alert for a working order still live after its window.

- **AC:** a property test over random session sequences — no order ever fills on its fourth
  session, and no order expires before its third has closed; a holiday between the signal and the
  expiry does not consume a session; running the sweep twice for one date cancels once; a
  partially filled order at expiry leaves `filled_quantity` shares in `vb_position` and cancels
  exactly the remainder; the alert fires in the test harness.

---

### VB8 — The pages

**Goal:** Maulik opens the web app and sees what the sleeve is doing; he opens the desk and
confirms it.

- API router `services/api/.../routers/vbt.py`: `GET /vbt/today`, `GET /vbt/book`,
  `GET /vbt/breadth?from&to`, `GET /vbt/backtest`, `GET /vbt/config`, `PATCH /vbt/config`
  (validated as VB3). OpenAPI and `packages/api-client` regenerated.
- Pages `/vbt`, `/vbt/book`, `/vbt/backtest` per `05` §2; the desk page per `05` §3.

- **AC:** the read-only test of `05` §2 exists and is green (only `noteAdd` / `noteDismiss`, no
  execution import); `GET /vbt/today` p95 < 300 ms on the dev stack (the `market/mood` budget); a
  rendered-DOM test shows the funnel line at zero candidates, the gate badge in both states, the
  caveats component above the numbers, and the fill-rate line; the desk page renders a plan with
  one line of each kind and a locked-circuit line with no button.

---

### VB9 — The backtest on the page

**Goal:** a number, from the plant's own bars, beside the study's, with its drift named.

- `tools/vbt/backtest.py` and a `baskfy.vbt.backtest` task on the compute queue: the `04` §11
  engine over the plant's `ohlcv_daily` from 2017, appended to `vb_backtest_run` with
  `source=PLANT`; `params` written on the way in, `error` on the way out.
- The drift computation of `03` §8 and the banner of `05` §2, flagged above **1.0 CAGR point**.
- The three books (`full`, `gate_off`, `raw_scan`) reported over one detection pass.

- **AC:** the run over the full history completes on the dev box in **< 30 minutes**; a fixture
  year with a planted signal reproduces the planted trade's return to 2 dp; the card shows the
  caveats verbatim from `01` §5 and the parameters that produced the number; a second run appends
  a row and **does not edit** the first; a run that raises stores its `error`, sets `finished_at`
  and leaves the last good number on the page.

---

### VB10 — Safety, and the claims become theorems

**Goal:** every Track-B and Track-C claim of `02` is a test, not an intention.

- With `BASKFY_VBT_EXECUTION_ENABLED=false`, **no VBT code path reaches
  `OrderGateway.place` / `place_gtt_stop` on a non-dry-run adapter** — a spy over the real
  gateway, under both values of `DRY_RUN`.
- **No auto-execute exists**: a source scan asserts no `BASKFY_VBT_AUTO_EXECUTE`-shaped setting,
  no scheduler entry that calls `/vbt/execute`, and that every order-producing path is reached
  only from a request carrying `confirm=true`.
- `apps/web` under `/vbt` imports no execution and calls no `/desk/*` or `/vbt/execute` (source
  scan with comments and docstrings stripped, the swing pattern).
- A property test over random books: a SELL never exceeds what the sleeve owns; a stop never
  falls; no line is ever produced for a symbol the sleeve does not hold.
- Every `vb_` write carries `BASKFY_SOLE_USER_ID`.
- **The neighbours are untouched**: the desk's weekly book and R1–R4 (`test_regime_names_do_not_collide.py`
  extended), `baskfy_core.swing` byte-identical to its state at the run's start (a hash test), and
  the swing suite and the desk suite green.
- `tools/vbt/drill.py`: the whole DRY_RUN session end to end against Postgres — detect → evening
  plan → confirm one of each line kind → simulated fills and GTTs → the next morning's plan →
  the third session's expiry sweep — printing the `vb_session` counters and **0 orders reaching a
  broker**.

- **AC:** every test above green; the drill exits 0 and prints its counters; `VB-FINAL-REPORT.md`
  written at the repo root in the style of `SW-FINAL-REPORT.md`; `NEEDS-MAULIK.md` § VBT reduced
  to what only his hands can supply; `STATUS.md` all-green or saying exactly what is not.
