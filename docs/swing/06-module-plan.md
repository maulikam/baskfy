# 06 — Module plan: SW0–SW12

One commit per module, `SW<N>: green — <one line>`. A module is green when its acceptance
criteria pass as tests (or, for a human-visible surface, when the page renders in the dev stack
and a browser/E2E check covers it), `make lint` is clean in the touched trees, both existing
suites still pass, and `STATUS.md` + (if judgement was exercised) `DECISIONS-SW.md` are updated.
Criteria are proxies for Goals — the charter's precedence order applies.

Dependencies: SW1 → SW2 → SW3 → SW4 → SW5 → {SW6, SW7} → SW8 → SW9 → SW9.5 → SW10 → SW11 → SW12.
SW6 and SW7 are independent of each other. SW9 (backtest) may run in parallel with SW6–SW8 if
a second terminal is available; it needs only SW1–SW3.

---

### SW0 — Baseline and read-in

**Goal:** a fresh session knows exactly where it stands and cannot damage what exists.

- Read the read-order docs; read `docs/swing/STATUS.md` and resume from the first non-green
  module if this is a resumed run.
- Record in STATUS: repo/branch state (this pack was written on `developer` while another
  session had unrelated dirty files in the tree — do not commit them under a swing module), both suites' pass counts,
  the latest `ohlcv_daily` date and `data_version`, the Alembic head (`0027` at write time), the
  Beat schedule inventory, whether a Kite token is present (`kite-token-expiry` task's view),
  `DRY_RUN` in every env file.
- Verify `DRY_RUN=true`; verify the desk suite is green before writing anything.
- **AC:** STATUS.md's SW0 section lets a reader with no other context name the tables, suites,
  data date and flags this run builds on; both suites green at baseline.

### SW1 — The pure core, re-verified and adopted

**Goal:** the pack's pre-built `baskfy_core.swing` is the run's foundation, proven in the
repo's own harness, not the author's.

- `make lint` and the core suite over `packages/core` with the swing tests collected (the pack
  ran them in a minimal venv on Linux; the repo's `uv` environment on macOS is the one that
  counts). Fix nothing in the module unless a test fails here.
- Add `baskfy_core.swing` to the mutation harness targets (`make mutants`) at the same threshold
  as `factors` — record the score.
- `packages/core/tests/test_swing_docs_parity.py`: every field name in `baskfy_core.swing.config`
  appears in `docs/swing/04-business-rules.md` (the document is the contract; a renamed field
  without a doc edit fails).
- **AC:** 103 swing tests + escape-hatch scan (8) green under `make test`; mutation score
  recorded; docs-parity test green.

### SW2 — Schema and settings

**Goal:** the `sw_` schema of `03`, migrated, seeded, idempotent, with the M4.1 boundary intact.

- Migration `0028_swing.py`: every table in `03` §1–§8, indexes, FKs, `user_id` everywhere.
- Models in `models/swing.py`; `sw_config` seeded for `BASKFY_SOLE_USER_ID` with the pack
  defaults and `sleeve_capital_inr = 0`.
- `baskfy_worker.settings` / `baskfy_api.settings`: `BASKFY_SWING_RISK_PER_TRADE_PCT_MAX`,
  `BASKFY_SWING_MAX_POSITION_PCT_MAX`, `BASKFY_SWING_MAX_OPEN_POSITIONS_MAX`,
  `BASKFY_SWING_EXECUTION_ENABLED`, `BASKFY_SWING_MONITOR_ENABLED`,
  `BASKFY_SWING_EP_PREMARKET_ENABLED` (root `.env.example` already documents them — mirror, do
  not re-invent). The desk's `config.py` reads the three flags too.
- A `SwingSettings` Pydantic spec (the desk's `analytics/settings.py` pattern): validation
  against the ceilings, `settings_audit` on write; `tests/test_settings_boundary.py` extended so
  a ceiling can never become a form field.
- **AC:** migrate → seed → migrate again is a no-op; a `risk_per_trade_pct` above the ceiling is
  a 422 with the ceiling named; every `sw_` table has `user_id`; `test_settings_boundary` green.

### SW3 — The daily detection job

**Goal:** every trading day, `sw_setup_daily` and `sw_market_daily` are written from the
published bars, and the numbers on them are exchange prices.

- Worker task `baskfy.swing.detect(trade_date)`: load the last 200 sessions of `ohlcv_daily` ⋈
  active `instrument` (EQ/BE), `with_swing_indicators`, `detect_setups`, convert levels by
  `adj_factor`, add `sector_slug` (from `index_member_daily`) and `listed_within_2y`, apply the
  `+5/+5` score adjustments of `04` §2.6, round with `apply_storage_precision` (extend
  `COLUMN_PRECISION` for the new columns), upsert idempotently.
- Breadth + index reading + ladder → `sw_market_daily` (the ladder reads closed `sw_position`
  rows; none yet → rung stays 0).
- A new `PipelineStep.COMPUTE_SWING` after `REFRESH_BASKET`, **unable to fail the run** (M30's
  rule for post-publish steps); also runnable by CLI `make swing DATE=…`.
- Funnel counts in the step's `detail`: universe → liquid → per-setup candidates.
- **AC:** running the task twice for a date changes no rows; a date with no published bars
  writes nothing and says so; a synthetic split inside a base (fixture with `adj_factor ≠ 1`)
  yields a stored `trigger` equal to the raw price; the step's failure leaves the run
  `SUCCEEDED`; a `sw_setup_daily` row for a real date exists on the dev stack.

### SW4 — API and the Setups / Market pages

**Goal:** Maulik opens the web app on a weekend and sees the flags forming with their pivots.

- Router `services/api/.../routers/swing.py`: `GET /swing/setups?date&setup&status`,
  `GET /swing/market?from&to`, `GET /swing/setups/{instrument_id}/bars` (130 bars for the
  mini chart), `GET /swing/sectors`, `GET /swing/config`, `PATCH /swing/config` (validated as
  SW2). OpenAPI regenerated; `packages/api-client` regenerated.
- Pages `/swing` and `/swing/market` per `05` §2, with the sector strip and the empty-state
  funnel line; nav entries flip to `ready`.
- Beat: `swing-weekend` (Sat 07:00 IST) runs detect over the last 5 sessions and writes the
  weekly breadth note into `sw_market_daily.detail`.
- **AC:** the read-only test of `05` §2 exists and is green; `GET /swing/setups` p95 < 300 ms on
  the dev stack with 2,500 instruments (the `market/mood` budget); a Playwright check renders
  the page with one flag and one locked EP and shows the lock icon.

### SW5 — Watchlist, plan preview, EOD job, alert

**Goal:** every evening the book is managed by the rules and tomorrow is already planned.

- `sw_watch` service + API: `POST /swing/watch`, `DELETE`, `PATCH` (note, catalyst); detector
  rows with `SETTING_UP` and score ≥ 60, and every `GAP_DAY` EP, are auto-watched (source
  `DETECTOR`); expiry per `03` §4.
- Task `baskfy.swing.eod(trade_date)`: `stops.manage` over open positions with today's bar and
  MAs → exit lines; `build_entries` over the watchlist with the tier → `sw_plan(source=EOD_PREVIEW)`
  + lines + skips; `AlertName.SWING_EOD` email per `05` §4.
- Pages `/swing/watchlist`, `/swing/positions`; the plan preview shown on positions.
- **AC:** a fixture book with one position on day 3 and green produces exactly a `SELL_AT_OPEN`
  of ⅓ and a `RAISE_GTT_STOP` to entry; the preview lists every skip with its reason; the alert
  renders in Mailpit with the naked-GTT section present (empty) and the session counter.

### SW6 — Premarket EP scan and the opening-range monitor

**Goal:** on a weekday morning the desk shows which pivots broke their 5-minute opening range.

- Task `baskfy.swing.premarket` (08:50 IST, flag `BASKFY_SWING_EP_PREMARKET_ENABLED`): refresh
  watch levels from the latest bar; at 09:09 (a second Beat entry) pull quotes for the liquid
  universe in ≤ 500-symbol batches, `live_gap` → new `sw_watch` rows (setup EP, source
  `DETECTOR`, catalyst empty), then rebuild the morning plan (`source=MORNING`).
- Desk process `app/strategies/swing_breakout.py` implementing `BaseStrategy` (it exists for
  exactly this — `intraday_skeleton.py` is the template), flag `BASKFY_SWING_MONITOR_ENABLED`:
  subscribe the watchlist's tokens on the `TickBus`; at window close build the range from
  `historical_data(interval="minute")` (fallback: the ticks' own high/low), then `evaluate_trigger`
  on every tick; `TRIGGERED` → `sw_signal` row + a one-line `sw_plan(source=SIGNAL)` + desk
  notification. `generate_targets` returns `[]` — this strategy **never** places. Stops at
  10:45.
- Replay harness: `tools/swing/replay.py` feeds a recorded morning (`bus.last_tick` journal, or a
  minute-candle CSV) through the strategy and asserts the signals; the pack ships one synthetic
  morning fixture.
- **AC:** with the flag off the strategy is not instantiated (test); the replay of the fixture
  morning raises exactly the expected signals with the expected entry/stop; a quote batch never
  exceeds 500 symbols and never exceeds the limiter; a locked name yields a
  `LOCKED_UPPER_CIRCUIT` signal and no plan line.

### SW7 — The desk page and `/swing/execute` (DRY_RUN)

**Goal:** one click confirms; the GTT is armed; nothing fires without the click.

- Desk route `GET /swing` per `05` §3; `POST /swing/execute {plan_id, line_id, confirm}`:
  validate expiry and `confirm=true`; `BUY_ON_TRIGGER` → `OrderGateway.place(side=BUY,
  product=CNC, order_type=LIMIT, price=trigger, client_id=plan_id:symbol:BUY)` then
  `place_gtt_stop(trigger=stop, band=StopBand(0.005, 0.10))` in the same request;
  `SELL_AT_OPEN` → `place(side=SELL, order_type=MARKET)` for **at most `quantity_open`** of an
  `sw_position` the sleeve owns; `RAISE_GTT_STOP` → `delete_gtt` + `place_gtt_stop` (refused if
  the new trigger is below the resting one). With `BASKFY_SWING_EXECUTION_ENABLED=false`, the
  same code path runs with the gateway's dry-run adapter and journals `simulated=true`.
- `sw_position` / `sw_fill` written from the gateway's journal records; `sw_session` counters.
- **Re-arm GTT** for a naked position.
- **AC:** the desk suite's 16 non-negotiable tests still green; new tests: expired plan → 410,
  missing confirm → 400, a SELL for more than `quantity_open` → `BLOCKED`, a SELL for a symbol
  not in `sw_position` → `BLOCKED`, a stop below the resting one → `BLOCKED`, every confirmed
  BUY has a GTT record in the same request, and with the flag false `place` is called only on
  the dry-run adapter (asserted by a spy) — **0 orders reach a broker** in the whole suite.

### SW8 — The journal page and the ladder closing the loop

**Goal:** the journal tells him, in R, whether he should be pressing or sitting.

- Close-out path: when `quantity_open` hits 0, write `r_multiple`, `pnl_inr`, `close_reason`;
  `swing-eod` feeds the last 5 closes into `exposure_tier`; the rung is written to `sw_config`
  and `sw_market_daily` and shown on every page.
- `/swing/journal` per `05` §2 with real and simulated cards, and `GET /swing/journal`.
- **AC:** five simulated closes with net positive R in a GREEN tape move the rung 0 → 1 on the
  next EOD run and the page says so with the five R values; three simulated losses move it back;
  RED puts it at 0 with entries disallowed and the next plan shows `GATE_RED` skips.

### SW9 — The EOD backtest

**Goal:** a number, with its caveats, before any real money.

- `tools/swing/backtest.py` + a `baskfy.swing.backtest` task (compute queue): `04` §11 over
  2017→, by setup and by year; the equity curve; the funnel; stored in `sw_backtest_run` (id,
  params, started/finished, stats JSONB) and served on `/swing/journal`'s backtest card.
- Reuse `baskfy_core.backtest`'s costs and calendar helpers where they fit; do not fork them.
- **AC:** the run over the full history completes on the dev box in < 30 minutes; a fixture year
  with a known planted flag reproduces the planted trade's R to 2 dp; the page shows the caveats
  verbatim from `04` §11; the run's parameters are on the card.

### SW9.5 — Reconcile with the primary sources

**Goal:** the rules are his, quoted, not a summary's — see `07-primary-source-corrections.md`.

- Apply `patches/2026-09-02-primary-source-corrections.patch` (it applies cleanly to SW8's
  `53f21c8`; if SW9 moved the files, re-apply by hand from the table in `07`), re-pin the 35
  contract/backtest tests it turns red, and make the doc edits `07` §"SW9.5" lists — **one
  commit**, `SW9.5: green — …`.
- The stop is one ADR or tighter (skip, never size down); the gate's index rule is the 10-day
  above the 20-day; at most three new entries a session; the plan takes the smaller of the
  rung and the trader's cap; the sleeve locks out new entries 15% below its peak until it is
  back within 10%; the swing GTT rests 3% under its trigger; ceilings 30% / 20 positions.
- **AC:** as `07` §"SW9.5" step 5.

### SW10 — Gating and safety proof

**Goal:** the Track-B and Track-C claims are theorems, not intentions.

- Tests: no `PARABOLIC_SHORT` can become a plan line (property test over random watchlists); no
  route in `apps/web` under `/swing` imports execution or calls `/swing/execute`; with
  `BASKFY_SWING_EXECUTION_ENABLED=false` no code path from `/swing/execute` reaches a non-dry-run
  adapter (spy over the gateway); every `sw_` write carries `BASKFY_SOLE_USER_ID`; a SELL never
  exceeds what the sleeve owns; a stop never falls (property test over `apply`); the monitor's
  strategy has no `place` call (source scan, `services/api/tests/test_sleeves_are_not_orders.py` style).
- The **DRY_RUN morning drill** in `RUN-AND-TEST.md` §Swing: premarket → replayed morning →
  confirm two lines → EOD → next morning's plan, end to end, **0 orders reaching a broker**.
- **AC:** every test above green; the drill script exits 0 and prints the counters that
  `sw_session` recorded.

### SW11 — Hardening and observability

**Goal:** the morning cannot fail quietly.

- Spans/metrics over detect, premarket, monitor, execute (M20 pattern, optional, unable to
  raise into the order path); alert rules `SWING_POSITION_NAKED`, `SWING_MONITOR_DID_NOT_START`
  (09:20 with the flag on and no `sw_session.monitor_ran`), `SWING_DETECT_STALE` (no
  `sw_setup_daily` for the published date by 21:30); runbook 6 entries.
- p95 budgets: `/swing/setups` < 300 ms, the monitor's tick→verdict < 5 ms in-process, the
  detect step < 3 minutes for 2,500 instruments.
- **AC:** the three alerts fire in a test harness; the budgets are measured and recorded in
  `benchmarks/AS-MEASURED.md`.

### SW12 — Verification, goldens, final report

**Goal:** a fresh session, or the Go lane, can pick this up.

- `tools/parity/golden.py` learns `swing`: dump `detect_setups`, `size_position`, `manage`,
  `exposure_tier` outputs for the fixture set into `go/testdata/golden/L1/swing/`; a line in
  `docs/go-rewrite/REQUESTS.md`.
- `SW-FINAL-REPORT.md` at the root: what was built, what was decided, what is NOT done, what
  needs Maulik (the `02` §3 checklist, verbatim, with the current evidence per item), and the
  exact steps for the first DRY_RUN morning.
- `NEEDS-MAULIK.md` gains a **Swing** heading with: sleeve capital, the risk he starts with, the
  execution-flag decision, and the catalyst/news question (D10).
- **AC:** goldens committed and byte-stable across two runs; the report exists; STATUS.md is
  all-green or says exactly what is not.
