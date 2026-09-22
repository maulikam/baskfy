# 06 — Module plan: OP0–OP15

One commit per module, `OP<N>: green — <one line>`. A module is green when its acceptance criteria
pass as tests (or, for a visible surface, when the page renders in the dev stack and a browser/E2E
check covers it), `make lint` is clean in the touched trees, **both trees' suites still pass** (the
desk must rebalance on any Friday; swing, TWT and VBT must run on any morning), and `STATUS.md` +
(if judgement was exercised) `DECISIONS-OP.md` are updated. Criteria are proxies for Goals — the
charter's precedence order applies.

**Order: the scans ship first.** OP0–OP5 end with the Options tab showing all three sleeves' live
candidates on the web. Plans, paper execution and the journal follow.

```
OP0 → OP1 → OP2 → OP3 → OP4 → OP5            (scans on the web)
                          OP4 → {OP6, OP7, OP8} → OP9 → OP10 → OP11
                   OP3 + OP4 → OP12 (Tiers 1–2; Tier 3 data-blocked)
                                 OP11 + OP12 → OP13 → OP14 → OP15
```

Nothing in this plan edits `frozen/`, the gateway's product gates, or the overnight-option guard. (One exception, found by OP0 and made in OP2: the product gate was *tightened* so a derivative venue admits MIS only and MIS needs `INTRADAY_ENABLED` there too — `DECISIONS-OP` OP0.6/OP2.1; this sentence was the stale half.)
Nothing flips a money flag. **Data-blocked** modules are marked ⛁ and say what they wait for.

## Where the condor modules went

| OC | Folded into | What changed |
|---|---|---|
| OC0 baseline | OP0 | + weekly master, STT verification, `OPTIONS_ENABLED` side-door inventory, route collision |
| OC1 pure core | OP1 (shared) + OP4 (O1 signal/structure/exits) | `baskfy_core.condor` → `baskfy_core.options.condor`; greeks via Black-76 on the parity forward |
| OC2 schema | OP2 | `oc_` → `op_`; the NFO master is `op_contract`, not `instrument` |
| OC3 reads + collector | OP3 | two expiries, one call a minute; `quote()` extended for depth/OI |
| OC4 API + hub | OP5 | one Options tab for all sleeves |
| OC5 plan builder | OP6 (O1), OP7 (O2), OP8 (O3) | |
| OC6 desk process | OP9 | one `options_monitor` for all sleeves |
| OC7 desk page + execute | OP10 | |
| OC8 journal + ledger | OP11 | per-sleeve R limits, book ₹ limits |
| OC9 backtest | OP12 | per sleeve; Tier 3 ⛁ |
| OC10 gating proof | OP13 | + `OPTIONS_ENABLED` side door, + no auto-execute scan |
| OC11 hardening | OP14 | |
| OC12 verification + report | OP15 | `OP-FINAL-REPORT.md` |

---

### OP0 — Baseline, read-in, and the facts this run stands on

**Goal:** a fresh session knows where it stands, cannot damage what exists, and has *verified* the
facts `04` and `07` depend on.

- Record in STATUS: branch, HEAD, others' dirty files (never committed under an OP module), both
  suites' pass counts, Alembic head, Beat inventory, Kite token state, `DRY_RUN` /
  `OPTIONS_ENABLED` / `INTRADAY_ENABLED` in every env file (`true`/`false`/`false`), the desk's
  `LOCKED_KEYS`.
