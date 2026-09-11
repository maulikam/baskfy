# 06 — Module plan: TW0–TW10

Eleven modules, in order, under the Autonomy charter in `/CLAUDE.md`: run long, decide-record-
continue, questions to Maulik are the exception. **One commit per module**, message
`TW<N>: green — <one line of what changed>`. No module ends with either tree's test suite broken.
`STATUS.md` is updated at the end of every module and is loud about what is NOT done.

**Acceptance criteria are tests.** An `AC` line that cannot be run is a wish; where an AC is prose
it names the file that has to say the thing and a test asserts the file says it. Never weaken a
test to make a module pass — if a criterion looks wrong, settle it by the charter's precedence
order, record it in `DECISIONS-TW.md`, continue.

The gates files for this run are `gates/twt-root.md` and `gates/twt-0.md` … `gates/twt-10.md`; a
module is finished when its gates file is fully checked **with evidence**, not when it feels done.

---

### TW0 — The pack ✅

**Goal:** a fresh session can build this sleeve without reading the research code, and cannot
accidentally build a different one.

- `docs/twt/`: this file plus README, `01`–`05`, `STATUS.md`, `DECISIONS-TW.md`, `QUESTIONS.md`.
- Every threshold of `01` restated in `04` as a named field with its value.
- `02` §3's real-money gate rewritten to Maulik's 11 Sep decision: **no DRY_RUN session count** — a
  green tree, the written runbook, his own flag flip, and half size for the first ten live entries.
- `NEEDS-MAULIK.md` gains a **TWT** heading with the three things known already.
- `docs/README.md` names the run beside the other four.
- The one correction the research note needs: STRATEGY §3's "ranked by 20-day turnover" against the
  code's signal-day turnover (DECISIONS-TW TW0.2). Fix the stale half, cite the code.

- **AC:** `gates/twt-0.md` fully checked — ten files present; the thirteen named thresholds all
  carry their values; `02` §3 contains no session-count gate and does contain the four conditions;
  `03` names `0041_twt` and `0041` is free; `06` plans TW1–TW10 with a Goal and an AC each;
  `QUESTIONS.md` carries the four defaults; `NEEDS-MAULIK.md` has the TWT heading; no document
  sets a flag true; the commit exists and touches no code.

---

### TW1 — The pure core

**Goal:** the arithmetic of TWT-1 exists as DataFrames-in / DataFrames-out, and touches nothing.

- `packages/core/src/baskfy_core/twt/`: `config.py` (`04`'s every field), `calendar.py` (§2's thin
  sessions — shared with VBT-1's implementation, TWT's own thresholds), `indicators.py`
  (`vol_sma`, `turnover`, `turnover_avg_20`, `sma_dma`, and the weekly/monthly columns),
  `signals.py` (`weekly_closes`, `month_low_back`, `tight_state`, `entry_events`,
  `with_twt_columns`), `breadth.py` (§4.4's delegation), `sizing.py`, `exits.py` (the two stops and
  the ratchet), `plan.py`, `sleeve.py`, `__init__.py`.
- **Polars**, `Decimal` for money and levels, no float in a price path. Law 1: no database, no
  network, no disk, no clock.
- `04` §2.1's thin sessions and §2.2's 10 % tolerance are tests, not comments.
- The breadth series is **shared with VBT-1** — `baskfy_core.vbt.breadth` is called, not copied,
  with TWT's own spelled-out thresholds (`04` §4.4).

- **AC:** `test_twt_purity.py` proves the package imports no database, network, disk or clock —
  the swing and VBT pattern, extended; `test_twt_signals.py` asserts §3.1's five lines with their
  senses, §3.2's three weekly closes on a fixture that spans a year boundary (ISO week 1 of the
  next year is *after* week 52, and `searchsorted` on `year × 100 + week` must not sort it before),
  §3.3's month-3 low across a December→March boundary, and §3.4's entry event including the
  fresh-listing case of `04` §3.4; `test_twt_exits.py` asserts §7.1, §7.2's clamp to the tick and
  §7.4's fill-day rule; `test_twt_sizing.py` asserts every cap of §6.2 in order and §6.4's half
  size; `test_twt_breadth.py` asserts the gate is strict and that a zero denominator is SHUT;
  `test_twt_no_literals.py` finds no threshold written as a number outside `config.py`;
  `test_no_escape_hatches.py` still green; `make lint` clean (`ruff`, `ruff format`, `mypy
  --strict`).

