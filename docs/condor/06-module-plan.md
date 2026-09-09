# 06 — Module plan: OC0–OC12

One commit per module, `OC<N>: green — <one line>`. A module is green when its acceptance
criteria pass as tests (or, for a human-visible surface, when the page renders in the dev stack
and a browser/E2E check covers it), `make lint` is clean in the touched trees, both existing
suites still pass, and `STATUS.md` + (if judgement was exercised) `DECISIONS-OC.md` are updated.
Criteria are proxies for Goals — the charter's precedence order applies.

Dependencies: OC1 → OC2 → OC3 → {OC4, OC5} → OC6 → OC7 → OC8 → OC9 → OC10 → OC11 → OC12.
OC4 and OC5 are independent of each other. OC9's Tier 1 and Tier 2 need only OC1–OC3 and may run
in a second terminal from then on; Tier 3 needs OC7's fill simulator.

Nothing in this plan edits `frozen/`, the gateway's product gates, or the overnight-option
guard. Nothing in this plan flips a flag.

---

### OC0 — Baseline, read-in, and the facts this run stands on

**Goal:** a fresh session knows exactly where it stands, cannot damage what exists, and has
verified — not assumed — the three data facts `07` depends on.

- Read the read-order docs; read `STATUS.md` and resume from the first non-green module if
  this is a resumed run.
- Record in STATUS: branch and HEAD; another session's dirty files (do not commit them under
  an OC module); both suites' pass counts; the Alembic head; the Beat inventory; the Kite token
  state; `DRY_RUN`, `OPTIONS_ENABLED`, `INTRADAY_ENABLED` in every env file (all must read
  `true`/`false`/`false`); the desk's `LOCKED_KEYS`.
- **Verify against the live API and record the answers:** (a) the earliest date
  `historical_data(interval="minute")` returns for the NIFTY 50 index token; (b) that a NIFTY
  option that expired last month is absent from `instruments("NFO")` and its history cannot be
  queried (the error text); (c) the NFO master's `expiry`, `strike`, `lot_size` columns for the
  current NIFTY and BANKNIFTY monthly contracts, and the lot sizes they state; (d) whether
  `margins()` shows the F&O segment enabled on the account; (e) whether the India VIX index
  token serves daily history. Each is one or two rate-limited calls; none places anything.
- Inventory `kite-momentum-rebalancer/data/outputs/strangle_*` **read-only** (`ls`, row counts,
  date ranges, columns) and record whether it holds anything Tier 2 can be checked against.
- Open the **Condor** heading in `NEEDS-MAULIK.md` with the items `02` §3.5 and `07` §2 name:
  F&O segment and margin pool, the historical option-data purchase decision (with the loader's
  field list), the event-day list for the financial year, and the written risk decision.
- **AC:** STATUS's OC0 section lets a reader with no other context name the tables, suites,
  data date, flags and the five verified facts this run builds on; both suites green at
  baseline; the NEEDS-MAULIK heading exists.

### OC1 — The pure core: `baskfy_core.condor`

**Goal:** the whole method as pure, typed arithmetic that a test can assert against `04`
without a database, a network, a disk or a clock.