- **Verify with rate-limited read-only calls and record** (condor OC0's five plus): (a) the earliest
  date `historical_data(interval="minute")` serves for the NIFTY 50 and INDIA VIX tokens; (b) that a
  last-week NIFTY weekly option is absent from `instruments("NFO")` and its history cannot be
  queried (the error text); (c) the master's NIFTY expiries for the next eight weeks — weekday,
  `lot_size`, `tick_size`, strike step, and which one is the monthly; (d) `margins()` — is the F&O
  segment enabled; (e) Kite `quote()` on five NIFTY options — the fields returned (depth levels, OI
  and its unit, `oi_day_high/low`, timestamps); (f) `basket_order_margins` answers for a fixture
  four-leg basket (read-only).
- **Verify from primary sources and record with URL and date**: the current STT rates on option
  sale and exercise (`04` §6's ⚠), NSE transaction and IPFT charges, Zerodha's MIS auto-square-off
  time and charge for NFO, the NSE circular for NIFTY's Tuesday expiry, and what Zerodha currently
  requires of API orders under the retail-algo framework. Where a fact cannot be verified, STATUS
  says so; `04` is edited only for verified facts.
- **Inventory what `OPTIONS_ENABLED=true` would wake in the desk today** (`02` Track B): the
  `/options*` routes, `_options_operations()`, the autorun options loop, the partial
  `app/strategies/strangle/` in the live tree — read-only, no flag set.
- Inventory, read-only: `kite-momentum-rebalancer/data/outputs/strangle_*` and any
  `options_forward.jsonl` (rows, date ranges, fields) for `07` §1.
- Open **Options** in `NEEDS-MAULIK.md` with `QUESTIONS.md`'s hands-only items.
- **AC:** STATUS lets a reader with no other context name every table, suite, flag and verified
  fact above; both suites green at baseline; the NEEDS-MAULIK heading exists.

### OP1 — The shared pure core `baskfy_core.options`

**Goal:** the calendar, greeks, costs, sizing, session machine, risk ledger and journal as pure,
typed arithmetic a test asserts against `04` with no db, network, disk or clock.

- `packages/core/src/baskfy_core/options/`: `config.py` (every `04` field; distinct
  `condor_monthly`/`condor_weekly` instances), `calendar.py` (§1), `greeks.py` + `chain.py` (§2),
  `costs.py` (§6 incl. `exercise_stt`), `sizing.py` (§7), `session.py` (§11), `risk.py` (§9),
  `journal.py` (§12), `execution.py` (§8's pure decisions: sequences, reprice, the depth-ladder
  simulator), `backtest.py` (engine only).
- Port by re-implementation from the frozen lab (`options.py`, `options_costs.py`,
  `strangle/fills_paper.py`, `strangle/calendar_nse.py`) naming the source in each docstring;
  fixtures store numbers computed at authoring time; nothing imports `frozen/` (PACK.2).
- Tests: `test_options_calendar.py` (the holiday-shift fixture defeats a weekday rule; O1-W refuses
  the monthly Tuesday), `test_options_greeks.py` (parity forward; Black-76 put–call parity to 1e-9;
  IV round-trips; refusals), `test_options_costs.py` (each rate a field; the STT-trap test of §6.3),
  `test_options_sizing.py` (PAPER_ONE_LOT; `NO_SLEEVE_CAPITAL` live; ceilings),
  `test_options_session.py` (random edge walks), `test_options_risk.py`, `test_options_journal.py`
  (never pools sleeves/simulated/sizing modes), `test_options_purity.py`,
  `test_options_docs_parity.py` (every config field name appears in `04`).
- Mutation harness at the `factors` threshold; score recorded.
- **AC:** suite green; `mypy --strict`, `ruff`, `test_no_escape_hatches.py` clean; purity green.

### OP2 — Schema, the NFO master, settings, `options_gates()`

**Goal:** `03` migrated, seeded and idempotent, the calendar reading only the master, the M4.1
settings boundary intact.

- Migration `<next>_options.py` (all of `03`; `op_chain_snapshot` partitioned by month; `op_sleeve`
  enum); models in `models/options.py`.
- The nightly instruments task persists NIFTY CE/PE rows into `op_contract` (never deleting) and
  rebuilds `op_expiry`; alert on a lot-size change or a moved expiry.
- `op_event_day` seed (`baskfy_worker/seeds/options_event_days.py`, each date's source URL beside
  it; unverifiable dates omitted — `04` §1.3).
- Settings: the six ceilings and nine flags of `02` in `baskfy_worker.settings`,
  `baskfy_api.settings` and the desk's `config.py`; `options_gates(sleeve)` in desk and worker;
  `OptionsSettings` validating `op_*_config` against ceilings with `settings_audit`;
  `tests/test_settings_boundary.py` extended (ceilings never form fields; `OPTIONS_ENABLED` /
  `INTRADAY_ENABLED` stay `LOCKED_KEYS`); root `.env.example` gains the options block.
- **AC:** migrate → seed → migrate is a no-op; the calendar's next monthly equals the master's
  (fixture with a shifted expiry moves it); every user-scoped `op_` table has `user_id`;
  `options_gates()` returns `PAPER` for every combination but all-four-true, per sleeve (a 16-row
  table × 4 sleeves); adding `BANKNIFTY` to any config is a 422.

### OP3 — Provider reads, index minute bars, the collector, the limiter

**Goal:** every input is a rate-limited read, and the forward dataset starts accumulating.

- `baskfy_providers.kite`: `quotes(..., exchange="NFO")` returns depth, OI and timestamps
  (`QuoteRecord` extended, or an `OptionQuoteRecord` — record the choice); `minute_bars(token, start,
  end)` chunked per Kite's minute-interval window; `basket_order_margins(legs)` read-only;
  fixtures and the M4 fake client extended.
- Worker tasks: `baskfy.options.index_bars` (each minute in session + EOD reconcile) into
  `op_index_minute`; `baskfy.options.backfill_index_bars(from, to)` resumable; `baskfy.options.
  collect_chain` (Beat each minute 09:15–15:30 on trading days, behind
  `BASKFY_OPTIONS_COLLECT_ENABLED`) computing forward/IV/greeks at write.
- **Measure the limiter share** with the swing morning's reads replayed from its timing probe
  (`docs/swing/status/S2-kite-timing.md`): the options reads (≤ 2 calls/minute: chain + index bars)
  fit inside the 3 req/s budget with headroom; numbers in STATUS. If they do, the run sets the
  **default** of `BASKFY_OPTIONS_COLLECT_ENABLED` and `BASKFY_OPTIONS_SCAN_ENABLED` to true
  (PACK.11 — operational, moves no money) and records it.
  *(OP3, 22 Sep 2026: this sentence is the stale half for OP3 — by the orchestrator's instruction the
  collector shipped with its flag **default false**, and enabling it on the box after the live probe
  and the limiter proof is the orchestrator's call, not the module's; `DECISIONS-OP` OP3.2.)*
- **AC:** the fake client's bars and quotes round-trip idempotently; a quote batch never exceeds
  500 symbols or the limiter (test); the collector makes no call on a non-trading day; the Tier-1
  backfill runs on the dev stack for at least one full year and STATUS states the full backfill's
  duration. ⛁ the live measurements need a Kite login (NEEDS-MAULIK if absent).

### OP4 — The sleeves' signal cores and the scans

**Goal:** each sleeve's today's-candidates computation, pure, and a worker task that writes it
every minute.

- `options.condor` (condor `04` §2, §4, §7 with two variants), `options.directional` (§4),
  `options.expiry_setups` (§5), `options.scan` (§10) — each candidate built by the same functions the
  plan builder will call.
- Worker task `baskfy.options.scan` (each minute after the collector, behind
  `BASKFY_OPTIONS_SCAN_ENABLED`) → `op_scan` rows from `op_index_minute` + `op_chain_snapshot`.
- Fixtures: a quiet monthly expiry (O1-M `WOULD_TRADE`), a weekly trend expiry (O1-W `WOULD_SKIP`
  with ER and containment, O3-A `TRIGGERED`), a gap-hold expiry (O3-B), an O2 up-trend break day, an
  O2 counter-trend break (seen, not traded), a Monday (O2 uses Tuesday's contract), a Tuesday (O2
  uses next week's).
- Tests `test_options_condor.py` (condor's ER identities; delta ∧ range; never-naked property),
  `test_options_directional.py`, `test_options_expiry_setups.py`, `test_options_scan.py` (the
  scan's candidate equals a plan built from the same inputs; slot exclusivity).
- **AC:** every fixture yields its expected state, reasons and candidate to the rupee; the scan
  task is idempotent per minute.

### OP5 — API and the web **Options** tab (the scans ship)

**Goal:** Maulik opens Options on his phone and sees each sleeve's state and candidate, the chain,
the calendar — and can change nothing that moves money.

- Router `routers/options.py` with `05` §2's routes; OpenAPI and `packages/api-client` regenerated.
- Pages `/options`, `/options/journal` (empty states honest), `/options/calendar`, `/me/options`;
  the tab appended to `STAFF_BUILD_TABS`; the clock labels of `05` §2; the CLAUDE.md clock-table row.
- `test_options_readonly.py` on both sides: exactly two mutations (event day, settings), everything
  else 405; no import of `packages/execution` in `apps/web` or the router.
- Deploy notes for the scan + collector on the box (compose service, Beat entries, flags).
- **AC:** read-only test green; `GET /options/today` p95 < 200 ms on the dev stack; Playwright
  renders `/options` from fixtures showing an O1 `WOULD_SKIP` with all its reasons, an O2 `ARMED`
  with distance-to-trigger, an O3 candidate spread, and the `As of close` label outside hours.

### OP6 — O1 plan builder (monthly and weekly), costs pinned

**Goal:** at 10:00 on an O1 day the system produces a reasoned no-trade or one four-leg plan with
credit, lots, cost test and margin, into `op_plan`.

- `build_plan(O1M|O1W, date)`: gate → chain → `build_condor` → costs → sizing → margin →
  `op_plan`/`op_leg`, or `SKIPPED` with the code; `expires_at = min(now + 30 min, 10:15)`.
- `CostRates` pinned to OP0's verified sources; `OPTIONS_COST_RATES_REVIEWED_ON`.
- `AlertName.OPTIONS_PLAN` rendered in Mailpit.
- **AC:** the fixture morning yields the expected plan to the rupee; wide short-put spread →
  `REJECTED_COST`; only in-band call inside the OR → `REJECTED_NO_SHORT_CALL`; O1-W on the monthly
  Tuesday → no session; slot held by O3 → `REJECTED_SLOT_TAKEN`; idempotent per date.

### OP7 — O2 plan builder

**Goal:** on an O2 trigger the system produces a one-leg plan on the right contract with its stop,
target, time stop and lots.

- `build_plan(O2, date, trigger)`: day filters → trigger → `expiry_for_o2` → strike → delta and
  liquidity → costs → sizing (incl. premium cap) → plan; `expires_at = min(now + 30 min, 13:30)`.
- **AC:** Monday fixture uses Tuesday's contract, Tuesday fixture next week's; a counter-trend break
  plans nothing; `VIX_TOO_HIGH` and `GAP_TOO_BIG` days plan nothing with the reason; a 0.45-delta
  "ITM" strike on a fast day → `REJECTED_DELTA`; the gap-through worst case appears on the plan.

### OP8 — O3 plan builder

**Goal:** on an O3 trigger the system produces a two-leg debit spread plan with its exits and lots.

- `build_plan(O3A|O3B, date, trigger)` per `04` §5; slot logic; one O3 per day.
- **AC:** fixtures for each setup and each direction plan the expected strikes and debit; a debit
  above 55 % of width → `REJECTED_DEBIT`; O3-A and O3-B both firing → O3-B holds; a confirmed O1 →
  O3 `SLOT_TAKEN`.

### OP9 — The desk process `options_monitor`

**Goal:** on a trading day the desk observes from 09:15, raises each sleeve's plan at its moment,
and after a fill runs the exit engine every tick to the sleeve's hard exit.

- `app/strategies/options_monitor.py` (a `BaseStrategy`, the swing monitor as template;
  `generate_targets` returns `[]`, it never places; behind `BASKFY_OPTIONS_MONITOR_ENABLED`):
  subscribes NIFTY 50 on the `TickBus` from 09:15; builds bars from ticks; reconciles against
  `op_index_minute` at each checkpoint (A4); calls the plan builders; after `OPEN`, subscribes the
  legs and marks them (quote fallback ≤ 1 call / 5 s, the swing B10 pattern); `exits.evaluate` per
  tick → an exit plan handed to OP10's executor. `app/options_clock.py`.
- `tools/options/replay.py` — a recorded day (minute CSV + snapshot series) through the process,
  asserting every plan and exit decision.
- **AC:** flag off → not instantiated; the O1 fixture replays to `PROFIT` at the expected minute;
  O2's fixture to `TIME_STOP`; O3-A's to `TARGET`; the index feed dying at 14:05 with O1 open →
  `HARD_EXIT / FEED_LOST`; a restart at 11:30 resumes from `op_position`; a source scan finds no
  `place(` in the module.

### OP10 — The desk page and `/nifty-options/execute` (paper)

**Goal:** one click confirms; legs go in the right order through the real gateway's dry-run branch;
exits run under the same confirm; everything is flat by its hard exit; nothing reaches a broker.

- `app/options_execute.py`: `execute_entry(plan)` and `execute_exit(position, decision)` through
  `OrderGateway.place` (`MIS`, `NFO`, `LIMIT`, minted `client_id`), fills from the depth-ladder
  simulator when the sleeve is `PAPER`; `PgOptionsStore` (sqlite twin in tests); `op_order` rows.
- Page `/nifty-options` per `05` §3 with the Confirm sentences verbatim.
- **AC:** with `DRY_RUN=true` and every flag false: O1 confirm → four simulated fills wings-first
  and `OPEN`; thin long-call depth → `ABANDONED_ENTRY`, no short ever sent; O3 partial long →
  abandoned, no short sent; an exit produces closes in sequence with the final attempt marketable; a
  gateway spy records **0 broker calls** under both `DRY_RUN` values with the execution flag false;
  never-naked holds across 500 seeded fill sequences per structure; no path calls `place_gtt_stop`.

### OP11 — Journal, ledger, pauses, first-live multiplier

**Goal:** every close writes the record, and the record governs tomorrow.

- Close → `op_journal` with the cost breakdown and MAE/MFE; `risk.Ledger` at every close and at
  09:00; pauses audited; `plan.build` refuses while paused; `OPTIONS_WEEKLY` alert; `/options/
  journal` filled.
- **AC:** −2R intraday on O2 → `DAILY_R`, position closed, sleeve paused today; four −1R O1-W weeks
  → `MONTHLY_R`; a book ₹ breach closes every open position; five real rows lift `half_size`; no
  summary pools sleeves, simulated, or sizing modes.

### OP12 — Backtests: Tier 1 and Tier 2 per sleeve; Tier 3 when data exists ⛁

**Goal:** the page shows what the data can honestly support, per sleeve, tier by tier.

- India VIX daily history into `index_snapshot_daily`; `tools/options/backtest.py --sleeve --tier
  --from --to` and the worker task on the compute queue; `op_backtest_run`; cards with caveats from
  the row.
- Tier 1 all sleeves with ±25 % sensitivity on each threshold; Tier 2 all sleeves with `04` §13.2's
  caveats; the NSE F&O bhavcopy check of `07` §2 (Tier 2's modelled close vs the bhavcopy close for
  the strikes it used) if OP0 found the file reachable through the NSE provider.
- **⛁ Tier 3** needs `op_chain_snapshot` days: it runs over whatever exists (fixture days at minimum)
  and becomes meaningful only after the collector has run on the box for weeks; or on vendor data
  (QUESTIONS Q5).
- **AC:** Tier 1 over the full backfill runs end to end per sleeve; a planted Tier 2 day reproduces
  its expected R to 0.01; Tier 3 over fixture snapshots reproduces OP10's drill P&L to the rupee; the
  caveat text equals `07`'s verbatim; no card mixes tiers or sleeves.

### OP13 — Gating and safety proof

**Goal:** every Track B/C claim of `02` is a test, and the drill proves a whole day with **0 orders
reaching a broker**.

- Hypothesis over chains, fills and tick paths: never naked (O1, O3); never overnight (no product
  but `MIS` constructible; the guard's refusal exercised as an integration test with
  `OPTIONS_ENABLED=true` and `NRML` forced); no second entry per sleeve per day; exit plans close
  exactly the position's legs.
- The four-flag AND per sleeve with a gateway spy; with a sleeve's execution flag **true** but
  `OPTIONS_ENABLED` false, the gateway itself refuses every leg.
- **The side door**: with `OPTIONS_ENABLED=true`, the frozen lab's `/options*` routes and ops hooks
  stay dark while `frozen/` is not thawed (fix by gating those hooks on their own absent-module check
  *in the live tree*, not by touching `frozen/`; record the choice).
- Source scans (docstrings stripped): no gateway import in web/API/worker options code; no
  `BASKFY_OPTIONS_*AUTO*` read anywhere; no scheduler or Beat entry targets `/nifty-options/execute`.
- `tools/options/drill.py`: a full paper day per sleeve against Postgres, and a skip day.
- **AC:** all green; the drill prints `sleeve=O1M confirms=1 fills=8 orders_to_broker=0` (and the
  O2/O3 equivalents) and the journal rows; desk suite green.

### OP14 — Hardening and observability

**Goal:** the day it goes wrong, it goes wrong loudly and safely.

- `05` §4's Prometheus rules; in-process checks at 09:20 (sessions exist), 10:20 (O1 verdict or
  plan on an O1 day), each hard exit + 3 min (nothing open), 15:35 (collector wrote ≥ 360 minutes);
  runbook entry.
- Restart resume; stale-quote and feed-loss behaviour; token death with a position open → immediate
  `HARD_EXIT / FEED_LOST`, and NEEDS-MAULIK says what the human does then.
- Budgets measured and recorded: tick → mark → decision latency, plan build time, the limiter share
  with swing, TWT and VBT mornings running.
- **AC:** each rule fires on a synthetic series; the checks fire on fixtures; budgets in STATUS.

### OP15 — Verification, goldens, deploy, final report

**Goal:** the box can run every sleeve's paper period, and Maulik holds one document that says
what is true.

- Goldens for the Go rewrite in `go/testdata/golden/L1/options/` + a line in
  `docs/go-rewrite/REQUESTS.md`; mutation report re-run; compose and `tools/deploy` gain the monitor
  and the flags (money flags false); `verify-options.sh` (flags, next expiry, token state).
- `OP-FINAL-REPORT.md` at the repo root (style of `SW-FINAL-REPORT.md`): built, decided, NOT done,
  needs Maulik, the first paper morning step by step, and `02` §3.2's paper checklist per sleeve.
- **AC:** goldens byte-stable; both suites green; `verify-options.sh` green against the dev stack;
  STATUS all ✅ except what the report names.

**After OP15, outside the run ⛁:** the paper periods themselves (`02` §3.2) — O2's 60 sessions take
about three months, O1-M's six monthly expiries about six.