---

### TW2 — The goldens: reproduce the study

**Goal:** the core is the study. Where it is not, the difference is named and explained, never
averaged away.

- `tools/twt/goldens.py` runs `baskfy_core.twt.backtest` over the **research panel**
  (`research/volume-breakout/data/panel.pkl` via a read-only loader that converts it to the same
  frame the plant produces) with `EntryConfig.research_min_turnover_inr` [₹2 crore] and the rest of
  `04`'s defaults, and compares against `research/tight-close/out/final_trades.csv` (164 trades)
  and `out/final_metrics.json` (CAGR 20.92, max DD −24.7, trades 164, win rate 40.9, profit factor
  2.71, avg hold 104.6).
- Fixtures small enough to live in the repository: the golden **trade list** and the **metrics**
  are committed; the panel is not.
- The recall test: **point-in-time reading 64.9 % recall at 61.5 % precision** against
  `chartink_backtest.csv`, **look-ahead reading 83.1 % at 97.8 %** — the measurement that proves
  the live signal is the point-in-time one. `tight_state_lookahead` lives in the test module only
  (`04` §3.2).
- Every difference from the research goes in `DECISIONS-TW.md` with its cause and its size. A
  difference that cannot be explained is a **blocker for TW9**, not a footnote.

- **AC:** `test_twt_goldens.py` reproduces the trade list to the tick, or asserts an explained
  delta whose entry exists in `DECISIONS-TW.md` and whose size is inside a named tolerance; the
  metrics match to the precision the research printed; `test_twt_lookahead_recall.py` reproduces
  both recall numbers to ±0.5 pt and asserts the look-ahead reading is **not reachable** from
  `baskfy_core.twt` (an import test).

---

### TW3 — Schema and settings

**Goal:** the `tw_` schema of `03`, migrated, seeded, idempotent, with the ceiling boundary intact.

- Alembic `0041_twt.py` revising `0040_vbt_scan_run`, with a downgrade. Thirteen tables of `03`,
  every check constraint (`tw_position.stop_price` never falls, `tw_plan_skip.reason` in the enum,
  `tw_order.side` in `{BUY, SELL}`).
- `packages/core/src/baskfy_core/models/twt.py`, in the `models/base.py` types.
- Seed: one `tw_config` row per sole user with **`sleeve_capital_inr = 0`** and the `04` defaults.
- The ceilings of `02` as system-only env in `.env.example`, `BASKFY_TWT_TRAIL_PCT_MIN` among them
  and bounded in the **other direction**.

- **AC:** `make migrate` then `make downgrade` then `make migrate` leaves the same schema;
  `test_twt_schema.py` (db-marked) asserts every column of `03` with its type and every constraint;
  a settings write above a ceiling — or **below** `BASKFY_TWT_TRAIL_PCT_MIN` — is refused with the
  ceiling named; the seed is idempotent (house rule 7); `test_schema_matches_docs.py` extended to
  `03`.

---

### TW4 — The nightly job

**Goal:** every trading session, `tw_state_daily`, `tw_signal_daily` and `tw_breadth_daily` are
written from the published bars, and every open position knows tomorrow's trigger.

- Worker task `baskfy.twt.detect(trade_date)`: load `bars_required` [260] sessions of
  `ohlcv_daily` ⋈ the `04` §1 universe, drop thin sessions, `with_twt_columns`, `tight_state`,
  `entry_events`, `breadth_above_dma`, convert levels by `adj_factor`, round with
  `apply_storage_precision` (extend `COLUMN_PRECISION`), upsert idempotently, write the funnel into
  `tw_breadth_daily.detail`.
- **The ratchet's arithmetic, the night before.** For every `OPEN` position: raise `high_since`
  with the session's high, compute `next_trigger` per `04` §7.2 (tick-floored, clamped under the
  close), store it with `next_trigger_for`. §7.3's corporate-action branch, including the
  **refusal** to emit a lower trigger.
- A new `PipelineStep.COMPUTE_TWT` **after `COMPUTE_VBT`**, added to `POST_PUBLISH_STEPS` and
  wrapped by `run_compute_twt_step`, which — like its two siblings — **cannot raise**.
- A **21:00 IST retry** Beat entry (`twt-detect`), a no-op when the session already has rows.
- CLI `make twt DATE=…`.