- `packages/core/src/baskfy_core/condor/`: `config.py` (`CondorConfig` and its groups; every
  threshold of `04` is a field), `calendar.py`, `gate.py`, `chain.py` (`OptionQuote`, BS/IV/
  delta, `is_liquid`), `structure.py` (`build_condor`, `entry_sequence`, `exit_sequence`,
  `PlanRejected` with every `REJECTED_*` code), `costs.py`, `sizing.py`, `exits.py`, `risk.py`,
  `session.py` (the state machine), `journal.py`, `backtest.py` (the engine only; loaders are
  OC9's).
- Port the arithmetic from the frozen lab's `options.py`, `options_costs.py` and
  `strangle/fills_paper.py` **by re-implementation** with the source file and function named in
  each docstring (PACK.1); `packages/core/tests/condor_fixtures.py` carries twenty recorded
  quotes and a fixture expiry morning (45 index bars, a chain at 09:59, a minute path to 14:30).
- Tests `test_condor_calendar.py`, `test_condor_gate.py` (incl. the ER identities of `04` §2.8),
  `test_condor_chain.py` (BS/IV agreement to 1e-6 with values computed by the frozen functions
  *at fixture-authoring time*, stored as numbers — the test imports nothing from `frozen/`),
  `test_condor_structure.py` (the never-naked property under hypothesis: over random chains and
  partial-fill outcomes, at no step is short quantity > long quantity for either side),
  `test_condor_costs.py`, `test_condor_sizing.py` (the worked examples of `01` §6 to the rupee),
  `test_condor_exits.py` (precedence, staleness, the single-decision rule), `test_condor_risk.py`,
  `test_condor_session.py` (the state machine under random edge sequences),
  `test_condor_journal.py`, `test_condor_purity.py` (no db/network/disk/clock imports — the
  swing test's pattern), `test_condor_docs_parity.py` (every config field name appears in `04`).
- Add `baskfy_core.condor` to the mutation harness at the `factors` threshold; record the score.
- **AC:** the suite green under `make test`; `mypy --strict` and `ruff` clean;
  `test_no_escape_hatches.py` still green; the purity test green; mutation score recorded; the
  three worked examples of `01` §6 reproduce to the rupee.

### OC2 — Schema, the NFO master, settings and `condor_gates()`

**Goal:** the `oc_` schema of `03`, migrated, seeded, idempotent, with the M4.1 boundary intact
and the calendar reading only the instrument master.

- Migration `<next>_condor.py`: every table in `03`; `oc_chain_snapshot` partitioned by month;
  `user_id` everywhere. Models in `models/condor.py`.
- `baskfy_worker.tasks.instruments` extended: persist NFO index-option rows for the configured
  underlyings (current + next two expiries), prune expired rows into `oc_contract_history`;
  rebuild `oc_expiry` nightly; alert on a `lot_size` change or a moved monthly expiry.
- `oc_event_day` seeded from `04` §1.3 for the current financial year (the list lives in
  `baskfy_worker/seeds/condor_event_days.py` with each date's source in a comment).
- Settings: the six ceilings of `02` in `baskfy_worker.settings` / `baskfy_api.settings` and the
  desk's `config.py`; the four flags; `condor_gates()` in the desk (and its twin in the worker)
  returning `(mode, dict_of_flags)`; a `CondorSettings` Pydantic spec validating `oc_config`
  against the ceilings with `settings_audit` on write; `tests/test_settings_boundary.py`
  extended so a ceiling can never become a form field and `OPTIONS_ENABLED` /
  `INTRADAY_ENABLED` stay `LOCKED_KEYS`.
- Root `.env.example` gains the condor block (mirror the swing block's shape and comments).
- **AC:** migrate → seed → migrate again is a no-op; the calendar's monthly expiry for the
  current month equals what the master says (a fixture master with a shifted expiry moves the
  date and a weekday-rule implementation would fail the test); `risk_budget_inr` above either
  ceiling is a 422 naming both; adding `BANKNIFTY` to `underlyings` with its flag off is a 422;
  every `oc_` table has `user_id`; `condor_gates()` returns `DRY_RUN` under every combination
  but all-four-true (a 16-row table test).

### OC3 — Index minute bars, option quotes with depth, margins, and the collector

**Goal:** every input the gate, the chain and the sizing need is a rate-limited provider read,
and the forward dataset starts accumulating.

- `baskfy_providers.kite`: `minute_bars(token, date, *, interval="minute")` (chunked as
  `daily_bars` is), `option_quotes(symbols)` returning `OptionQuote`-shaped records with depth
  (≤ 25 symbols a call, through the shared limiter), `basket_order_margins(legs)` (one call,
  read-only, `consider_positions=true`), `index_history(token, start, end)` for the VIX. Ports
  and fixtures in `records.py` / `fixtures.py`; the M4 fake client extended.
- Worker task `baskfy.condor.index_bars(date)`: the day's minute bars for the configured
  indices into `oc_index_minute` (add to `03` if the run prefers a table to `detail` JSON —
  record the choice); `baskfy.condor.backfill_index_bars(from, to)` resumable, for Tier 1.
- The collector `baskfy.condor.collect_chain` (Beat: every minute 09:15–15:30 on
  `is_trading_day`, behind `BASKFY_CONDOR_CHAIN_COLLECT_ENABLED`): two quote calls a minute,
  rows into `oc_chain_snapshot`, gap counter in `detail`.
- **Measure the limiter share**: with the swing book's morning reads replayed from its timing
  probe (`docs/swing/status/`), the collector's two calls a minute fit inside the 3 req/s budget
  with headroom; record the numbers in STATUS.
- **AC:** the fake client's minute bars round-trip to `oc_index_minute` idempotently; a
  `quote()` batch never exceeds 25 symbols and never exceeds the limiter (test); the collector
  skips non-trading days without a call; a `basket_order_margins` fixture returns the hedged
  and the transient figure; the Tier-1 backfill from the verified earliest date runs on the dev
  stack for at least one full year and STATUS says how long a full backfill will take.

### OC4 — API and the web hub

**Goal:** Maulik opens the web app on a phone and sees the next expiry, the last verdict with its
reasons, the position if any, and the journal — and can change nothing that moves money.

- Router `services/api/.../routers/condor.py` with the routes of `05` §3; OpenAPI regenerated;
  `packages/api-client` regenerated.
- Pages `/condor`, `/condor/journal`, `/condor/calendar`, `/me/condor` per `05` §3; nav flips to
  `ready`.
- `test_condor_readonly.py` on both sides of the wire: exactly two mutations allowed (event-day
  add/remove, the settings save), everything else a 405.
- **AC:** the read-only test green; `GET /condor/session/today` p95 < 200 ms on the dev stack;
  a Playwright check renders `/condor` with a fixture `SKIPPED` session showing all three of its
  reasons and a fixture `OPEN` session showing the D bar.

### OC5 — The plan builder, costs verified, the verdict alert

**Goal:** at 09:59 on an expiry day the system produces either a reasoned no-trade or one
four-leg plan with its credit, lots, cost test and margin — from live reads, into `oc_plan`.

- Service `baskfy_worker/condor/plan.py` (or the desk's `app/condor_plan.py` — the desk is the
  clock, so the desk; record the choice): `build_session_plan(date, underlying)`: the gate over
  the day's bars → chain read → `build_condor` → `expected_round_trip` → `lots_for_budget` →
  `basket_order_margins` → `oc_plan` + `oc_leg` rows with `expires_at = min(now + 30 min,
  10:15)`, or `oc_session.state = SKIPPED` with the code.
- **Cost rates verified**: each rate in `CostRates` checked against the broker's published
  schedule on the day, the URL and date in the docstring, and a `CONDOR_COST_RATES_REVIEWED_ON`
  constant the OC11 check warns on after 90 days.
- `AlertName.CONDOR_VERDICT` — email + the dark Telegram notifier — with the verdict or the plan
  summary, rendered in Mailpit.
- **AC:** the fixture morning yields exactly the pack's expected plan (strikes, credit, three
  lots, cost share, margin) to the rupee; a fixture with a wide spread on the short put is
  `REJECTED_COST` and the alert says so; a fixture where the only in-band call is inside the
  opening range is `REJECTED_NO_SHORT_CALL`; the alert renders; the builder is idempotent for a
  date (a second call returns the same `plan_id`).

### OC6 — The desk process: observation, verdict and the exit engine

**Goal:** on an expiry morning the desk watches the index from 09:15, raises the verdict at
09:59 and the plan at 10:00, and after a fill runs the exit engine every tick to 14:30.

- `app/strategies/condor_expiry.py` implementing `BaseStrategy` (the swing monitor is the
  template), flag `BASKFY_CONDOR_MONITOR_ENABLED`: subscribe the index token on the `TickBus`
  from 09:15; build the minute bars from ticks and reconcile once at 09:59 against
  `minute_bars` (A4 pattern); call the plan builder; after `OPEN`, subscribe the four legs and
  the index, mark D from the legs' quotes (the `quote()` fallback capped at one call per 5 s,
  B10 pattern), `exits.evaluate` per tick → an `EXIT` plan in sequence handed to the executor
  (OC7). `generate_targets` returns `[]` — the strategy never places.
- `app/condor_clock.py`: 09:15 start, 09:59 verdict, 10:15 lapse sweep, `hard_exit_time`
  flat, 15:35 stop; the desk is the clock.
- Replay harness `tools/condor/replay.py`: a recorded expiry morning (ticks or minute CSV plus
  a chain snapshot series) through the strategy, asserting the verdict, the plan and every
  `ExitDecision`; the pack ships one synthetic morning; OC3's collector output becomes further
  fixtures as real days accumulate.
- **AC:** with the flag off the strategy is not instantiated (test); the fixture morning replays
  to the expected verdict, plan and a `PROFIT` exit at the expected minute; a morning whose
  index feed dies at 14:05 raises `HARD_EXIT / FEED_LOST`; a restart at 11:30 resumes from
  `oc_position.last_d_points` and continues; on a non-expiry day the process exits at 09:15
  having made no Kite call.

### OC7 — The desk page and `/condor/execute` (DRY_RUN)

**Goal:** one click confirms; wings fill before shorts; the exits run under the same confirm;
the position is flat by 14:30; nothing fires without the click and nothing reaches a broker.

- `app/condor_execute.py`: `execute_entry(plan)` sends the four legs in `entry_sequence`
  through the real gateway (`product="MIS"`, `exchange="NFO"`, `client_id = plan_id:symbol`)
  in its dry-run branch, waiting on fills per `04` §4.5–4.6 and abandoning correctly;
  `execute_exit(position, decision)` in `exit_sequence` per §4.7 with `client_id =
  plan_id:symbol:CLOSE`; fills simulated against the depth ladder (`sim_method=DEPTH_LADDER`,
  ported from the frozen `fills_paper.simulate_fill` — PACK.1) when `DRY_RUN`.
- The gateway's `StopBand` is irrelevant here (no GTT) — assert that no condor path calls
  `place_gtt_stop`.
- `PgCondorStore` over the desk's Postgres adapter (sqlite twin in tests), the swing store's
  pattern.
- Page `GET /condor` with panels A–D of `05` §2; `POST /condor/execute`, `POST /condor/close`,
  `POST /condor/event-day`; the confirm label and PACK.5 sentence verbatim.
- **AC:** through the real gateway with `DRY_RUN=true`: a confirm produces four simulated fills
  in the right order and an `OPEN` position; a fixture where the long call's depth is thin
  yields `ABANDONED_ENTRY`, the filled put wing closed, no short ever sent, session
  `NEVER_OPENED`; an `ExitDecision` produces four closes in the right order with the third
  attempt on a short at market; `GET /condor` renders each panel from fixtures; a spy on the
  gateway records **0 orders reaching a broker** under both `DRY_RUN` values with the
  execution flag false; the never-naked property holds across 500 seeded fill sequences.

### OC8 — Journal, ledger, pauses, the first-live multiplier

**Goal:** every close writes the record, and the record governs tomorrow.

- Close → `oc_journal` row with the full cost breakdown; `risk.Ledger` evaluated at close and
  at 09:00 (`04` §8); `oc_config.paused_until` set and audited; `plan.build` refuses while
  paused; the first-live multiplier at plan time; `GET /condor/journal` per `05`;
  `AlertName.CONDOR_MONTHLY`.
- **AC:** a fixture month with three closes totalling −₹76,000 sets `MONTHLY_PAUSE` to
  month-end and the next expiry's session is `SKIPPED / REJECTED_PAUSED` with the verdict still
  journalled; a −₹31,000 marked loss intraday raises `DAILY_LIMIT` and the close; five real
  rows lift the half-size tag on the sixth plan; the summary never pools real and simulated.

### OC9 — The backtest, three tiers

**Goal:** the page shows what the data can honestly support, tier by tier, with the caveats
that make it honest.

- India VIX daily history backfilled into `index_snapshot_daily` (OC3's `index_history`).
- `tools/condor/backtest.py --tier {1,2,3} --underlying NIFTY --from --to` and the worker task
  `baskfy.condor.backtest` on the compute queue; `oc_backtest_run` rows; `GET
  /condor/journal.backtest`; the cards on `/condor/journal` with each tier's caveat from the
  row.
- Tier 1 with the ±25 % sensitivity table on each threshold; Tier 2 per `04` §11.2; Tier 3 over
  whatever `oc_chain_snapshot` holds (the run's own fixture days at minimum), with the sample
  banner.
- Both underlyings, always separately; BANKNIFTY's run exists whether or not its flag is on
  (a backtest moves no money).
- **AC:** Tier 1 over the full backfilled range runs end to end and its funnel is on the page;
  Tier 2's planted fixture day reproduces the pack's expected R to 0.01; Tier 3 over the fixture
  snapshots reproduces OC7's drill P&L to the rupee (the same engine, the same fills); the
  caveat text on the page equals `07` §4's verbatim; no card mixes tiers; the speed of a
  ten-year Tier 2 run is recorded.

### OC10 — Gating and safety proof

**Goal:** every Track-B/C claim in `02` is a test, and the drill proves the whole day with
**0 orders reaching a broker**.

- Hypothesis over random chains, fills and tick paths: never naked (both directions), never
  overnight (no `product` other than `MIS` is constructible from any condor path; the guard's
  refusal is exercised as an integration test with `OPTIONS_ENABLED=true` and `product="NRML"`
  forced), no second entry per session, no `ExitPlan` that is not the position's own four legs.
- The four-flag AND: a 16-row table over `condor_gates()` and a spy on the gateway — with any
  one flag false, `/condor/execute` journals `simulated=true` and the spy sees no live call;
  additionally, with the execution flag **true** but `OPTIONS_ENABLED` false, the **gateway
  itself** refuses every leg (`options disabled`) — belt and braces.
- Source scans with docstrings/comments stripped over the web hub, the API and the worker: no
  import of the gateway, no `place(`; every `oc_` write names its `user_id`.
- `tools/condor/drill.py`: the whole paper day against Postgres — 09:15 observation from a
  fixture feed → verdict → plan → confirm through `execute_entry` → the exit engine over the
  fixture path → close → journal → the ledger → the next expiry's 09:00 check — and the same
  drill with a `SKIP` morning.
- **AC:** every property test green; the drill prints `confirms=1 fills=8 orders_to_broker=0`
  and the journal row; the skip drill prints `confirms=0 orders_to_broker=0` and the verdict
  row; desk suite green.

### OC11 — Hardening and observability

**Goal:** the day it goes wrong, it goes wrong loudly and safely.

- Prometheus rules of `05` §4; in-process worker checks at 09:20 (a session exists on an expiry
  day), 10:20 (a plan was issued or the verdict was SKIP), 14:33 (nothing is `OPEN`), 15:35 (the
  collector wrote ≥ 300 minutes); runbook entry.
- Restart resume from `oc_position`; stale-quote and feed-loss behaviour of `04` §7.7 exercised;
  the `CONDOR_COST_RATES_REVIEWED_ON` warning; the token-expiry interplay (a token that dies at
  11:00 with a position open raises `HARD_EXIT / FEED_LOST` at once — and NEEDS-MAULIK says
  what the human does then).
- Budgets measured and recorded: verdict latency at 09:59, plan build time, tick → D → decision,
  the collector's share of the limiter with the swing morning running.
- **AC:** each rule evaluated from a synthetic series; the four checks fire on fixtures; the
  restart test green; budgets in STATUS.

### OC12 — Verification, goldens, deployment notes, final report

**Goal:** the run ends with the box able to run six paper expiries and Maulik holding one
document that says what is true.

- Goldens for L1 in `go/testdata/golden/L1/condor/` guarded by `test_condor_goldens.py`; a line
  in `docs/go-rewrite/REQUESTS.md`.
- The mutation report re-run; `tools/deploy` and compose gain the condor process and the four
  flags (false); `verify-condor.sh` on the box's side (`DRY_RUN=true`, three product/execution
  flags false, the monitor and collector flags as set, the next expiry date, the token state).
- `OC-FINAL-REPORT.md` at the repo root in the style of `SW-FINAL-REPORT.md`: what was built,
  what was decided, what is NOT done, what needs Maulik, the exact steps for the first paper
  expiry morning, and the six-expiry checklist of `02` §3.2.
- NEEDS-MAULIK § Condor reduced to hands-only items.
- **AC:** goldens byte-stable; both suites green; `verify-condor.sh` green locally against the
  dev stack; the report exists and STATUS's ledger is all ✅ except what the report names.