- **AC:** running the task twice for a date changes no rows; a date with no published bars writes
  nothing and says so in the step's `detail`; a synthetic split (`adj_factor ≠ 1`) yields a stored
  `entry_reference_close` equal to the raw price **and** a `next_trigger` that is not lower than
  the resting one (§7.3); the step's failure leaves the run `SUCCEEDED` (a test that makes the
  detector raise); the 21:00 retry after a successful nightly writes nothing new; **a look-ahead
  test shifting the panel by one session moves every gate, signal and trigger by exactly one**;
  `COMPUTE_SWING` and `COMPUTE_VBT` still precede and neither is touched (a test asserts the
  chain's order); `high_since` agrees with a recomputation from `ohlcv_daily` over the hold.

---

### TW5 — The sleeve's cash and book

**Goal:** the sleeve sizes against **its own** money, owns **only what it bought**, and counts its
first ten live entries.

- `baskfy_api` / `baskfy_worker` service `twt_sleeve.py`: `sleeve_equity(user_id, as_of)` and
  `cash_available` per `04` §9.
- `04` §6's caps wired, including the 1 %-of-turnover cap and the ₹10,000 floor.
- **The half-size counter**: `tw_config.first_live_entries_left`, decremented once per **filled**
  entry by the session that filled it, recorded on `tw_position.half_size` and in
  `tw_session.first_live_entries_counted`. A counter in the sleeve, **not a flag** — nothing about
  it is a switch somebody can turn off.
- The sleeve is a `MY_STRATEGY` capital portfolio, as the swing and VBT sleeves are.

- **AC:** a fixture account holding a name the sleeve never bought produces **no** TWT line for it
  and the name is invisible to `sleeve_equity`; a sleeve at ₹0 plans nothing and every signal is
  skipped `NO_SLEEVE_CAPITAL`; the turnover cap binds on a ₹2 crore name at ₹25 lakh and does not
  on a ₹50 crore one (`04` §3.5's argument, as a test); a position marked on a session with no bar
  falls back to the last close and says so; the counter decrements on a fill and **not** on a
  proposed line, not on a `DRY_RUN` plan being built, and not twice for the same fill.

---

### TW6 — The desk plan, `/twt/execute`, and the ratchet

**Goal:** one click confirms; the GTT is armed; the stop ratchets; nothing fires without the click.

- `baskfy.twt.evening(trade_date)`: `exit_lines` → `ARM_GTT` and `RAISE_GTT_STOP`; `build_entries`
  → `BUY_AT_OPEN`; write `tw_plan` (source `EVENING`) with its lines and skips;
  `AlertName.TWT_EVENING` email per `05`; close the `tw_session` row.
- `baskfy.twt.morning`: rebuild the same plan as `MORNING` before the open, re-sized, **not
  re-detected** (`04` §11.3).
- Desk route `GET /twt` per `05` §2 and `POST /twt/execute {plan_id, line_id, confirm}`: validate
  `confirm=true` and the 30-minute expiry; re-derive and re-size under a row lock on the day's
  `tw_session`; then `BUY_AT_OPEN` → `OrderGateway.place(BUY, CNC, MARKET)`; `ARM_GTT` →
  `place_gtt_stop` with `TWT_STOP_BAND` and `limit_fraction=TWT_GTT_LIMIT_FRACTION` [0.97];
  **`RAISE_GTT_STOP` → the swing book's delete-and-replace path**: cancel the resting trigger, arm
  the new one, and when the cancel succeeds but the arm does not, record the intent, null the
  `gtt_id` and return `BLOCKED` naming the position as **NAKED**.
- The **15:15 sweep** re-arms anything naked and raises `TWT_GTT_MISSING_AT_1515` for what is still
  naked afterwards.
- Fills come back through the desk's existing `on_order_update` handler into `tw_fill`,
  `tw_position` and the GTT.

- **AC:** the desk suite's non-negotiable tests still green; new tests — an expired plan → **410**;
  a missing `confirm` → **400**; a `RAISE_GTT_STOP` at or below the resting trigger → `BLOCKED` ("a
  stop never falls"); a `RAISE_GTT_STOP` at or above the last price → `BLOCKED`; a cancel that
  fails leaves the **old** stop resting and places nothing; a cancel that succeeds and an arm that
  fails leaves `gtt_id` null, the intent recorded and the position reported NAKED; a fourth
  `BUY_AT_OPEN` confirm in one session → `BLOCKED (SESSION_CAP)`; a second confirm of the same line
  is refused by the idempotency key; **every fill arms a GTT in the same request**; and with the
  flag false `place` is called only on the dry-run adapter (asserted by a spy) — **0 orders reach a
  broker** in the whole suite.

---

### TW6a — The six routes the runbook needs, and one of them is the stop switch

**Added 11 Sep 2026, by the module that wrote `FIRST-LIVE-MORNING.md` before the code.** Writing
the first live morning down turned up six commands a person needs and no module was planned to
build. That is the whole reason the runbook was written first; it is cheaper to find a missing
route in a document than at 09:05 with money on the line.

**They belong to TW6** (the desk) and **TW8** (the web read), and are listed here so neither can
finish without them.

| Route | Why the morning needs it | Mirrors |
|---|---|---|
| **`POST $DESK/twt/halt`** | **The stop-the-sleeve command.** Nothing in this plan provided one: the desk's kill switch is per-process and manual, and the execution flag is read once at startup so no route can flip it. | new |
| `POST $DESK/twt/reconcile` | Attaches a hand-armed GTT to a position. **Without it, a line Maulik protects by hand reads naked forever** — and the 15:15 sweep would keep shouting about a line that is in fact covered. | `POST /swing/reconcile` |
| `POST $DESK/twt/rearm` | `05` §2 specifies a per-line Re-arm button and names no route for it. | `POST /swing/rearm` |
| `POST $DESK/twt/sweep` | The 15:15 chore as a route, so it is checkable from a phone. | `POST /swing/cutoff` |
| `make twt-plan DATE=… [SOURCE=EVENING\|MORNING]` | The plan from a terminal. | `make vbt-plan` |
| `GET`/`PATCH $WEB/api/v1/twt/config`, `$WEB/me/twt` | Reading and setting the sleeve's capital. `sleeve_capital_inr` travels as a **string**. | `/api/v1/vbt/config`, `/me/swing` |

**`/twt/halt` has four required behaviours, and the third is the one a test must pin:**

1. It zeroes the sleeve's capital, audited — a sleeve with no money cannot size a line, which is
   the same mechanism that keeps it safe before the first morning.
2. It expires every live plan, so no outstanding `plan_id` can still be confirmed.
3. **It never touches protection.** Resting GTTs stay where they are, and `ARM_GTT` and
   `RAISE_GTT_STOP` keep working. A halted sleeve cannot BUY; everything it already holds keeps
   its stop and keeps ratcheting. A halt that removed stops would be the most dangerous button in
   the product.
4. It is one line, runnable from a phone, and says what it did.

**Every POST to the desk must carry `-H "Origin: $DESK"`.** `app/core/websec.py::DeskSecurity`
refuses a state-changing request that cannot name its origin. A browser sends it; `curl` does not,
and the failure reads exactly like a permissions problem. Found while writing the runbook.

- **AC:** each route exists and is exercised by a test; `/twt/halt` has a test for each of its four
  behaviours, and the protection one asserts that a resting GTT is still resting and a
  `RAISE_GTT_STOP` still plans after a halt; a POST without an `Origin` header is refused; the
  config route round-trips `sleeve_capital_inr` as a string without going through a float.

---

### TW7 — The fill-day rule and the naked-line assertion

**Goal:** a line is never unprotected for a session, and the backtest's `stop_day0` is the live
book's same-session GTT.

- `04` §7.4 in the backtest: a fill whose session low breaches the initial stop is closed that
  session, at the stop or at the open when the open was already below it, reason `STOP_DAY0`.
- In the live book: the GTT armed in the same request as the fill (TW6) **is** the rule, and the
  sweep is the proof. `tools/twt/sweep.py` and the desk's 15:15 chore assert that **every** `OPEN`
  position with `quantity_open > 0` has a non-null `gtt_id` before 15:30.
- The alert `TWT_POSITION_NAKED` on any naked line at any time, and
  `TWT_GTT_MISSING_AT_1515` for one the sweep could not fix.

- **AC:** `test_twt_fill_day.py` — a synthetic bar whose low is 21 % under the open closes the
  position the same session with reason `STOP_DAY0` and the right price; a bar whose **open** is
  already below the stop fills at the open; a bar that touches exactly the stop fills at the stop;
  `test_twt_safety_property.py` — over a generated book of fills and sessions, **no session ends
  with an `OPEN` position and a null `gtt_id`** once the sweep has run; the sweep is idempotent and
  keyed on the day.

---

### TW8 — The pages

**Goal:** Maulik opens the web app and sees what the sleeve is doing; he opens the desk and can act
on it.

- Web `/twt` per `05` §1: the gate, today's tight names with the point-in-time sentence, the open
  book with `high_since`, the trigger and **the distance to it**, and the half-size counter.
  Read-only; 405 on every mutation but a note and a dismissal.
- Web `/twt/backtest` per `05` §3, with `01` §8's caveats as a component.
- Desk `/twt` per `05` §2: exits first, then entries, then the book, then Confirm; the 15:15 strip.
- `apps/web` e2e: the page renders with an empty sleeve, with a `SHUT` gate, and with a naked line.

- **AC:** the read-only test extended to `/twt` — every non-GET route under it is a 405 except the
  two that change no money; `test_twt_page_copy.py` asserts the point-in-time sentence and the
  caveats are present verbatim; **no bare dash** in any unavailable metric (the rule the portfolio
  work established); the desk page shows `DRY_RUN` as a badge and an expired plan's buttons are
  **absent**, not disabled; light and dark both pass the existing contrast check.

---

### TW9 — The backtest on the page

**Goal:** a number, from the plant's own bars, beside the study's, with its drift named.

- `tools/twt/backtest.py` and `make twt-backtest`: run `baskfy_core.twt.backtest` over
  `ohlcv_daily` from `BacktestConfig.start` to the last published session, at the **shipped**
  `min_turnover_inr` [₹5 crore], sized against `params.sleeve_inr`, **never** against
  `tw_config.sleeve_capital_inr`.
- Append one `tw_backtest_run` row with `source = PLANT`; TW2's reproduction appends one with
  `source = RESEARCH_EXPORT`. Append-only, both.
- `drift`: `cagr_pct_delta`, `max_dd_pct_delta`, `trades_delta` against `01` §6, `flagged` when
  `abs(cagr_pct_delta) > 1.0`.
- The page reads the latest **finished** run per source.

- **AC:** two runs of the same date produce two rows and edit nothing; a run that raises writes
  `finished_at` and `error` and re-raises; the page shows the latest finished run and **not** a
  later failed one; a synthetic result 1.5 CAGR points from `01` §6 renders the drift warning
  naming both numbers; the run never reads `tw_config.sleeve_capital_inr` (asserted by a spy).

---

### TW10 — Safety, the claims become theorems, and the runbook

**Goal:** every Track-B and Track-C claim of `02` is a test, and the first live morning is written
down before it happens.

- `test_twt_safety_properties.py`: with `BASKFY_TWT_EXECUTION_ENABLED=false`, **no TWT code path
  reaches `OrderGateway.place` or `place_gtt_stop` with `DRY_RUN=false`** — a property test over
  every route and every task, not a spot check.
- **No auto-execute flag exists for this sleeve**: a test greps the tree for
  `BASKFY_TWT_AUTO`-anything and asserts nothing is found, and that non-negotiable 1's named
  exception is still the swing sleeve's alone.
- **`exit_lines` never emits `SELL_AT_OPEN`** (`04` §10.2).
- The neighbours are untouched: the weekly book, R1–R4, the swing book and VBT-1 — their suites run
  and `git diff` touches none of their rules.
- `tools/twt/drill.py`: the full `DRY_RUN` drill — evening plan, morning plan, confirm every line,
  a fill, a GTT, a ratchet, the sweep — with **0 orders reaching a broker**.
- **`docs/twt/FIRST-LIVE-MORNING.md`**: the exact sequence for the first real session (Kite login
  before 09:00, the token sync, the flag, the capital setting, `/analyze`, what the plan must show,
  `/execute`, the GTT check at 09:20 and at 15:15, what to do when a GTT is missing, and **how to
  stop the sleeve in one command**), and the daily routine after it (login, ratchet plan, confirm,
  sweep).

- **AC:** the property test is green and the drill reports 0 broker orders; the auto-execute grep
  finds nothing; `exit_lines` emits no `SELL_AT_OPEN` over a generated book; both trees' suites
  pass and `make lint` is clean in both; `FIRST-LIVE-MORNING.md` exists, names every step with the
  command that runs it, and contains the stop-the-sleeve command; `gates/twt-10.md` fully checked.

---

## After TW10

`TW-FINAL-REPORT.md` at the repo root, in the style of `SW-FINAL-REPORT.md` and
`VB-FINAL-REPORT.md`: what was built, what was decided, **what is NOT done**, what needs Maulik,
and the first-live-morning runbook verbatim. Then ask Maulik to review.

This run deploys nothing.
