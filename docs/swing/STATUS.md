# SW run — live status

The status page for the swing run. Updated at the end of every module, loud about what is NOT
done. A fresh session resumes from the first module not marked ✅.

**Run state: in progress.** Started 2 Sep 2026 on branch `developer`.

## Module ledger

| Module | State | One line |
|---|---|---|
| SW0 — Baseline and read-in | ✅ | Both suites green at baseline; the numbers, the data date, the Alembic head, the Beat inventory and every `DRY_RUN` are recorded below |
| SW1 — The pure core, re-verified | ✅ | Green in the repo's own `uv` environment; in the mutation harness for the first time, and three new contract modules took it from **41.0% to 83.4%** — above `factors`' 77.5% |
| SW2 — Schema and settings | ✅ | Twelve `sw_` tables migrated and round-tripped, `sw_config` seeded at zero capital, the three flags and three ceilings wired into API, worker and desk, and a ceiling can never become a form field |
| SW3 — Daily detection job | ✅ | `baskfy.swing.detect` writes `sw_setup_daily` + `sw_market_daily`, wired in as the chain's twelfth step (unable to fail the night), with `make swing DATE=…` and two Beat entries |
| SW4 — API + Setups/Market pages | ✅ | Five `/swing` routes (four reads, one bounded settings write), the Setups and Market pages, and two read-only tests — one per side of the wire |
| SW5 — Watchlist, plan preview, EOD, alert | ✅ | The evening job manages the book, plans tomorrow with its skips, fills and prunes the watchlist, emails the summary and counts the session; two more pages and four more routes |
| SW6 — Premarket EP scan + opening-range monitor | ✅ | The morning job refreshes levels, scans the pre-open for gaps behind its flag (≤ 500 a call, inside the limiter) and rebuilds the plan as `MORNING`; the desk's opening-range monitor raises `sw_signal` rows and one-line `SIGNAL` plans behind its flag, holds no gateway, and replays a fixture morning exactly |
| SW7 — Desk page + `/swing/execute` (DRY_RUN) | ✅ | ✅ execute logic: `app/swing_execute.py` turns a confirmed line into a LIMIT buy + a GTT in the same call, a market sell that re-sizes the stop, or a raised stop — through the real gateway in its dry-run branch, 70 tests, 0 orders reach a broker · ✅ page: `GET /swing` with its three panels and status bar, `PgSwingStore` over the desk's Postgres adapter (sqlite twin in tests), `POST /swing/execute` / `/swing/rearm` through `execute_line` — 73 tests, the buy → position → GTT path proven end to end over an exploding broker client |
| SW8 — Journal + ladder closes the loop | ✅ | ✅ ladder + API: the evening settles the rung and writes it to `sw_config` (audited, `swing-eod`) and the day's market row, and the plan is built with it; `GET /swing/journal` answers C2's shape — real and simulated cards apart, the six-bucket histogram, by setup, by month, the ladder card, 14-of-20 · ✅ page: `/swing/journal` renders it — two cards that never mix, six bars that read at zero, the one sentence on what the next close does to the ladder, "14 of 20 paper sessions logged", and the backtest heading with its caveats verbatim or an honest "not run yet"; 30 rendered-DOM tests, the fifth tab in the row |
| SW9 — EOD backtest | ✅ | ✅ core (1.3.1): `baskfy_core.swing.backtest` runs `04` §11 through the live book's own functions; a planted flag reproduces R = 0.28 to the paisa; 300 × 8y in 24 s · ✅ runner (1.3.2): `sw_backtest_run` (migration 0029, append-only), `baskfy.swing.backtest` on the compute queue loading bars as the detectors do plus the names that died inside the run, `tools/swing/backtest.py` (`--fixture` with no database, or the task body against `BASKFY_DATABASE_URL`), `GET /swing/journal.backtest` as C2's card with the caveats verbatim, and the page drawing the run's R distribution, win rate and expectancy with the journal cards' own tiles and bars — 18 + 6 + 6 tests; the 2017→ run itself is **not measurable on this machine** (ten sessions of bars, database at 0026) and extrapolates to 4–5 min against the 30 |
| SW9.5 — Reconcile with the primary sources | ✅ | The rules are his, quoted (`07`): the stop is one ADR or tighter and a wider one is skipped, the gate's index rule is the 10-day over the 20-day, at most three new entries a session, the plan takes `min(rung, max_open_positions)` with the top rung at 10, the sleeve locks out new entries 15 % below its peak until back within 10 % (settled by the evening from the sleeve's own NAV, migration `0030`), the swing GTT rests 3 % under its trigger through an additive keyword the weekly book never sees, ceilings 30 % / 20, ADR floor 4.0 — the patch applied, 41 red tests re-pinned by re-deriving each number, 7 acceptance tests in `test_swing_primary_sources.py`, 4 backtest cases and 9 ladder cases new; core **2665 passed**, G5 **445 passed**, execution **170**, desk **1553 passed, 17 skipped** |
| SW9.6 — The index rule, the drawdown, gate-on vs gate-off in the backtest | ✅ | STANDING-ANSWERS A12 and B1–B5: `run_backtest` takes the benchmark's closes (NIFTY 500 from `index_snapshot_daily`, NIFTY 50 fallback, resolved once per run) and reads the 10/20 averages in-frame up to and including the session's own close (a look-ahead test shifts the series by one session and the gate moves by exactly one); the drawdown lock-out runs on the constant-sleeve curve, peak-to-trough as % of the sleeve, with the live 15 %/10 % hysteresis (a 16.99 % drawdown locks, 10.84 % holds, 9.52 % releases); three books over one detection pass — gate off, breadth only, full — reported per year entered and per setup with breadth's and the index rule's contributions as differences; `DELISTED` beside `NO_BAR`, B1's circuit caveat verbatim in `04` §11, the card and the page draw the drawdown and the two comparison tables; core backtest **68 passed**, speed **26.9 s** for three books, worker **24**, API **43**, web **42**, `make lint` clean |
| SW10 — Gating and safety proof | ✅ | Every Track-B/C claim is a test: hypothesis over random watchlists (no `PARABOLIC_SHORT` line, a stop never falls, a SELL never exceeds the book), a spy over the real gateway through `/swing/execute` (eight calls, all dry, under both `DRY_RUN` values), source scans with docstrings/comments stripped over the web hub, the API and the monitor, every `sw_` write named with its `user_id`; **the confirm-time gate (STANDING-ANSWERS A5, SW10.4)**: `POST /swing/execute` re-derives the book under `SELECT … FOR UPDATE` on the day's `sw_session` and re-sizes or refuses (`EXPOSURE_FULL` / `TIER_FULL` / `SESSION_CAP`), the monitor re-reads context per trigger, 500 seeded confirm sequences never exceed the ceiling, the count or the cap; and `tools/swing/drill.py` runs the whole paper session against Postgres — evening → LEVELS → MORNING → replayed morning through `PgSignalStore` → two confirms through `execute_line` (the second re-sized 833 → 389) → EOD → next morning — **0 orders reach a broker**, `confirms=2 fills=2`, **`EXPOSURE after confirms … = 25.0%`** (was 34.3 %; SW10.2 closed). Desk **1,599 passed** |
| SW10.5 — Maulik's review corrections (A7, A8, A9, A10, A14, B7) | ✅ | STANDING-ANSWERS applied across core, worker, API, gateway and desk: a live gap is a `PENDING_RANGE` line holding a session slot, released once at its trigger or freed at 10:45, never executable (route + module + source set); the live buy is a marketable LIMIT (`min(trigger × 1.005, range high + 0.25 ADR)`) polled ≤ 10 s at ≤ 2 req/s, a partial is `SENT` with a GTT for exactly what filled, later fills grow the position and **modify** the GTT through a new guarded `modify_gtt_quantity`, the 10:45 sweep cancels remainders through a new guarded `cancel_order`; half risk at plan time via `risk_multiplier` while the countdown runs and a confirm would be real, the countdown moved once per LIVE session by the evening; the ladder reads real closes from day one with a 09:09 catch-up; top-20 auto-watch, top-5 + every-EP focus, MANUAL rows on a ten-session clock with `PATCH … {"reconfirm": true}`; migration `0031`; the drill replays five signals and a late partial fill — **0 orders reach a broker**. Core +26, execution +18, worker +23, API +3, desk **1,644 passed** |
| SW11 — Hardening and observability | ⬜ | |
| SW12 — Verification, goldens, final report | ⬜ | |

States: ⬜ not started · 🔄 in progress · ✅ green · ⛔ blocked · 🟡 partial.

---

## SW0 — Baseline and read-in ✅

Everything below was **measured on this machine on 2 Sep 2026**, not recalled. A reader with no
other context should be able to name the tables, suites, data date and flags this run builds on
from this section alone.

### Repository

| | |
|---|---|
| Branch | `developer`, HEAD `4e2c7ba` ("M81/M82: connecting pulls the holdings…") |
| Trees | `decile-blueprint/` (screener + API + worker + web), `kite-momentum-rebalancer/` (the desk), `frozen/strangle/` (untouched) |
| **Another session's dirty files** | `decile-blueprint/apps/web/src/app/(app)/portfolio/activity/page.tsx` and its `__tests__/` were modified before this run started. **They are not this run's and are not committed under any SW module.** |
| Pack files adopted by this run | `docs/swing/*`, the `.env.example` swing block, `packages/core/src/baskfy_core/swing/` and `packages/core/tests/{swing_fixtures,test_swing_*}.py` |

### Both suites, at baseline

| Suite | Command | Result |
|---|---|---|
| The desk | `DRY_RUN=true .venv/bin/python -m pytest -q` in `kite-momentum-rebalancer/` | **1330 passed, 17 skipped, 12 subtests passed**, 54.8 s, exit 0 |
| The screener | `uv run pytest -q` in `decile-blueprint/` | **3383 passed, 1184 skipped, 0 failed** (see the note below on how this was counted) |

Two things a future session needs to know about the screener run:

1. **Do not `source .env` before running it.** The first baseline run of this session did, and
   two provider tests failed — `test_cli_doctor.py::test_kite_is_reported_unavailable_with_the_reason`
   and `test_kite.py::test_unconfigured_is_unavailable_not_an_exception`. Both assert the message
   an *unconfigured* Kite provider produces; with `BASKFY_KITE_API_KEY` exported into the shell,
   the provider gets further and reports a different (correct) reason. The failures are an
   artefact of the shell, not of the tree: the same suite run without the export is green.
2. **The suite prints no final summary line** (`N passed in Xs`) on a full run — the last thing
   on stdout is the `[100%]` progress line. It does print one for any subset. The counts above
   were therefore taken by counting the progress characters (3517 `.` and 1184 `s` over 4701
   collected) and subtracting the **134 tests this session had already added** before the count
   was taken (`test_swing_docs_parity.py`, 109; the twelve new `sw_` rows in
   `test_schema_matches_docs.py`, 25). This quirk is pre-existing and not investigated; it is
   recorded here so the next session does not read a missing summary as a crash.

Swing core, measured separately under the repo's own `uv` environment (this is SW1's evidence,
recorded here because it was run during the baseline): **103 passed** across the seven
`test_swing_*.py` files, **8 passed** for `test_no_escape_hatches.py`, `ruff check` and
`ruff format --check` clean over 18 files, `mypy --strict` clean over the same 18.

### Data and schema

| | |
|---|---|
| Alembic head in the repository | `0027_instrument_null_dedup` |
| Alembic head in the local dev database (`baskfy` on `localhost:5433`) | `0026_auth_identity` — **the dev database is one migration behind the tree**; `make migrate` before any SW3+ work against it |
| `ohlcv_daily` in the local dev database | 1,800 rows, **2026-08-17 → 2026-08-28**, 180 instruments |
| `factor_daily` in the local dev database | **0 rows** |
| `instrument` | 200 rows |
| Latest `pipeline_run.data_version` | 104 |
| The `baskfy-staging-*` compose stack | running, but its Postgres is at `0024_portfolio_kind_default` with **no instruments and no bars** — it is not the staging box `GATES.md` G6 refers to |

**This is the single most important fact for SW3.** Ten sessions of bars cannot feed a detector
that needs 125. SW3's acceptance criterion "a `sw_setup_daily` row for a real date exists on the
dev stack" cannot be met by the data currently on this machine; SW3 says what it did about that.

### Flags and safety

| | |
|---|---|
| `kite-momentum-rebalancer/.env` | `DRY_RUN=true` |
| `decile-blueprint/.env` | no `DRY_RUN` key (the screener places nothing; law 2) |
| `BASKFY_SWING_EXECUTION_ENABLED` | documented `false` in the root `.env.example`, unset in every `.env` |
| `BASKFY_SWING_MONITOR_ENABLED` | same |
| `BASKFY_SWING_EP_PREMARKET_ENABLED` | same |
| `INTRADAY_ENABLED` / `OPTIONS_ENABLED` | `false` (desk `config.py` defaults; unset in `.env`) |
| Kite token | not exercised in this run; no live order path was touched |

### The Beat schedule this run adds to

`baskfy_worker.celery_app.BEAT_SCHEDULE`, IST, as of the baseline — 17 entries. The ones the
swing jobs sit beside:

| Entry | When |
|---|---|
| `refresh-reference-data` (`baskfy.pipeline.nightly`) | 18:45 Mon–Fri |
| `cb-eod-metrics` | 20:20 Mon–Fri |
| `cb-dividends` | 20:25 Mon–Fri |
| `dispatch-screen-alerts` | 20:30 Mon–Fri |
| `portfolio-eod-nav` | 20:35 Mon–Fri |
| `cb-sip-reminders` | 09:00 Mon–Fri |
| `cb-rebalance-notify` | 09:30 Mon–Fri |
| `weekly-integrity-audit` | Sat 02:00 |
| `kite-token-expiry` | hourly at :05 |

So `swing-premarket` (08:50/09:09), the monitor window (09:15–10:45), `swing-eod` (after
publish) and `swing-weekend` (Sat 07:00) land in gaps rather than on top of anything.

### What SW0 changed

Nothing executable. The `docs/swing/` pack is committed so the run has a specification in the
repository rather than in a session's context.

---

## SW1 — The pure core, re-verified and adopted ✅

### What was re-verified

Run under the repository's own `uv` environment on macOS, not the pack author's Linux venv:

| | |
|---|---|
| `pytest packages/core/tests/test_swing_*.py` | **103 passed** — the pack's number, reproduced |
| `pytest packages/core/tests/test_no_escape_hatches.py` | **8 passed** |
| `ruff check` + `ruff format --check` over the 18 pack files | clean |
| `mypy --strict` over the same 18 | clean |

Nothing in `baskfy_core.swing` was changed. SW1's instruction is "fix nothing in the module
unless a test fails here", and none did.

### `test_swing_docs_parity.py` (new, 109 tests)

Every field of `baskfy_core.swing.config` — all 69 of them — must be named in
`docs/swing/04-business-rules.md`, and so must every enum value the engine can write to a `sw_`
row or show on a page (`Setup`, `CandidateStatus`, `MarketGate`, `LineKind`, `SkipReason`,
`ActionKind`, `ActionReason`, `TrailMa`, `StopMode`). Direction matters and is deliberate: code →
document, not the reverse, because `04` legitimately names quantities that are columns rather
than config fields.

It found one real gap on its first run: `04` §6.1 described the default stop without naming
`LOW_OF_DAY`. The document now names both modes (DECISIONS-SW SW0.1).

### The mutation harness, and what it found

`tools/mutation.py` learned the eight `baskfy_core.swing` modules. Two changes were needed
before the number meant anything, and both are recorded in DECISIONS-SW SW1.1: a **per-target
test selection** (a swing mutant scored against the factor suites survives every time, because
those suites never import the package) and a **path fix** in `generate()` (it recorded a
target's basename, so `swing/setups.py` would have been written to `baskfy_core/setups.py` and
every swing mutant would have been scored against an unmutated package).

**The first honest run scored 41.0%**, against `factors`' 77.5% — and `setups.py` scored 19.1%,
with 140 of its 173 mutants surviving. The survivors were two families: comparison boundaries
(`>=` read as `>`) and terms of the scoring formulas. Neither is visible to a test that asks
"was this shape detected?".

Three modules were added — **273 tests**, none of them touching the seven the pack shipped:

| Module | Tests | What it pins |
|---|---|---|
| `test_swing_contract_detectors.py` | 64 | `04` §1–§4. Every threshold tested *at* its own value by setting the threshold to the measurement the engine produced; every score recomputed from the document's formula |
| `test_swing_contract_book.py` | 131 | `04` §5–§10. Sizing's four caps and four refusals, the stop rules' precedence and their day-3/day-5 boundaries, the gate, the ladder, the opening range, the plan's skip order, the journal's statistics |
| `test_swing_contract_edges.py` | 78 | The measurements themselves, recomputed from the **raw fixture bars** by a second implementation; the defaults and guards (`fill_null(1.0)`, `turnover > 0`, the first bar's up-streak); and `frozen=True` on every dataclass the engine hands out |

**Recorded score: 41.0% → 67.8% → 83.4%** over three runs of the same 459 mutants (383 killed,
76 survived), which is above `factors`' 77.5%. Per module: `indicators` 97.4%, `market` 94.8%,
`journal` 92.9%, `stops` 88.9%, `sizing` 87.2%, `opening_range` 83.7%, `plan` 78.6%,
`setups` 73.4%.

Of the 76 survivors, **27 are `slots=True` on a `@dataclass` decorator** — the same equivalent
mutant `reconciliation/MUTANTS.md` already justifies for `factors`: `slots` changes an instance's
layout, not any value. The `frozen=True` half of every one of those decorators is now killed, by
`TestNothingTheEngineHandsOutCanBeMutated`.

`reconciliation/MUTANTS.md` is regenerated by `make mutants`, which now covers `factors`,
`screener` and the eight swing modules.

### What SW1 did NOT do

- It did not change `baskfy_core.swing`. Every number the engine computes is the pack's.
- It did not add the `+5/+5` score adjustments of `04` §2.6 — those need `instrument.listed_on`
  and `index_member_daily`, which core must not read. SW3 owns them.
- The surviving mutants are **not** individually justified in `reconciliation/MUTANTS.md` the way
  `factors`' are; the report lists them and the run continued. SW12 is where that list should be
  read again.

---

## SW2 — Schema and settings ✅

### The schema

Migration `0028_swing`, **twelve tables** (`03` describes eleven; the twelfth is the settings
audit — DECISIONS-SW SW2.2):

`sw_config` · `sw_config_audit` · `sw_setup_daily` · `sw_market_daily` · `sw_watch` ·
`sw_signal` · `sw_plan` · `sw_plan_line` · `sw_plan_skip` · `sw_position` · `sw_fill` ·
`sw_session`

Models in `packages/core/src/baskfy_core/models/swing.py`. Every status, kind, reason and gate
string in a check constraint is **imported from `baskfy_core.swing`** rather than retyped, so a
detector cannot emit a value the database rejects.

Verified on a scratch database (`baskfy_sw_mig`): `upgrade → downgrade → upgrade` leaves exactly
twelve `sw_` tables and no residue, and every modelled column exists with the modelled
nullability.

Two decisions:

- **SW2.1** — `sw_setup_daily` is keyed `(user_id, date, instrument_id, setup)` and
  `sw_market_daily` `(user_id, date)`, against `03`'s sketch, because `02` Track C §6 requires
  `user_id` on every row and both tables are computed through per-user liquidity floors.
- **SW2.2** — the settings audit is a table, not two columns.

### The settings, and the boundary

| Where | What |
|---|---|
| root `.env.example` | the three flags and three ceilings, all marked `# system-only` (the pack wrote this block; SW2 commits it) |
| `baskfy_api.settings` | `swing_execution_enabled`, `swing_monitor_enabled`, `swing_ep_premarket_enabled` (all `False`), the three `*_max` ceilings, `swing_index_slug` |
| `baskfy_worker.settings` | the same six, mirrored rather than imported (the worker does not depend on `baskfy_api`) |
| desk `app/config.py` | `SWING_EXECUTION_ENABLED`, `SWING_MONITOR_ENABLED`, `SWING_EP_PREMARKET_ENABLED`, and the swing GTT band (0.005–0.10, PACK.3) beside the weekly book's 0.08–0.12 |
| desk `app/analytics/settings.py` | all six `BASKFY_SWING_*` keys added to `LOCKED_KEYS` |
| `baskfy_api.swing_settings` | `SwingConfigPatch` (`extra="forbid"`), `SwingCeilings`, `apply_patch`, `record_system_change`, `audit_trail` |

`exposure_level` and `first_live_sessions_left` are **not fields of the patch model**. A person
who could set the exposure rung has deleted the ladder; they are written only through
`record_system_change`, which requires the job's name.

A value above a ceiling answers **422 `setting-above-ceiling`** carrying `field`, `requested`,
`ceiling` and `env_var` — a new problem type, argued in DECISIONS-SW SW2.3, because this API
reserves 400 for schema violations and a well-formed number over a limit is not one.

### Seeding

`baskfy_api.seed` gained `seed_swing_config` and a `swing` command. It writes one `sw_config` row
for the sole tenant with **`sleeve_capital_inr = 0`**, and `ON CONFLICT DO NOTHING` — so
re-running `make seed` never resets a capital or a risk setting a person has chosen. Zero capital
is the safety property: `baskfy_core.swing.sizing` refuses every entry with `NO_EQUITY` until
Maulik decides what the book may risk (`02` §3.4).

### Tests

| Suite | Result |
|---|---|
| `services/api/tests/test_swing_schema_and_settings.py` | **69 passed** with `BASKFY_TEST_DATABASE_URL` set (62 + 7 skipped without a database) |
| `kite-momentum-rebalancer/tests/test_settings_boundary.py` | **23 passed** — extended with `TestTheSwingBoundary` |
| `packages/core/tests/test_schema_matches_docs.py` | extended: the twelve tables, their primary keys, and a `docs/swing/03` documentation check |

Among them: a patch that crosses a ceiling on its second field changes **neither** field; an
accepted patch writes one audit row per field that actually moved (a no-op change writes none);
and `ck_sw_position_stop_never_below_initial` refuses an `UPDATE` that lowers a stop.

---

## SW3 — The daily detection job ✅

### What runs

| | |
|---|---|
| `baskfy_worker.tasks.swing.run_detect_swing` | the job: bars → indicators → detectors → exchange prices → score adjustments → `sw_setup_daily`, then breadth → gate → ladder → `sw_market_daily` |
| `PipelineStep.COMPUTE_SWING` | the chain's **twelfth** step, after `refresh_basket`, via `orchestrator.run_compute_swing_step` |
| `baskfy.swing.detect` (Beat, 21:00 IST Mon–Fri) | the same job again, in case the chain failed its quality gate — the bars are still there and yesterday's setups are still worth having |
| `baskfy.swing.weekend` (Beat, Sat 07:00 IST) | re-detects the last five sessions (`01` §8's weekend routine) |
| `make swing DATE=… [SESSIONS=…]` | the same body without Celery, printing the funnel |

### The three things the job does that core cannot

1. **Exchange prices.** Every level comes out of the detector *adjusted* and is divided by the
   row's `adj_factor` before storage, with the factor stored beside it. A person types a trigger
   into a broker and the broker has never heard of our adjustment.
2. **The circuit band, in the other direction.** `upper_circuit` is an exchange print and `high`
   is adjusted in place, so the band is multiplied by the factor on the way *in*. Without it, the
   morning after a 1:2 split every name would read as locked (or none would). DECISIONS-SW SW3.2.
3. **The two `+5`s of `04` §2.6**, which need `instrument.listed_on` and `index_member_daily` —
   tables law 1 forbids core from reading. Applied after the detector, capped at 100.

### The sector strip

`05` §2 says the strip comes from `market_health_daily`, which covers only the twelve **size**
universes — no sector index has a breadth row, so as specified it has nothing to read. The job
computes sector breadth itself over its own liquid universe and stores it in
`sw_market_daily.detail.sectors`; `SECTOR_INDEX_SLUGS` in `baskfy_core.universes` is the explicit
list of NSE's fifteen sectoral indices. DECISIONS-SW SW3.1.

### Tests — `services/worker/tests/test_swing_detect.py`, 25 passed

Every acceptance criterion, plus the ones the criteria imply:

- running the job twice for a date changes no rows, and there is still exactly one market row;
- a date with no published bars is `SKIPPED` with a reason, and a date where bars exist but none
  is dated today is `SKIPPED` with a **different** reason;
- a synthetic split inside a base (`adj_factor = 0.5`) stores a trigger equal to the raw price,
  and the row carries the factor;
- the step's failure leaves the run intact — asserted against `run_compute_swing_step`, which SW3
  extracted from the orchestrator so the guarantee can be tested for what it is;
- a deployment with no `BASKFY_SOLE_USER_ID` skips the step and says so;
- the young-listing `+5` is exactly 5.00 more than the same flag without it, and the bonuses
  cannot push a score past 100;
- the sector is read **for the detection date** — membership a month earlier does not count;
- the lookback is 200 *trading* days, not 200 calendar days.

### What SW3 did NOT do

- **Nothing has run the detectors over a real NSE day.** The dev database holds ten sessions of
  bars; the detector needs 125. The "on the dev stack" criterion is met against bars the test
  writes — a better test, and an honest gap. DECISIONS-SW SW3.3.
- The `sw_watch` auto-watch rules, the EOD plan and the alert are SW5's; this job writes no
  watchlist row and builds no plan.
- `sw_market_daily.exposure_level` is computed and stored but `sw_config.exposure_level` is not
  yet written back — SW8 closes that loop.

---

## SW4 — API and the Setups / Market pages ✅

### The API

| Route | What it answers |
|---|---|
| `GET /swing/setups?date&setup&status` | the day's candidates, **with the gate, the tier and the funnel** — so a page never makes a second call to learn whether the rows it is showing may be acted on |
| `GET /swing/setups/{instrument_id}/bars?date&count` | the mini chart's series: adjusted closes with their 10- and 20-day averages, `None` before each window is full |
| `GET /swing/market?from&to` | `sw_market_daily` over a span, oldest first |
| `GET /swing/sectors?date` | the strip, with how many of the day's candidates sit in each sector |
| `GET /swing/config` · `PATCH /swing/config` | the settings, the ceilings and the two read-only system fields |

`baskfy_api/swing.py` owns the SQL, `routers/swing.py` owns the HTTP — the split `market_data.py`
uses. Every route resolves the caller through `scoped_sole_user_id`, which **refuses** a principal
who is not the sole tenant rather than serving them somebody else's book (the M43.4 failure).

Prices and measurements are `Decimal` **all the way to the wire**: the response models declare
`Decimal`, and `canonical_json` serialises them with their stored digits. A `float` anywhere in
that chain turns a trigger of `149.60` into `149.6`, and that number is typed into a broker.

OpenAPI and `packages/api-client` regenerated.

### The pages

`/swing` (Setups) and `/swing/market`, both server-rendered, both `force-dynamic` (a setup is a
claim about *today*; the failure mode of a cached one is somebody acting on yesterday's pivot).

The Setups page answers three questions in the order a person asks them — is the tape worth
trading, which names are ready, where would each break out — and answers the first **above** the
list, because a list of setups above a RED gate is a list of trades not to take. The empty state
is written from the funnel, so "no flags today" says *how many names were liquid*.

`docs/swing/05` §1's "the Build hub gains a section tab Swing" is done: `/swing` lights **Build**
in the primary chrome rather than adding a sixth destination (HOME1 fixes it at five).

### Two read-only tests, one per side of the wire

| Test | What it asserts |
|---|---|
| `services/api/tests/test_swing_readonly.py` | five paths, four of them GET; the one PATCH is whitelisted by path; neither the router nor the service names the execution package, a broker or an order; no swing path contains `execute`/`gtt`/`order`; the two system-owned fields are not in the request model |
| `apps/web/src/lib/swing/__tests__/read-only.test.ts` | the pages declare no server action and render no form; the fetch helper issues no non-GET; **every endpoint it names is on a whitelist**, because `/swing/execute` would match any pattern that allowed `/swing/setups` |

`services/api/tests/test_api_swing.py` is the contract: the funnel survives an empty filter, the
gate rides with the candidates, `149.60` stays `149.60` on the wire, another account is refused,
and a setting above its ceiling answers 422 naming the ceiling.

### What SW4 did NOT do

- **The mini chart is an endpoint, not a drawing.** `GET /swing/setups/{id}/bars` returns the
  130-point series `05` §2 asks for and is tested; the SVG on the row is not drawn yet.
- **No Playwright check.** `05` §2's browser assertion ("renders the page with one flag and one
  locked EP and shows the lock icon") is not written; the page is covered by the read-only test
  and by the API contract, not by a rendered-DOM test.
- **No p95 measurement.** The `< 300 ms` budget for `/swing/setups` is not measured — SW11 owns
  the budget table (`benchmarks/AS-MEASURED.md`) and there is no 2,500-instrument dataset on this
  machine to measure against.
- The three remaining tabs (`Watchlist`, `Positions`, `Journal`) are SW5's and SW8's; the tab row
  carries the two that exist rather than showing three dead links.

---

## SW5 — Watchlist, plan preview, the EOD job and the alert ✅

### The evening, in order

`baskfy.swing.eod` (Beat 21:05 IST Mon–Fri, five minutes after the detectors) does four things,
and the order is the rule:

1. **Auto-watch and expire.** Every flag scoring `watch.auto_watch_min_score` [60] or better with
   status `SETTING_UP`, and **every** `GAP_DAY` EP whatever it scored — an EP is enterable for
   three sessions and there is no second chance to notice one. A `PARABOLIC_SHORT` is never
   watched (PACK.1: a watchlist is a list of things to buy). Rows past their expiry become
   `EXPIRED`, never deleted.
2. **Manage what is open.** `stops.manage` over every open `sw_position` with today's bar and its
   two averages. A position with **no bar today** is skipped rather than managed — a suspended
   name has not given the rules a close, and managing it against a stale bar would sell it on
   yesterday's information.
3. **Plan tomorrow.** `build_entries` over the watchlist with the day's tier, into an
   `sw_plan(source=EOD_PREVIEW)` with its lines and its skips. Exits first: `04` §9.3's "the money
   they free is the money the entries spend".
4. **Email, and count the session.** One `sw_session` row per session the system ran — the number
   `02` §3.2 gates the real-money flag on.

`04` gained **§9.5** and `baskfy_core.swing.config` gained **`WatchConfig`**, because the kickoff
requires every threshold to be a config field rather than a literal: `auto_watch_min_score` [60]
and `flag_valid_bars` [10] are the two new numbers, and the docs-parity test would have caught
either as a literal.

### The email (`05` §4)

`swing_eod` in `baskfy_api.email.templates`. The subject carries the two numbers that decide
whether it needs opening tonight — how many positions are unprotected and how many lines the plan
has. **The unprotected section is rendered even when it is empty** ("Every open position has a
resting stop"), because a section that appeared only when something was wrong would train the
reader to skim past the top of the message. A mail failure never fails the evening: the plan is
on the page whether or not the message arrived.

### Four more routes, two more pages

| Route | |
|---|---|
| `GET /swing/watch?state` | the list; `state=all` includes what expired and what was dismissed |
| `POST /swing/watch` | add a name by hand — no expiry, and re-adding one updates its levels rather than making a second row that would spend the cash twice |
| `PATCH /swing/watch/{id}` | the note and the catalyst, **and nothing else** |
| `DELETE /swing/watch/{id}` | a state change to `DISMISSED` |
| `GET /swing/positions` | the book **and** the plan preview, in one call, because they are read together |

`/swing/watchlist` sorts by **distance to trigger**, not by score: a list sorted by how good a
setup looks tells you what to admire; one sorted by how close it is tells you what to watch.
`/swing/positions` leads with any unprotected position, in the same place whether or not there
is one.

### Tests

| Suite | |
|---|---|
| `services/worker/tests/test_swing_eod.py` | **22 passed.** The module plan's own fixture — one position, day 3, green — produces *exactly* a `SELL_AT_OPEN` of 100 of 300 and a `RAISE_GTT_STOP` to the entry, and nothing else; day 2 produces nothing; one full R on day 2 moves the stop and *that* is a different rule; a suspended name is not managed; a naked position is reported every evening; the watchlist fills, prunes and does not double-add; a RED gate skips every name with `GATE_RED`; a sleeve with no capital skips with `SIZE_REFUSED / NO_EQUITY`; the session counter counts sessions, not events; and the email renders with its empty naked section and its counter |
| `services/api/tests/test_api_swing.py` | the three watchlist writes end to end, a level that cannot be edited after the fact, a `PARABOLIC_SHORT` that cannot be watched, and another account refused |
| `services/api/tests/test_swing_readonly.py` | extended: four mutating routes, each whitelisted by path with its reason; `swing_watch` may write and names no `SwPlan`/`SwPosition`/`SwFill`; every handler takes an authenticated principal and calls `scoped_sole_user_id` |

### Two things worth knowing

- **`PlanOut` collided.** OpenAPI names a schema by its Python class name, and a second `PlanOut`
  in this service silently renamed *both* — breaking every existing `PlanOut` reference in the
  TypeScript client. Every response model in `routers/swing.py` is now prefixed `Swing`.
- **The web read-only test's `gtt` ban was too wide.** A position carries a `gtt_id` and whether
  one exists is the single most important safety fact the hub shows. The ban now names the verbs
  (`place_gtt`, `delete_gtt`, `/gtt`) and a second test asserts the id is only ever read.

### What SW5 did NOT do

- **`sw_position` is written by nothing yet.** SW7 owns the fills; until then the book is empty on
  every real database and the position tests build their rows directly.
- The watchlist page is read-only: adding, annotating and dismissing exist as API routes and are
  tested, but there is no form on the page yet.
- `05` §2's "yesterday's `sw_signal` rows shown under the row" needs the monitor (SW6).
- No Mailpit assertion. The email is asserted as a rendered `Message` — subject, both bodies, every
  section — rather than by delivering it to a local inbox, which would test SMTP rather than the
  template.

---

## SW6 — Premarket EP scan and the opening-range monitor ✅

### The morning, in order

| When (IST) | What | Where |
|---|---|---|
| 08:50 | `baskfy.swing.premarket --stage LEVELS`: every `WATCHING` row that a detector wrote has its `trigger`/`stop_ref` re-expressed under the latest bar's `adj_factor` (`03` §9 — a split since detection would otherwise leave the person watching the wrong price by exactly that ratio). `MANUAL` rows keep what was typed. No Kite call | `services/worker/.../tasks/swing_premarket.py` `refresh_levels` |
| 09:09 | `--stage GAPS`: with `BASKFY_SWING_EP_PREMARKET_ENABLED=true`, the liquid universe as of the last close (the detectors' own `liquid_expr`, over the same bars) is quoted through `KiteProvider.quotes` — **≤ 500 symbols a call, one limiter token a call** — and every name `live_gap` accepts becomes an `sw_watch` row: setup `EP`, source `DETECTOR`, catalyst empty, trigger = the indicative price, **no stop yet** (SW6.2), expiring after `ep.valid_bars` sessions. A name already watched is not duplicated. With the flag off, no quote is pulled and the report says `universe: 0` | `liquid_universe`, `evaluate_gaps`, `watch_live_gaps` |
| 09:09 | Either way: the plan is rebuilt from the same watchlist as the evening's preview, with the last close's gate and rung, as `sw_plan.source = MORNING` (`03` §6) | `build_morning_plan` — SW5's `watch_items` / `sleeve_account` / `store_plan`, now public |
| 09:15 → 10:45 | `python -m app.swing_monitor` in the desk, only with `BASKFY_SWING_MONITOR_ENABLED=true`: loads the watchlist (tokens from `instrument.kite_token`, circuit bands from one quote pass), subscribes on the `TickBus`, builds each name's opening range at window close from `historical_data(interval="minute")` (fallback: the ticks' own high/low), runs `evaluate_trigger` on every tick | `kite-momentum-rebalancer/app/swing_monitor.py`, `app/strategies/swing_breakout.py` |
| on a break | `TRIGGERED` → one `sw_signal` row **and** one `sw_plan(source=SIGNAL)` whose single line is sized by the same `build_entries` the evening uses — so a RED gate, a full tier, an already-held name or an empty sleeve produce a plan with a *skip and its reason*, never a line. `LOCKED_UPPER_CIRCUIT` / `BELOW_PIVOT` → a signal row and nothing else, once each. A name that triggered is done for the session. The line is `PROPOSED`; nothing on this path can move it | `PgSignalStore` |
| 10:45 | The monitor stops and marks `sw_session.monitor_ran` for the day (upsert; the evening fills in the rest) | `run_until_close`, `record_monitor_ran` |

### The provider grew one read

`KiteProvider.quotes(symbols)` — `GET /quote` in batches of `QUOTE_BATCH_SIZE = 500`, each batch
one throttled `_call`, rows mapped onto a new `QuoteRecord` (Decimal prices, the exchange's own
previous close and circuit bands). Read-only like everything on that class; the composite router
does not route it (no `Capability` was added — the only thing that pulls quotes is this job, and
it asks the Kite provider by name). `packages/providers/tests/test_kite.py::TestQuotes`, 6 tests:
1,234 symbols → batches of 500/500/234; three batches take three limiter tokens; a symbol Kite
does not answer for is absent, not invented; a row with no price is skipped.

### The replay harness

`tools/swing/replay.py` feeds a minute-candle CSV (or a `bus.last_tick` journal, `--journal`)
through `SwingBreakout` with a list for a store and the CSV for a candle source — no broker, no
bus, no database — and prints what was raised; `--expect` compares against a JSON expectation and
exits 1 on a mismatch. The fixture morning `tools/swing/fixtures/morning-synthetic.*` has four
names and raises exactly four signals:

```
09:20  LOCKED_UPPER_CIRCUIT   GAMMALOCK    entry=       - stop=       -
09:31  TRIGGERED              ALPHAFLAG    entry=  100.80 stop=   97.80
09:35  BELOW_PIVOT            DELTAWAIT    entry=       - stop=       -
09:45  TRIGGERED              BETAEP       entry=  210.50 stop=  204.50
```

Each is a rule of `04` §7.2, not a number typed into the fixture: ALPHAFLAG's entry is the first
tick over the 09:15–09:19 range high × 1.001 that is also above its pivot (09:31's open), and its
stop is the lower of the range low (98.0) and the low of the day (97.8 at 09:22).

### Tests

| Suite | |
|---|---|
| `services/worker/tests/test_swing_premarket.py` | **20 passed.** The gap rule through the scan's plumbing (12 % on 4.17× pace is a candidate; the gap alone is not; the volume alone is not; the exchange's previous close beats the bar table across a bonus; the pace clock starts at 09:00 and is never zero); a 1:2 split rescales a watched level 110 → 220 and a MANUAL row keeps its number; the liquid universe is the detectors' own; **with the flag off no quote is pulled** and the plan is still built; with it on exactly the liquid universe is quoted; a live gap becomes an EP row with no stop and a three-session expiry; not twice; idempotent; the morning plan is `MORNING` with the last close's gate; a live EP without a stop is not a line; no market row → SKIPPED with the reason |
| `packages/providers/tests/test_kite.py::TestQuotes` | 6 passed — the 500 cap and the limiter, as above |
| `kite-momentum-rebalancer/tests/test_swing_monitor.py` | **20 passed.** With the flag off `SwingBreakout.__init__` is never called (a spy on the constructor); `generate_targets` is `[]`; the strategy's *code* (docstrings stripped by `ast`) never names `self.gw`, a placing verb, `kc.` or `kiteconnect`; the runner never names one either and builds the monitor with `gateway=None`; the fixture morning raises exactly the expected signals and the harness exits 0; a name triggers once and is then done; a locked name yields one `LOCKED_UPPER_CIRCUIT` and never triggers; a foreign token is ignored; nothing after 10:45; a failing store does not stop the monitor; a 7-minute window is refused at start-up; a trigger writes a signal row and a one-line SIGNAL plan (qty 1,666 = 0.5 % of ₹10 lakh over a ₹3 stop, `client_id = plan_id:AAA:BUY_ON_TRIGGER`, 30-minute expiry); **a locked name writes a signal row and no plan and no line**; a RED gate writes the trigger, a plan and a `GATE_RED` skip |
| Desk suite | 1,367 passed, 17 skipped (was 1,345) |

`make lint` clean; `test_no_escape_hatches` green over the new worker and provider code.

### Commands

```
make swing-premarket DATE=2026-09-02 STAGE=LEVELS      # 08:50's job, by hand
make swing-premarket DATE=2026-09-02                   # 09:09's; quotes only with the flag on
cd kite-momentum-rebalancer && python -m app.swing_monitor      # exits 0 and builds nothing with the flag off
cd kite-momentum-rebalancer && .venv/bin/python ../tools/swing/replay.py \
    ../tools/swing/fixtures/morning-synthetic.csv \
    --watchlist ../tools/swing/fixtures/morning-synthetic.watchlist.json \
    --expect ../tools/swing/fixtures/morning-synthetic.expected.json
```

### Decisions

SW6.1 (the pace clock starts at 09:00), SW6.2 (a live gap has a trigger and no stop), SW6.3
("desk notification" is the row and a log line — the desk has no channel), SW6.4 (the monitor is
built with no gateway at all).

### What SW6 did NOT do

- **No live morning has run.** Whether Kite's `volume` at 09:09 carries the pre-open matched
  quantity (SW6.1) and whether `historical_data(interval="minute")` returns the forming 09:20
  candle at 09:20:xx or only from 09:21 are both unknown until a flagged morning answers them.
  With either answer the code is correct — the scan reports `candidates: []` and the monitor
  asks for the range again on the next tick — but the *timing* of the first signal may be a
  minute later than the fixture shows.
- **The dev database is still at `0026`**, deliberately: another session is working against it.
  `make migrate` is the first line of the DRY_RUN morning drill (SW10). The premarket CLI was
  exercised against a migrated test database only.
- No push channel (SW6.3 → NEEDS-MAULIK).
- The monitor does not journal ticks. `bus.last_tick` is a dict, not a log; the `--journal`
  input to the replay harness reads a file nothing writes yet. SW11.
- `sw_watch.trigger` for a live EP is not updated to the range high at 09:20 — the signal row
  carries `range_high`, and the watch row keeps the indicative price it was found at.

---

## SW7 — The desk page and `/swing/execute` (DRY_RUN) ✅

**This section covers the execute logic (leaf 1.1.1). The page, the Postgres store and the
routes are leaf 1.1.2's and are appended below it — (page: see 1.1.2).**

### What a click does

`app/swing_execute.py` is the code between "Confirm" and the book. It is handed a `SwingStore`
(one user's `sw_` rows, behind a Protocol so the same code runs over Postgres on a morning and
over a dict in a test) and an `OrderGateway`, and it does exactly one of three things:

| Line | What goes to the gateway | What is written |
|---|---|---|
| `BUY_ON_TRIGGER` | `place(BUY, CNC, LIMIT @ trigger, client_id = plan:symbol:BUY)` and, **in the same call**, `place_gtt_stop(trigger = stop, last_price = entry, client_id = plan:symbol:GTT)` | `sw_position` (entry = trigger, `initial_stop = stop`, the GTT's id/trigger/armed-at), one `sw_fill(BUY)`, the line `FILLED` with the order id, `sw_session` confirms +1 fills +1 |
| `SELL_AT_OPEN` | `place(SELL, CNC, MARKET)` for **at most `quantity_open`** of an `sw_position` the sleeve owns — never a broker holding; then the resting GTT is cancelled and one for the remainder armed | `sw_fill(SELL)`; `quantity_open` down, `PARTIAL` with `partial_done`/`partial_date` and a running share-weighted `exit_avg`; or `CLOSED` with `exit_avg`, `r_multiple` (2 dp, `sizing.r_multiple`), `pnl_inr` (`journal.ClosedTrade`), `close_reason` (the line's note when it is an `ActionReason`, else `MANUAL`), GTT columns cleared |
| `RAISE_GTT_STOP` | `delete_gtt(old)` then `place_gtt_stop(new, quantity_open)` — refused **before** the cancel when the new stop is not above the resting one (`04` §6.5) or not below the last price | `stop`, `gtt_id`, `gtt_trigger`, `gtt_armed_at`; `sw_session` manage_actions +1 |

Plus `rearm_gtt(position_id)` for a NAKED position (`gtt_id` None with shares open) — one
GTT for `quantity_open`, `BLOCKED` when one is already resting.

The refusals are the desk's own, restated for this surface: `confirm != "true"` → 400;
unknown plan, or a line that is not in that plan → 404; `now > expires_at` → 410 (the line is
marked `EXPIRED`); a line not `PROPOSED` → 409. The line is marked `CONFIRMED` **before** the
gateway is called, so a request that dies mid-way leaves a line that answers 409 on re-post,
and an exception from the gateway (an untouchable instrument) marks it `REJECTED` and goes up.

### The flag, and the gateway it gets

`swing_gates()` returns `ProductGates(dry_run = DRY_RUN or not SWING_EXECUTION_ENABLED,
intraday_enabled=False, options_enabled=False)` — the swing book's dry-run is its own flag
(`02` Track B), and the book is CNC-only whatever the weekly desk has been allowed (Track C
§1). `build_swing_gateway(kc, risk)` is the desk shim's `OrderGateway` with those gates, the
swing band `StopBand(0.005, 0.10)` (PACK.3) and its own journal file,
`data/outputs/swing_orders_journal.jsonl`, beside the weekly book's so the test isolation
fixture covers both and the weekly execution report never reads a paper swing fill.

Money stays `Decimal` in the store; it becomes `float` at the gateway boundary and nowhere
earlier. The gateway's statuses map onto `ExecOutcome.status` as C1 says: `DRY_RUN` →
`SIMULATED`, `PLACED` → `SENT`, `BLOCKED`/`RISK_BLOCKED`/`DUPLICATE` → `BLOCKED` with the
gateway's own words, `REJECTED`/`ERROR` → `REJECTED`.

### Tests

`kite-momentum-rebalancer/tests/test_swing_execute.py` — **70 passed**; desk suite **1,439
passed, 17 skipped** with 1.1.2's files in the tree at the time of measurement (SW6 left it at
1,367). Every test drives the real
`baskfy_execution.OrderGateway` built by `build_swing_gateway` over an `ExplodingKC` whose
`place_order` / `place_gtt` / `delete_gtt` / `instruments` raise — so **0 orders reach a broker
in the whole module**, and a test proves the other direction too (under LIVE gates the same
fake surfaces as `REJECTED` and writes nothing). The four HTTP refusals each have a test; a
SELL beyond `quantity_open`, for a name the sleeve does not hold, for a closed position and
for zero shares are each `BLOCKED`; a RAISE below or equal to the resting stop is `BLOCKED`
and the old trigger is untouched; a confirmed BUY carries `gtt` with `DRY_RUN_GTT` and the
row carries `gtt_id`; the partial re-sizes the GTT (`delete` journalled before `place`); the
close-out writes R = 2.25 / ₹2,700 for 300 @ 100→109 with a 96 stop, and 100 @ 110 then 200 @
104 averages to 106.00 / R 1.50; the first-live halving applies only to a real order and
counts the session down once, not per order; the code (docstrings stripped by `ast`) never
names `kc.place_order`, `kc.place_gtt`, `kiteconnect` or `kite_client`.

**A defect the tests caught before it shipped:** SW5's own evening fixture produces a
`SELL_AT_OPEN` of ⅓ **and** a `RAISE_GTT_STOP` to breakeven for the same name in the same
plan. With both minting `plan:symbol:GTT` the second was `DUPLICATE` in the gateway's map —
*after* the old trigger had been cancelled, i.e. a naked position produced by the rules
working as designed. GTT ids are now `plan:symbol:GTT` (buy), `plan:symbol:SELL:GTT` and
`plan:symbol:RAISE:GTT`; the pair is a test.

### Decisions

SW7.1 (a live order is `SENT`, not a fill: no position and no GTT until the fill is known; a
simulated buy fills whole at the trigger and is stopped in the same call; a GTT that cannot be
armed leaves an honest NAKED row), SW7.2 (the first-live halving is per real order, the
countdown per session, in-process).

### What SW7 (1.1.1) did NOT do

- **No live fill reconciliation.** A real order (flag on, `DRY_RUN=false`) ends as `SENT`
  with its order id on the line; nothing here polls the order book, writes the position when
  it fills, or arms the GTT for the filled quantity. That is the gap SW7.1 records and the
  gate `02` §3 keeps the flag false across — the SENT path exists only so that the day it is
  needed it is not invented under pressure. `swing_orders_journal.jsonl` carries the order id.
- **SELL and RAISE need a `last_price` from the page.** A simulated market fill has no price
  of its own and a stop check needs one; without it both are `BLOCKED` with a reason that says
  so — never a fill invented at the entry or the stop. 1.1.2 passes the quote.
- **The first-live countdown is remembered in-process** (`_FIRST_LIVE_COUNTED`, keyed on the
  IST session date). A desk restart mid-session with the flag on would count the same session
  twice — one fewer half-size session. The store could make it idempotent from
  `sw_config_audit` (`changed_by = '/swing/execute'` today); SW7.2 says how.
- The line's `quantity` is not rewritten when a live order is halved; the journal line and,
  later, the fill carry the size actually sent.
- No `manage_actions` from a SELL line: the evening job counts the rule's actions; the desk
  counts confirms and fills, and a re-arm or a RAISE as a manage action.
- `sw_position.user_id` / `broker_account_id` are the store's to stamp (`create_position`
  gets the book's columns only).
- The page, the routes, `PgSwingStore`, the mounts in `main.py` / `base.html` — (page: see 1.1.2).

### The page (1.1.2) — `GET /swing`

`app/swing_desk.py` + `app/templates/swing.html`, mounted by one import and one
`include_router` in `main.py` and one nav entry (`Swing`, after `Reconcile`) in `base.html`.
Per `05` §3, top to bottom:

| Panel | What it shows |
|---|---|
| **Status bar** | `DRY_RUN`, `BASKFY_SWING_EXECUTION_ENABLED`, the mode they add up to (SIMULATED unless both allow), the monitor's state (`idle` / `running since 09:15` / `stopped at 10:45` / `not enabled` / **`did not run`** — derived from the flag, the clock and `sw_session.monitor_ran`, SW7.3), the Kite token's age from the encrypted store (read only when the file exists, so looking at the bar never writes a key file), and today's `sw_session` counters — or "no row yet" |
| **Triggers** | today's `sw_signal` rows newest first: time, symbol, setup, "5-min ORH 100.50 broken at 100.80", entry, stop, the sized line (`SWING BUY ALPHAFLAG x1666 @ 100.80`, ₹ risk, ₹ value, % of allocation, cap), the plan id and its countdown, and **one Confirm form per line**. A `LOCKED_UPPER_CIRCUIT` row shows with no button; a triggered name the SIGNAL plan skipped shows the skip and its reason (found through the plan's skip row — the signal links only to a line) |
| **Plan** | the day's `MORNING` plan (yesterday's is not shown) and the latest `EOD_PREVIEW` (collapsed, and always expired — it is built at 21:05 with a 30-minute TTL): exits first (`SELL_AT_OPEN`, then `RAISE_GTT_STOP`), then the waiting buys, then the skips with reasons. A waiting buy carries a Confirm — the EOD entry mode of PACK.2 — unless the name is already held; an exit carries one unless there is no position, the SELL is for more than is open, or the RAISE is not above the resting stop |
| **Book** | open positions, the unprotected one first, with GTT id and trigger or **naked** in red, **Re-arm GTT** only on a naked row, `Simulated` / `Real` per row; then the last five manage actions (the exit-kind lines, newest first, with the state each reached) |

Every line label starts with **SWING**. An expired plan stays on the page with `0:00` and no
button. Confirm posts through `fetch` and renders the `ExecOutcome` inline (`SIMULATED` /
`SENT` / `FILLED` / `BLOCKED (reason)`); a 4xx renders as "not executed". The page polls
`/swing/data` every 5 s inside 09:15–10:45 and reloads only when the view's fingerprint
changes; outside the window it does not poll. **There is no confirm-all**: the template has one
form shape, one `line_id` per form, and no checkbox.

### The store

`PgSwingStore(conn, user_id, schema="public")` implements every `SwingStore` method of C1 over
the desk's sqlite3-shaped connection with `?` placeholders and `schema.table` names —
`public.sw_plan` on the desk's Postgres (its connection sits on `search_path=desk`), bare names
for sqlite. Decimal in, Decimal out: values are quantised to the schema's scale on the way in
(house rule 8) and re-quantised on the way out, because the Postgres adapter hands NUMERIC back
as `float` and sqlite as whatever it stored. `bump_session` is an `INSERT … ON CONFLICT … DO
UPDATE` that adds to the counters and keeps the monitor's mark; `config()` returns the schema's
server defaults for a user with no row (and says `present: False`); `create_position` drops the
`symbol` `execute_line` passes (the row keys the instrument by id) and stamps `user_id` +
`broker_account_id`; a malformed `plan_id` is `None`, not a driver error on the uuid column.
Beyond the contract the store carries the page's reads (`signals_for`, `latest_plan`,
`lines_for`, `skips_for`, `open_positions`, `recent_manage_actions`, `session`, `fills_for`).

### The routes

`POST /swing/execute` (Form `plan_id`, `line_id`, `confirm`) resolves `app.swing_execute` at
call time — no reference is held, so a monkeypatch on the sibling's attribute is what runs —
builds the swing gateway lazily and once (`build_swing_gateway(kite().kc, main._risk)`: the
weekly book's risk manager, one account, one kill switch), reads the broker's last price
through `Kite.ltp` **for an exit line only**, and returns the outcome as JSON plus `line_id`.
The four refusals come through as 400 / 404 / 410 / 409; an `UntouchableInstrumentError` comes
back as `BLOCKED` with the guard's words, as `/execute` reports one. `POST /swing/rearm`
(Form `position_id`, `confirm`) is the same shape. `GET /swing/data` is the view as JSON
(Decimal → string). A desk on sqlite has no `sw_` tables: the page renders its status bar and a
banner saying so, not a 500. The desk's `websec` middleware covers all four routes (a foreign
Origin is 403).

### Tests (1.1.2)

`kite-momentum-rebalancer/tests/test_swing_desk.py` — **73 passed**; desk suite **1,512
passed, 17 skipped** with both halves in the tree. The `sw_` tables are built in a per-test
sqlite file from a DDL that mirrors `0028_swing.py` column for column (SW7.3); one scenario is
the morning at 09:40 — a trigger with its SIGNAL plan, a locked name, a skipped trigger, a
morning plan with a SELL, a RAISE, two waiting buys (one already held) and a skip, last night's
preview, one armed and one naked position, a session row. Asserted on the rendered page: the
three panels in order and the status bar's five facts; Confirm and a `21:00` countdown on the
trigger, none on the locked row, none on an expired plan (which is still shown, at `0:00`);
every form label starts with `SWING`; exactly one form per confirmable line and one `line_id`
per form, no checkbox, no confirm-all in the source. The route: called through the sibling's
attribute with a `PgSwingStore`, the fake gateway, a tz-aware `now` and no price for a buy /
the broker's price for an exit; 400, 404, 409, 410 and 422 through the route; a guard refusal
as `BLOCKED`. **Through the real `swing_execute`** (imported when present, a stub in
`sys.modules` when not) with a real dry-run gateway over an exploding broker client: a BUY
ends `SIMULATED` with an `sw_position` (`DRY-…` GTT id), an `sw_fill` and the session
counters moved, and the page then shows the position and the line as filled; 400 and 410 (the
line marked `EXPIRED`); a partial SELL to `PARTIAL` 200 open; a re-arm, then `BLOCKED` on the
second. The store: every C1 method round-trips with `Decimal`, the upsert adds and keeps the
monitor's mark, rounding at write time, unknown columns refused, another user sees nothing,
every table name carries the schema prefix. The view: a held name's waiting buy, an exit for
an unheld name, a SELL beyond open and a RAISE below the stop each lose their button with a
reason; yesterday's morning plan is not today's; the fingerprint moves when a line moves. The
module, docstrings stripped, never names a placing verb, `kiteconnect`, or `.place(`.

**Two defects the real module caught in the store before it shipped:** `set_line` built its
SET clause with `insert(1, …)` and appended parameters in the other order, so a call with both
`journal_ref` and `position_id` — exactly `_record_line`'s — wrote each into the other's
column; and `create_position` refused the `symbol` key `execute_line` passes. Both are tests now.

### Decisions (1.1.2)

SW7.3 — nine page-level calls: the sqlite DDL twin in tests, the derived monitor state and its
fifth answer, Confirm on a waiting buy (PACK.2), no button where `execute_line` would refuse,
manage actions read off the plan lines, the shared risk manager, the broker's last price for
exits only, a guard refusal as `BLOCKED`, and reload-on-change polling.

### What SW7 (1.1.2) did NOT do

- **No morning has been rendered against Postgres.** Every statement the store issues was
  checked for both dialects and the schema prefix is asserted on every table name, but the
  page has been run only over the sqlite twin. The DRY_RUN drill (SW10) is the first real
  render; `make migrate` still precedes it (the dev database is at `0026`).
- **The morning plan expires at 09:40.** `03` §6's 30-minute TTL on a plan built at 09:10
  means its exit lines can be confirmed only at the open; after that the page shows them at
  `0:00` with no button and nothing on the desk rebuilds the plan — that is the 09:09 job's
  (`make swing-premarket`). A "rebuild now" control was not added.
- **"Running since 09:15" is an assumption**, not an observation: the flag is on and the
  window is open. Only the 10:45 mark is a fact. A heartbeat is SW11's if wanted (SW7.3 §2).
- **Weekday holidays read as "did not run"** after 10:45 with the flag on; the desk has no
  holiday calendar. Weekends are handled.
- **No R showing, no distance to the trail** on the book rows: the page carries no market
  data. The hub's `/swing/positions` (SW5) has the levels; the desk shows the book as recorded.
- **The `last_price` the route passes is a single LTP at confirm time**, read through the
  same Kite session the desk logs into; with no session it is `None` and the outcome says the
  line was `BLOCKED` for want of a price rather than filled at a guess.
- Postgres `updated_at` on `sw_plan_line` / `sw_position` is set by the store on every write
  (`CURRENT_TIMESTAMP`); the sqlite twin's is UTC-naive and used only to order the manage list.
- `tests/test_pages.py`'s route list was not extended: adding `/swing` to the nav did not break
  its every-page-links test, and the swing suite carries its own (`TestNav`).

---

## SW8 — The journal page and the ladder closing the loop ✅

**The first half of this section covers the ladder write-back and `GET /swing/journal` (leaf
1.2.1); the page (leaf 1.2.2) follows under "The page".**

### The loop, closed

Until tonight the ladder was computed and never remembered: the detection job worked out a
tier from `sw_config.exposure_level`, wrote it to `sw_market_daily`, and `sw_config` stayed at 0
forever — so every evening started from rung 0 and the book could never press. The evening job
now **settles the ladder** as its third step, after `manage` and before `build_entries`:

| | |
|---|---|
| reads | the rung in force (`rung_in_force`: tonight's own settlement record on a re-run, else the previous session's, else `sw_config`), the last `MarketConfig.lookback_trades` [5] closes of the book the ladder reads (PACK.6 — simulated until the flag is on), **closed on or before the session** (house rule 5), and the day's gate as the detectors measured it |
| computes | `baskfy_core.swing.market.exposure_tier` — `04` §8.4, unchanged |
| writes | `sw_config.exposure_level` through `swing_settings.record_system_change` → an `sw_config_audit` row, `changed_by = "swing-eod"`, old → new, with a note naming the gate and the closes; and the day's `sw_market_daily` tier columns + `detail.closed_r_multiples` / `closed_trades_read` / `ladder {from, to, settled_by}` |
| then | the plan is built with **that** tier, so `sw_plan.exposure_level`, `sw_config.exposure_level` and the market row say one number; the morning rebuild reads the row |

An unchanged rung leaves no audit row: the audit is a history of changes, not a log of runs.
A deployment with no `sw_config` row keeps the rung on the market row and logs a warning.

**Why the starting rung is a record, not `sw_config`.** The job writes `sw_config`; a job that
also *read* its starting rung from there would climb twice when the same evening ran twice.
So the rung it started from is recorded on the row (`detail.ladder.from`), the previous
session's settled rung (`detail.ladder.to`) is the base on an ordinary evening, and `sw_config`
is the fallback only. The previous row's `exposure_level` *column* is deliberately not read:
the detection job rewrites it (see "did NOT do"). DECISIONS-SW SW8.1.

### `GET /swing/journal`

`baskfy_api.swing_journal` (read-only, structurally — `test_swing_readonly.py` scans it) and one
route. The shape is contract C2's, exactly, `Decimal` on the wire:

- `real` and `simulated` — two `JournalCard`s, never summed: `stats` (`journal.summarize`: trades,
  win rate, mean win/loss R, expectancy, profit factor **null when there is no loss**, net R,
  largest win/loss, current loss streak), `histogram` (always the six buckets `<-1`, `-1..0`,
  `0..1`, `1..2`, `2..3`, `>3`, in order, zeros included — a `-1.00` sits in `-1..0`, a `3.00`
  in `2..3`), `by_setup` (alphabetical), `by_month` (`YYYY-MM` of the **exit** date, ascending),
  `trades` (newest first, at most 200, the numbers **stored at close**).
- `sessions {logged, required: 20}` — every `sw_session` row, the same count the EOD email shows.
  `PAPER_SESSIONS_REQUIRED` moved from the worker to `baskfy_api.swing_journal` (the worker
  depends on the API, not the reverse); the EOD job imports it from there.
- `ladder {level, gate, max_open_positions, max_exposure_pct, new_entries_allowed, last_r, reads}`
  — `level` is **`sw_config.exposure_level`** (the loop, closed), the gate and tier from the
  latest market row (`gate: "UNKNOWN"`, entries disallowed, tier 0 when there is none), `last_r`
  the last five closes of the book the ladder reads, oldest first, `reads` `SIMULATED` while
  `BASKFY_SWING_EXECUTION_ENABLED` is false and `REAL` after.
- `backtest: null` — SW9 fills `SwingBacktestCardOut` (`run_id, params, started_at, finished_at,
  stats, caveats`), declared now so the TypeScript client already carries the type.

A closed row without an `r_multiple`, an `exit_avg` or a `closed_on` is not a trade (a close-out
that never finished writing), on the page and in the ladder alike. The route is the ninth swing
path, a GET, `AuthenticatedDep` + `scoped_sole_user_id` like the other eight; `openapi.json` and
`schema.ts` are regenerated (additions only).

### Tests

| Suite | |
|---|---|
| `services/worker/tests/test_swing_ladder.py` | **18 passed**, through `run_swing_eod` against the real database. The three acceptance criteria verbatim: five simulated closes net +4.50R in a GREEN tape move 0 → 1 and the row carries the five R values; two wins then three losses move 1 → 0; RED from rung 2 with five good closes behind it lands at 0, `new_entries_allowed = false`, and every watched name is a `GATE_RED` skip. Then: the audit row (`swing-eod`, `0` → `1`, dated with the job's stamp, the closes in the note); the plan is built with the settled rung — three watched names get three lines at rung 1 where the control at rung 0 gives two lines and a `TIER_FULL`; an unchanged rung leaves no audit row; **run twice, climbs once**; the rung carries over from the previous session's settlement even when today's row previews 0; a previous row a re-detect rewrote (column 2, no record) does not fool it; a re-run starts from the rung it recorded; an unseeded sleeve still settles the row; the paper book is read with the flag off and the real one with it on; a close dated after the session is not read; only the last five count, in the order they closed; a `CLOSED` row without an R is not a trade; the step payload explains the move |
| `services/api/tests/test_api_swing_journal.py` | **18 passed** over HTTP. The empty journal is the whole C2 shape with zeros and a 200; real and simulated are separate cards; the six buckets in order with the boundary cases; `by_setup` / `by_month`; profit factor null without a loss; the five-close statistics worked by hand against `04` §10 (60%, 2.00 / -0.75, expectancy 0.90, PF 4.00); the loss streak counts from the latest close backwards; trades newest first with their stored numbers; `2.50` arrives as `2.50`; a row without an R is not a trade; the ladder card with six closes shows the newest five oldest-first, `reads: SIMULATED`; `REAL` with the flag on reads the real book; RED shows entries disallowed; 14 of 20; another account is a 404, anonymous a 401 |
| `test_swing_readonly.py`, `test_api_artifacts.py` | nine paths; `swing_journal` is scanned for writes and for the execution package alongside `swing`; `/swing/journal` is in `EXPECTED_PATHS` |

Existing suites: `test_swing_eod.py` 22, `test_swing_detect.py`, `test_swing_premarket.py`,
`test_api_swing.py` — all green with the settlement in the evening (their `_market` fixtures
say rung 3 and the evening now settles to what the closes justify; no test asserted the fixture's
rung, and none needed to).

### Decisions

SW8.1 (which book the ladder reads, when the write-back happens relative to `manage`, and why
the starting rung is a settlement record rather than `sw_config`).

### What SW8 (1.2.1) did NOT do

- **The detection job still rewrites a settled row.** `tasks/swing.py::write_market_row` (not
  this leaf's file) upserts the tier columns *and* `detail` from `sw_config` + un-date-bounded
  closes. On an ordinary evening (detect 21:00, EOD 21:05) the two agree. But the Saturday
  five-session re-scan runs *after* Friday's settlement: it recomputes Friday's row from the rung
  Friday's evening just wrote — one rung too high in a GREEN tape — and drops the settlement
  record. The evening job is immune (it reads records, then `sw_config`, never the column), but
  **`swing-premarket` reads the column** for Monday's morning plan, and the Market page shows it.
  The fix is three lines in `write_market_row` (keep a row's tier and `ladder` record when
  `detail.ladder.settled_by == "swing-eod"`, and bound the closes by date); it needs an owner —
  SW11 touches that file for telemetry and is the natural place. Recorded in SW8.1.
- **The ladder climbs on every GREEN evening while the last five closes stay net positive**, one
  rung per evening, with no new trade in between — that is `04` §8.4 as written and as the core
  is tested (SW1), so this leaf did not add a "needs a new close since the last move" rule. It is
  the kind of thing the paper period exists to notice; if it needs changing, it is a `04` §8.4
  edit and a `MarketConfig` field, not a worker change.
- `05` §2's "the current loss streak and what it means for the ladder" is two numbers on the
  wire (`stats.current_loss_streak`, `ladder`) and a sentence on the page (1.2.2).
- Sessions are counted as every `sw_session` row, not `mode = 'DRY_RUN'` rows, to match the EOD
  email's number. The two coincide until the flag flips; after it, the gate is moot.
- No 201-trade test for the `MAX_TRADES` cap; the cap is one slice and the statistics are over
  the whole book regardless.
- `03` §3's description of `detail` gains a `ladder` key in practice; the doc line ("the
  closed-trade R list the ladder read") still describes it truthfully and was not edited.
- The page — below.

### The page (1.2.2) — `/swing/journal`

Server-rendered, `force-dynamic`, one bearer read through `lib/swing/fetch.ts`'s `readOrNull`
(`fetchJournal`, the seventh whitelisted path). Top to bottom, in the order `05` §2 lists them:

| | |
|---|---|
| the sentence | the record **the ladder reads** (`ladder.reads`), in one line: "The simulated record stands at +4.50R over 5 trades, +0.90R a trade, with 1 loss in a row behind it." — or, with nothing closed, "No simulated trade has closed yet — 3 of 20 paper sessions logged, and the ladder is reading an empty record." The footnote says the two records are never added and that nothing on the page can place an order |
| the paper record | "N of 20 paper sessions logged" from `sessions`, as a line and as a `progressbar` capped at the gate, with what the gate still needs beyond the count |
| the ladder | rung (`level + 1` of 4, the convention the Market and Setups pages already use), gate (`UNKNOWN` shown as "not measured"), what the tier allows, whether entries are allowed, and **the sentence**: `copy.ts::ladderSentence`, `04` §8.4's four rules in precedence order reduced to the one that applies tonight, said as what the next close does (DECISIONS-SW SW8.2). Under it, which record the ladder reads and the closes it read, oldest first |
| two cards | `Real` and `Simulated`, one component fed two objects; each card is its own `section` with its ten statistics (profit factor `—` with a line saying why when it is null), the six-bucket histogram as count bars (loss buckets in the negative colour, the rest in the positive; at zero trades the bars are empty and a caption says so), by setup, by month (`2026-08` read as "Aug 2026"), and the trade list newest first with the stored numbers — a header note when the list is the latest 200 of more |
| the backtest | under the heading `02` §3.3 names, **"Backtest, EOD approximation"**: with `backtest: null`, "Not run yet…" and that the gate stays shut on this count; with a run, the run id and its IST timestamps, the `caveats` **verbatim** as a list, then `stats` and `params` rendered generically (nested records opened two levels as `group · key`, series shown as a count, `sleeve_inr` read as "allocation inr" — the schema keeps its word, the reader sees the product's, SW4.2) |
| empty state | the API answering `null` (unreachable, or the routes not deployed) renders the header, the tabs and one paragraph — no cards, no backtest section, no crash |

`lib/nav.ts` carries the fifth tab (`Journal`), `lib/vocabulary.ts` the `PAGES["/swing/journal"]`
entry, `lib/swing/__tests__/read-only.test.ts` whitelists the path and scans `journal/copy.ts`
along with the page. Copy says "allocation" and "record", never "sleeve" or "book"
(`no-jargon.test.ts` is in the suite that ran).

### Tests (1.2.2) — `apps/web/src/app/(app)/swing/journal/__tests__/page.test.tsx`, 30 passed

Rendered with `@testing-library/react` over a mocked `fetchJournal`, the fixture being the
five-close record `test_api_swing_journal.py` works by hand. The spec, not the tree: real and
simulated are two separate cards and a `PAPERCO` close never appears in the real card (nor
`REALCO` in the simulated one); each card shows its own count and nothing is summed; the six
buckets render in the API's order with their counts and their `aria-label`s; the six buckets
still render at zero trades with a caption; the groupings, with the month named and R signed;
profit factor `null` explained; an empty `by_month` and a `close_reason: null` render; a
200-row trade list renders 200 rows and says the statistics cover 240; the stored numbers keep
two places and a sign (`+4.50R`, `-0.75R`, `-₹380`); "14 of 20 paper sessions logged" as text
and as the progress bar's value, capped at 20 when 27 are logged; the ladder sentence at rung 1
under RED, at `UNKNOWN`, at two losses (rung 3 → 2, and at the bottom), at three losses (rung 4
→ 3 each evening), at five losses at the bottom, at five net-positive closes in GREEN (rung 2 →
3), at the top rung (never "to 5"), with fewer than five closes, with none, in AMBER, and in
GREEN net negative; the closes the ladder read, listed oldest first; "not run yet" under the
`02` §3.3 heading, and the three caveats **verbatim** with the generic rendering of `stats` and
`params`; the top sentence in both states; the `null` empty state; and that the page says
nothing here can place an order. Whole web suite 2,070 passed; `tsc`, `eslint` (0 errors),
`make lint` clean.

### Decisions (1.2.2)

SW8.2 (what the ladder sentence says at each rung and gate, why the streak is counted from
`last_r`, the three `MarketConfig` numbers mirrored in the page, and how the empty journal
reads).

### What SW8 (1.2.2) did NOT do

- **No Playwright check**, as for every swing page so far (SW4.3): the page is covered by
  rendered-DOM tests over a mocked fetch and by the API contract, not by a browser.
- **The backtest card is generic until SW9 fills it.** `02` §3.3 wants "its R-distribution, win
  rate and expectancy" on the page; the page shows whatever `stats` carries, as label/value
  pairs, and the caveats as sent. A drawn R histogram for the backtest needs SW9 to put a
  `histogram` in `stats` in the journal card's shape (`[{bucket, count}]` in the six-bucket
  order) — then the page can reuse `Histogram`. Leaf 1.3.2 owns the card's content from here.
- **Three `MarketConfig` numbers are mirrored in `journal/copy.ts`** (four rungs, a five-close
  lookback, a three-loss step-down) because the API ships the rung and the closes but not the
  rule. If `GET /swing/journal` grew a `rule {rungs, lookback_trades, step_down_loss_streak}`
  field the mirror could go; not this leaf's file (C2). Same shape of debt as the market page's
  two breadth thresholds.
- The trade list is not paginated or exportable: 200 rows in one table inside an
  `overflow-x-auto`, and the header says when the statistics cover more.
- The `Answer` sentence reads one card — the one the ladder reads. The other card's numbers are
  on the page but not in the sentence; two sentences would be two verdicts.

---

## SW9 — The EOD backtest ✅

**The first half of this section covers the pure engine in `packages/core` (leaf 1.3.1). The
runner task, the `sw_backtest_run` table, the CLI and the journal card (leaf 1.3.2) follow under
"The runner".**

### What `baskfy_core.swing.backtest` does

`run_backtest(bars, params, *, calendar) -> BacktestResult` — `04` §11, session by session,
over the adjusted frame `load_swing_bars` produces and the trading calendar the runner supplies.
Nothing in it is a second opinion about the method: it **calls** the functions the live book
calls, and reports what they did.

| Step of a session | The function that decides it |
|---|---|
| fills at the open — yesterday's `SELL_AT_OPEN` lines, then yesterday's candidates (open ≥ trigger → the open; high ≥ trigger → the trigger) | `stops.apply`; the entry rule is §11's own sentence, in `_fill_at_the_open` |
| sizing and every refusal (tier full, size refused, exposure full, cash not spent twice) | `plan.build_entries`, handed the fill as the trigger and the prior day's low as the stop |
| the close: stop hit / EP failed / close below the trail / partial / breakeven / hold | `stops.manage`, then `stops.apply` |
| detection at the close | `setups.detect_setups` over a contiguous slice of the indicator frame — the last `bars_required` sessions, one `group_by` for the universe |
| breadth → gate → rung for tomorrow | `market.breadth_snapshot`, `market.market_gate` (breadth-only: no index in the frame, SW9.1), `market.exposure_tier` |
| the statistics | `journal.summarize`, overall, per setup and per close-year |

From the screener's engine it reuses `PRICE_EXPONENT` (every price snapped to the four-decimal
grid), `MONEY_EXPONENT` and `MISSING_BAR_TOLERANCE_DAYS` (a held name that stops printing bars
is sold at its last close after five sessions — the same delisting rule). Costs are
`cost_pct_per_side` [0.13] on the price paid and on every price received; the journal's
`entry` and `exit_avg` carry them, so §10's `r_multiple` and `pnl_inr` are unchanged formulas
on honest prices (SW9.4).

The result: `trades` (close order), `stats`, `by_setup` (FLAG and EP, zeros when empty),
`by_year` (every year of the run, keyed by the close), `equity_curve` (one point per session:
sleeve + realised + open positions at the close), `funnel` (with
`entered + Σ skipped_* == candidates` always true — `skipped_locked`, `_gate`, `_held`,
`_no_next_session`, `_no_bar`, `_no_trigger`, `_tier`, `_size`, `_exposure`), and a `ladder`
trace `(session, gate, rung)` that C3 did not name and the runner stores as-is (SW9.1).
`to_json()` is plain JSON with a fixed key order: `params, trades, stats, by_setup, by_year,
equity_curve, funnel, ladder, caveats`. `CAVEATS` is §11's three sentences.

### Tests — `packages/core/tests/test_swing_backtest.py`, 44 passed

`swing_backtest_fixtures.py` plants **one flag** — `swing_fixtures.flag_series`, the base the
detector suite already proves is `SETTING_UP` on its last bar, re-dated onto weekdays and
followed by a scripted tail whose every price is a constant. The trade is worked by hand in
the fixture's docstring from §5, §6, §10 and §11: 492 shares at 152.00 (the open, above the
149.6025 pivot), stop 141.855 (the detection day's low), a third sold at the day-4 open of
162.00, the rest at the breakeven stop on day 5, 0.13% a side → **R = 0.28**, and the engine
reproduces it to the paisa. Beside it: the entry rule at the open, at the trigger, at an open
*exactly* on the trigger, and with a high that never reaches it; the prior day's low as the
stop; costs on both sides and a stop-out that reads worse than −1R by exactly the costs;
sizing from `params.config` and from the sleeve; the ladder climbing a rung per GREEN session
after a win and stepping down one per session on a losing run (a second planted flag, a
one-trade lookback); a RED tape refusing every candidate; `by_setup` and `by_year` equal to
`summarize` over the right subsets; the curve marking open shares at the close and ending at
the realised rupees; the funnel identity on five frames; a locked EP counted, never entered;
a GAP_DAY EP entered the next session and *not* sold for a red close that day (§6.4.2 is the
gap day's rule); a gap through the stop filling at the open (on day eight, and on day one); an
entry-day stop-out filling at the stop; a close below the trail MA selling everything at the
next open; `END_OF_RUN` and `NO_BAR` closes with their counts and the curve's last point at
the price received; a pending partial waiting for a bar that never comes; a same-symbol
duplicate entering once; a second position the day after the first refused `EXPOSURE_FULL`
against the book at cost; the ladder starting at rung 0; the gate reading only the session's
own close (a tape that turns later is RED until it does — house rule 5); a candidate on the
last session counted, not entered; a Saturday bar never traded; a one-session run; a repeated
calendar day refused; the empty typed frame running flat; byte-identical JSON across two runs;
the frozen contract objects.

### Speed, measured

`test_speed_300_instruments_over_8_years_runs_under_60s`: 300 instruments × 2,000 sessions
(600,000 bars) in **24.1 s** inside the suite (27.7 s standalone, first run) on the dev
laptop. Profiled, 97% of the time is inside `detect_setups` itself (its `_window` and three
`group_by.agg` calls); the trade loop is noise. The detectors are linear in the universe, so
2,500 instruments × 2,300 sessions (2017 →) extrapolates to **about four to five minutes** —
inside `06` SW9's thirty with room. The speed test skips itself inside the mutation harness's
workspace (a mutant's speed proves nothing).

### Mutation score

`tools/mutation.py` knows `swing/backtest.py` (100 mutants; primary test file
`test_swing_backtest.py`, which is also appended, last, to the selection every other swing
module is scored against). Two runs, same 100 mutants: **62.0% → 76.0%** (76 killed, 24
survived), above the 70% this leaf's gate asks for and just under `factors`' 77.5%. The first
run's fourteen extra survivors were real gaps — no trail-MA exit, no gap through the stop on
day one, no high *exactly* at the trigger, no exact funnel counts, no pending sell across
missing bars, no one-session run, no repeated calendar day, and a breadth slice that could
have read tomorrow's rows — and each became a spec test. The 24 that remain, by family: eleven
`slots=True` / `frozen=True` flips on the private dataclasses (`_Panel`, `_Position`,
`_Candidate`) and `slots` on the public three (the same equivalent mutant
`reconciliation/MUTANTS.md` already justifies for `factors`); the `row_of` guard for an
instrument absent from the panel (unreachable: a candidate detected today has a bar today);
`open >= trigger` read as `>` (an open exactly on the trigger falls through to "high ≥ trigger"
and fills at the same price — equivalent by construction, and the test for it passes both
ways); `RED or not new_entries_allowed` read as `and` (RED implies the second); the empty-day
`BreadthSnapshot(0, 0.0, ...)` constants (a one-name universe with nobody up 25% is RED just
the same); `_MIN_WINDOW_BARS` 2 → 3 and `>=` → `>` (a two-bar name cannot set up either); the
detection window one session longer or shorter (`first ± 1` — the planted base is 35 bars
inside a 126-session window); the window slice extended past today (the detector's own
`date <= as_of` filter is the look-ahead guard, so the mutant changes nothing — and that is the
right place for the guard to live); `cash_available = sleeve + open_cost` (the tier's exposure
ceiling refuses a position before §5's cash cap can bind, at every rung); and `missing = 1` as
a default that the entry day's bar resets to 0 before it is ever read. Report and JSON are in
the leaf's scratchpad; `reconciliation/MUTANTS.md` is `make mutants`' and was not regenerated
here (SW12).

### What SW9 (core half) did NOT do

- **Nothing has run over real NSE bars.** The number `02` §3.3 wants on the page does not exist
  yet; the runner (1.3.2) produces it, and the dev database still holds ten sessions (SW3.3).
- **The gate has no index inside the backtest** — breadth only (`market_gate(breadth, None)`),
  because the bars frame carries no index and C3 fixes the signature. Days the NIFTY 500 sat
  below both MAs while breadth held are AMBER/GREEN here and RED on the desk. SW9.1 names the
  one-keyword reversal.
- **An EP gets one entry session, not the watchlist's three** (§11 verbatim; SW9.2). The
  backtest under-counts the EPs a desk would have taken on day two or three.
- **The ladder is re-settled every session** and climbs a rung per GREEN session after five
  good closes — the same behaviour SW8 recorded for the evening job. The `ladder` trace on the
  result is there so the paper period can judge it.
- `detect_flags` **raises** on an instrument with a single bar in its window (its listing day);
  the backtest leaves single-bar names out (`_MIN_WINDOW_BARS`) rather than editing `setups.py`,
  which is SW1's. The worker will meet the same edge on the first session of a new listing —
  for `setups.py`'s owner (SW12's survivor pass is the natural place).
- **No `+5/+5` score adjustments** (`04` §2.6: young listing, hot sector) — they need
  `instrument.listed_on` and `index_member_daily`, which core may not read. Scores in the
  backtest are the detector's; they decide only the order candidates are considered in.
- No circuit modelling before 2020 and no intraday data — the caveats, verbatim, on the result.
- `reconciliation/MUTANTS.md` is not regenerated by this leaf (it is `make mutants`' output
  over every target and is shared); the backtest's own survivors are listed above and left for
  SW12's justification pass.

### The runner (1.3.2) — `sw_backtest_run`, `baskfy.swing.backtest`, the CLI, the card

**What runs.** `baskfy_worker.tasks.swing_backtest` does the three things the pure engine may
not: it loads the bars, it supplies the calendar, and it stores the run.

| | |
|---|---|
| the bars | the frame `load_swing_bars` gives the nightly job — adjusted, the cash series, `upper_circuit` multiplied into the adjusted space, the user's own liquidity floors from `sw_config` over the pack's defaults — over `lookback_start(start, 200)..end`, so the run's first session is detected on the same 200 sessions the live job would have had that night. One clause differs, and it is the one `04` §11 names: names **delisted on or after `start`** are kept (the nightly query keeps none), and the engine's `NO_BAR` rule sells them when they stop printing. Survivorship handled by `instrument.delisted_on`, as the caveat says (SW9.6) |
| the calendar | `trading_day` for the NSE over the same window; a bar on a holiday is read by the detectors and never traded on |
| the sleeve | `params.sleeve_inr`, `04` §11's constant ₹10 lakh — never `sw_config.sleeve_capital_inr`, which is ₹0 until Maulik sets it; risk per trade is the pack's 0.5% |
| the row | `sw_backtest_run` (migration `0029_swing_backtest`, model `SwBacktestRun`, `03` §10): `params` written on the way in, `stats = BacktestResult.to_json()` on the way out — the trade list, the equity curve and the ladder trace included, every price a string of its exact decimal — and **never edited**: a re-run is a second row. A run that raises records `"{Type}: {message}"` and the traceback in `error`, sets `finished_at`, and **re-raises** so Celery sees it |
| the task | `baskfy.swing.backtest(start, end, sleeve_inr, cost_pct_per_side)` on the **compute** queue (set on the task: no `baskfy.swing.*` route exists and there is no Beat entry to carry the option); the body commits the started row first and the result or the error after, so a failed nine-year run is a durable row, not a rollback. `start` defaults to 2017-01-01, `end` to today in IST |
| the CLI | `tools/swing/backtest.py` at the repo root. `--fixture` runs the planted year with no database, prints the table and the planted trade beside the number the fixture expects (`planted R=0.28 expected R=0.28: reproduced`), exit 1 otherwise. The default mode runs the task body against `BASKFY_DATABASE_URL` for `--start/--end` (`--sleeve-inr`, `--cost-pct`, `--user-id`, `--json` for the stored row), stores the run and prints the statistics **and the elapsed time** — the `< 30 min` measurement, when it is taken |
| the card | `GET /swing/journal.backtest` is the latest **finished** run — `finished_at` set and `error` null, ordered by finish, so a run in flight or a failed re-run never displaces the last good number — as contract C2's `{run_id, params, started_at, finished_at, stats, caveats}`. `caveats` is `baskfy_core.swing.CAVEATS` verbatim, from the constant, not the row. `stats` is a card, not the row (SW9.7): `04` §10's ten numbers at the top level, the journal's own six-bucket `histogram` (`bucket_of` over the stored trades — one bucketing for the paper book and the backtest), `by_setup`, `by_year`, `funnel`, and `equity {sessions, start, end, low, high}`; every stored decimal a `Decimal`, so `0.28` reaches the page as `0.28` |
| the page | under `02` §3.3's heading, "Backtest, EOD approximation": the run line, the three caveats verbatim, a note that entry and exit prices carry the cost per side and nothing compounds, then **one sentence** ("412 trades over 2300 sessions: +0.31R a trade, 42% winners, +127.75R in all"), the same ten tiles and six bars the two journal cards draw (`StatTiles` and `Histogram` are now shared), by setup and by year closed as tables, the allocation's start and end, the funnel, and any key the page does not know under "Other results". Parameters: first and last session, the constant allocation, the cost per side as tiles, and the whole method configuration behind a disclosure. A `stats` without the ten headline numbers falls back to 1.2.2's generic listing, kept by a test |

**Not measured: the 2017→ run.** `06` SW9's "the run over the full history completes on the dev
box in < 30 minutes" cannot be measured on this machine: the dev database holds **ten sessions**
of bars for 180 instruments and is deliberately at migration `0026` (SW0.2, SW3.3); there is
nothing for a 2017→ run to read, and no run was faked against it. The honest number is the
extrapolation from 1.3.1's speed test — 300 instruments × 2,000 sessions in **24.1 s**, the
detectors linear in the universe — which puts 2,500 instruments × 2,300 sessions at **about four
to five minutes**, six times inside the 30 min budget, plus one query over ~5.7 million
`ohlcv_daily` rows and one JSONB write. The CLI prints the elapsed time; the first
`uv run python ../tools/swing/backtest.py --start 2017-01-01` on a backfilled database is where
the measured number goes (SW-FINAL-REPORT's first-morning steps). The database mode *was*
exercised end to end here, against the leaf's private test database, over a five-session range
with no bars: an honest empty run stored and printed, and a reversed range stored as a failed
row and reported — the CLI and the task body are proven, the history is not (SW9.8).

### Tests (1.3.2)

| Suite | |
|---|---|
| `services/worker/tests/test_swing_backtest_task.py` | **18 passed**, against the real database. The planted year written into `ohlcv_daily` on the NSE calendar, read back and run through `run_swing_backtest`: the one trade is `FLAGWIN`, 492 shares at 152.1976, stop 141.855, exit 155.13, **R = 0.28** — `06` SW9's acceptance through the whole runner, not the engine alone; the stored `stats` equals `result.to_json()` and `params` equals `to_json()["params"]`, with the caveats and the funnel's session count; the run trades at 492 shares with `sw_config.sleeve_capital_inr = 0`; the user's `adr_min_pct` is the run's config and everything else is the pack's; a given sleeve and cost override the defaults. The bars: the frame **equals `load_swing_bars`** over the lookback window, column for column; a run starting on the detection day still finds the flag (the 140 bars before it are in the frame); the calendar is `trading_day`'s over the same window with no weekday holiday in it; a name delisted inside the run is in the frame, one delisted before it is not, and the nightly loader has neither; a name delisted on the first session is kept. A run is a fact: two runs are two rows and the first is untouched (and the second is byte-identical — determinism); a range with no bars stores an honest empty result; `end < start` is recorded as `ValueError: end … is before start …` with the traceback, `finished_at` set, `stats` null, and re-raised. The binding: named `baskfy.swing.backtest`, `queue == "compute"`, `acks_late`, no Beat entry; 2017 is the default start; no `BASKFY_SOLE_USER_ID` skips rather than inventing a tenant; through `run_checkpointed`, a failed body leaves a durable error row and a good one returns the summary of the row it stored |
| `services/api/tests/test_api_swing_journal.py` | **24 passed** (18 + 6): null with a run in flight and a failed run present; C2's card with the three caveats verbatim, the run's parameters (money as numbers with their precision, the config's thresholds) and IST-formattable timestamps; the headline numbers, the six buckets in order with the planted trade in `0..1`, `by_setup` (FLAG 1, EP zeros, profit factor null), `by_year` sorted, the funnel and the equity ends — and no trade list, curve or ladder on the card; the latest *finished* run wins over a later-started unfinished one and a later failed one, and the later finish wins between two finished; an empty run is a card of zeros, not null; another account's run is not this account's card |
| `apps/web/.../journal/__tests__/page.test.tsx` | **36 passed** (30 + 6): the caveats verbatim (unchanged); a `stats` without the ten headline numbers falls back to the generic listing; the sentence, the tiles, the six bars with their `aria-label`s and the `>3` count; the by-setup and by-year rows, the funnel, the allocation's start and end in Indian grouping; the four parameter tiles and the configuration disclosure, and no "sleeve" anywhere; a run with no trade says so and draws six empty bars; an unknown result or parameter is listed, never dropped |
| `packages/core/tests/test_schema_matches_docs.py` | 199 passed — `sw_backtest_run` in the table list and in `03`; `test_migrations.py`, `test_celery_config.py`, `test_swing_eod/ladder/detect/premarket.py` re-run green (113); `test_swing_readonly.py` and `test_api_artifacts.py` green; alembic's `compare_metadata` reports no diff for the table |

`make lint` clean; `test_no_escape_hatches` green; `openapi.json` and `schema.ts` regenerated with
**no change** — 1.2.1 had already declared `SwingBacktestCardOut`, and the card fills it.

### Decisions (1.3.2)

SW9.6 (the append-only row, the survivorship clause, the config and the sleeve, the queue),
SW9.7 (the card's `stats` shape and the page), SW9.8 (the CLI's two modes and why the 2017→ run
is an extrapolation here).

### What SW9 (1.3.2) did NOT do

- **The 2017→ run has not been run**, and its `< 30 min` is an extrapolation, above. The number
  `02` §3.3 wants on the page does not exist until a backfilled database exists; the card says
  "not run yet" until then, truthfully.
- **The bars reader is mirrored, not shared.** `bars_frame` in `tasks/swing_backtest.py` is
  `load_swing_bars`'s row-to-frame reading again, because the delisting clause needed a
  different query and `tasks/swing.py` is SW3's. A `delisted_since` keyword on `load_swing_bars`
  is the one-line merge, for SW12's pass; a test asserts the two frames are equal meanwhile.
- **No `+5/+5` score adjustments** in the backtest (the core half's note stands): the runner
  hands the engine bars, not `instrument.listed_on` or `index_member_daily`. The scores decide
  only the order candidates are considered in.
- **The card is the latest finished run; there is no run picker** and no way to compare two
  runs on the page. `tools/swing/backtest.py --json` prints any run's row for a diff.
- **No telemetry** on the task (SW11 owns spans and metrics for `swing*.py`).
- **The Celery binding itself is tested for its name, queue and skip**, and its body through
  `run_checkpointed`; the `build_pipeline_dependencies()` call in front of it is not driven in a
  test (it builds the provider stack from settings, which the other swing bindings also leave to
  the drill).
- The planted year is reached from the worker and API suites by putting
  `packages/core/tests` on `sys.path` (SW9.8) — a departure from `test_swing_detect.py`'s
  retyping, taken so the runner's acceptance is the fixture's own trade, not a copy of it.

---

## SW9.5 — Reconcile with the primary sources ✅

**Goal, from `06`:** "the rules are his, quoted, not a summary's — see `07`." `07`'s "What
changes" table is the spec; the patch it names was applied at step 1 (`git apply`, clean), the
35 contract/backtest tests it turned red plus the six docs-parity ones were re-pinned by
re-deriving every number from the amended `04`, and the rest of this section is what the change
rippled into. Everything below was **measured on this machine on 2 Sep 2026**.

### What changed, rule by rule

| Rule (`07`) | Where it now lives | Proven by |
|---|---|---|
| The stop is one ADR or tighter; a wider stop is **skipped**, never sized down | `StopConfig.max_stop_adr_multiple` [1.0], `stops.widest_stop_pct = min(adr × 1.0, 10%)`, `plan.build_entries` hands it to `size_position` | `test_swing_primary_sources` (hypothesis, 500 random watchlists: no `BUY_ON_TRIGGER` line's stop is wider than its ADR; 6 % under on a 5 % name is `STOP_TOO_WIDE`, 5 % is lined), `test_swing_stops`, `test_swing_plan_and_journal` |
| The index rule is the 10-day MA above the 20-day; the close is not consulted | `IndexReading.long_bias` / `bearish` (`above_both` / `below_both` gone); `market_gate` RED on `bearish`, GREEN needs `long_bias` | `test_swing_market`, `test_swing_contract_book::TestTheGate` (a close under both averages on a rising 10-day is long; over both under a falling one is bearish; equal averages are neither), `test_swing_contract_edges` |
| At most three new entries a session | `SizingConfig.max_new_entries_per_session` [3]; skip `SESSION_CAP`, counted on lines in **this** plan | `test_swing_primary_sources` (ten qualifying flags → three lines, seven `SESSION_CAP`; three names held still get three new lines), `test_swing_backtest` (five flags on one day at a five-position rung: three entered, `skipped_session_cap` 2) |
| The plan takes `min(rung, sizing.max_open_positions)`; top rung 10 | `MarketConfig.tiers` `((2, 25), (4, 50), (6, 75), (10, 100))`; `build_entries`; the worker's `load_swing_config` now hands the plan the trader's three sizing knobs (SW9.5.3) | `test_swing_primary_sources` (rung 1 vs cap 10 → 4; rung 3 vs cap 2 → 2; the `TIER_FULL` detail names the number), `test_swing_contract_book::TestTheLadder` |
| The sleeve locks out new entries 15 % below its peak until back within 10 % | `MarketConfig.max_drawdown_pct` [15] / `resume_drawdown_pct` [10]; `market.drawdown_pct`, `market.drawdown_locked` (hysteresis); `exposure_tier(..., drawdown_pct, was_drawdown_locked)` → rung 0, no entries, `ExposureTier.drawdown_locked`; skip `DRAWDOWN_LOCKOUT` | core: `test_swing_market`, `test_swing_primary_sources` (15 % → zero lines, every skip `DRAWDOWN_LOCKOUT`; 9.9 % after a lock-out → entries at rung 0; 12 % locked stays locked, 12 % unlocked never was); backtest: `test_a_sleeve_in_drawdown_is_locked_out_of_new_entries_until_it_recovers` (two positions, a 0.61 % loss, a lock at 0.56 %, a recovery through the open position's mark, release at 0.23 % under a 0.25 % line and not under a 0.10 % one); evening: `test_swing_ladder::TestTheDrawdownContainment`, 9 cases |
| The swing GTT rests 3 % under its trigger | `place_gtt_stop(limit_fraction: float \| None = None)` — additive, `None` is `GTT_LIMIT_FRACTION`; the desk's `_arm` passes `C.SWING_GTT_LIMIT_FRACTION` (env `BASKFY_SWING_GTT_LIMIT_FRACTION`, 0.97) on every GTT | `packages/execution/tests/test_gtt_limit_fraction.py` (9: 0.97 lands as 86.35 on an 89 trigger, 2440 on a whole-rupee tick; omitted → the weekly book's 88.55, byte for byte; the dry-run journal records the fraction; a fraction outside (0, 1] is refused before any layer runs), `tests/test_swing_execute.py` (+4: the constant, every live GTT, every dry-run GTT, the weekly book's files never name it); `git diff --stat` on `packages/execution/tests`, `test_seven_non_negotiables.py` and `test_execute_gateway.py` is empty |
| Ceilings 30 % / 20; ADR floor 4.0 | `Settings` / `WorkerSettings` defaults; the root, decile and desk `.env.example`; `LiquidityConfig.adr_min_pct` 4.0; `sw_config` server defaults 10 / 4.00 (`0030`) | `test_swing_schema_and_settings::test_the_ceilings_are_his_own_numbers`, `test_schema_matches_docs` (the columns are in `03`, the defaults are 10 / 4.00), `test_api_swing_journal` (the card says 4.0) |

### The evening, and the schema it needed

`settle_ladder` now settles the drawdown beside the rung. `tasks/swing.py::sleeve_nav` computes
the sleeve's EOD NAV from the book the ladder reads (PACK.6) — capital + closed `pnl_inr` +
open positions marked at the latest close + the SELL fills of still-open positions (SW9.5.1;
`03` §1 has the formula) — and `sleeve_drawdown` reads it against `sw_config.sleeve_peak_inr`
(null until the first evening: the first session is never locked; a ₹0 peak divides nothing).
The peak and the drawdown go back to `sw_config` unaudited (the market row is their history);
`drawdown_locked` is audited with the NAV and the peak in the note. The night the ladder
switches books (the execution flag flipped since the previous settlement) the peak starts over
at that night's NAV, so a paper peak is never held against the real book. The detection job's
`write_market_row` computes the same measurement as a preview and passes it to
`exposure_tier` the same way; the evening's settlement is authoritative. Re-running the
evening moves neither the peak (a maximum) nor the lock (the re-run reads the lock it wrote as
"was locked" and answers the same). Migration **`0030_swing_primary_sources`**: `sw_config.
sleeve_peak_inr` / `drawdown_pct` / `drawdown_locked`, `sw_market_daily.drawdown_pct` /
`drawdown_locked`, the `sw_plan_skip.reason` constraint rebuilt with `SESSION_CAP` and
`DRAWDOWN_LOCKOUT` (the first evening test hit the old constraint — a check the model imports
from the engine and the database had frozen at SW2), `max_open_positions` default 8 → 10 and
`adr_min_pct` 3.50 → 4.00 with rows at exactly the old default moved (SW9.5.4). Round-tripped
(`upgrade` → `downgrade 0029` → `upgrade`) on `baskfy_sw_t2`. `SwingConfigPatch` forbids the
three new fields (`SYSTEM_OWNED_FIELDS`, owner `swing-eod`; the parametrised refusal test covers
each).

The evening's `watch_items` now reads a name's **latest** detection row on or before the
session rather than today's only (SW9.5.2): the ADR sizes the stop now, and a flag watched on
Monday and not re-detected on Wednesday would otherwise have carried an ADR of 0 into the plan
and been refused. A `MANUAL` row with no detection behind it still is — a stop nobody can measure
against the range is not shown to be inside it.

### The re-plant

`swing_backtest_fixtures` draws every bar ±2 % around its close, an ADR of 4.08 %, and the
planted stop (the detection day's low, 6.67 % under the entry) would be `STOP_TOO_WIDE` on such
a name. The fixture is now a leader with a leader's range: every low **before** the detection
bar sits 7 % under its close (`LOW_FACTOR`), the detection bar keeps its tight ±2 % — the
contraction the method wants — so the 20-bar ADR is **9.40 %** (worked by hand:
`(19 × 9.68 + 4.08) / 20`), the pivot (a high) and the stop (the detection day's low) are exactly
where they were, and the hand-worked trade (492 shares, R = 0.28) is unchanged; the trail is now
the 10-day (ADR ≥ 6). `test_planted_stop_sits_inside_one_adr_of_a_leaders_range` re-derives
all of it and asserts the ±2 % fixture would have refused the stop. One test ends its run on the
entry day because the stopped-out bar reads as a base again on a leader's-range name. The
worker fixtures (`test_swing_ladder/eod/premarket`: trigger 110, stop 104, 5.45 %) carry an ADR
of 5.60 instead of 5.00; the EP doji test widens its prior bars so nineteen bars and a flat one
average above the 4.0 floor. A `HOLD_TAIL` was added for the drawdown case: a position that
neither partials nor stops for nine sessions and then rallies — the only way a locked sleeve
recovers.

### Numbers

| Suite | Result |
|---|---|
| G1 — docs parity, market, plan/journal, stops | **170 passed** |
| G2 — contract book / edges / detectors (274) + backtest (48 + speed) | **323 passed**, 71 s |
| G3 — `test_swing_primary_sources.py` | **7 passed** (1 hypothesis × 500 + 6 exact) |
| G4 — `packages/core/tests` less SW10's uncommitted file | **2665 passed, 2 skipped**, 147 s |
| G5 — ladder, eod, detect, premarket, schema docs, backtest task, journal API, swing API, schema+settings on `baskfy_sw_t2` | **445 passed**, 105 s |
| G6 — the desk | **1553 passed, 17 skipped**, 49 s; the three named files unchanged |
| G7 — `packages/execution/tests` | **170 passed** (161 + 9) |
| G9 — `make lint` | clean |
| SW10's uncommitted files under the new rules | `test_swing_safety_properties.py` **8 passed**; `services/api/tests/test_swing_track_c.py` **19 passed**; `kite-momentum-rebalancer/tests/test_swing_track_c.py` green inside the desk suite. **None went red.** |

The pre-existing red that G5 surfaced and this module fixed:
`test_swing_schema_and_settings::test_the_migration_drops_what_it_creates[sw_backtest_run]` had
been failing since SW9 (it read `0028_swing.py` alone for the drop list; `sw_backtest_run` is
`0029`'s). It now scans every `00NN_swing*.py`.

### Decisions

PACK.7 (the drawdown breaker: 15 % lock, 10 % release, why hysteresis and why the bottom of his
range), PACK.8 (the GTT cushion: 0.97, additive, refused outside (0, 1]), PACK.9 (the 1.0 %
ceiling kept against his small-account 1.5 %) — all ⚠ UNREVIEWED; SW9.5.1 (how the sleeve's NAV
is computed, what is audited, the peak reset on a book switch, the capital-lowering edge),
SW9.5.2 (an unmeasured ADR is a refused stop; the latest detection row), SW9.5.3 (the worker
hands the plan the trader's sizing knobs — a pre-existing gap: `sw_config.risk_per_trade_pct` /
`max_position_pct` / `max_open_positions` never reached `SizingConfig`), SW9.5.4 (two defaults
moved in the migration for rows nobody set).

### What SW9.5 did NOT do

- **The morning rebuild and the monitor do not carry `drawdown_locked`.** `swing_premarket.py`
  (SW6's) and the desk's `swing_monitor.py` (SW6's) rebuild `ExposureTier` from the market row
  without the new column. A locked sleeve still plans **no** entries in the morning
  (`new_entries_allowed` is false on the row), but the skip reads `GATE_RED` instead of
  `DRAWDOWN_LOCKOUT`; one keyword each (`drawdown_locked=market.drawdown_locked`) and one more
  column in the monitor's `SELECT` for their owners. Safe, mislabelled.
- **The monitor still sizes with the pack's risk knobs.** `swing_monitor.load_config` applies
  the liquidity floors only (SW9.5.3 fixed the worker; the desk copy is SW6's file, and MD6 has
  SW10 re-deriving a SIGNAL line's size at confirm from `sw_config`).
- **The ladder card and the EOD email do not show the lock-out.** `LadderCard` / `SwingLadderOut`
  (`routers/swing.py`, not this module's) and `email/templates.py` are unchanged; the state is on
  `sw_market_daily` and in `sw_config_audit`, and `05` now says where the pages should show it.
- **A MANUAL watch row with no detection behind it is refused `STOP_TOO_WIDE`** (SW9.5.2). The
  right answer — the ADR from the bars — is one query the evening does not run yet.
- **Lowering `sleeve_capital_inr` reads as a drawdown** of that size (SW9.5.1). Conservative;
  the reset is manual.
- **The top-20 / top-5 funnel numbers are `05`'s spec, not code.** The evening still auto-watches
  on `auto_watch_min_score` [60]; the monitor watches every `WATCHING` row. SW11's.
- **The drill (`tools/swing/drill.py`, SW10's) was not run.** Its `baskfy_sw_t3` is not this
  leaf's database. Read against the new rules: its stops sit 3 % under 5 %-ADR names, its rung-0
  `TIER_FULL` still holds, its market row's new columns take their defaults, and the evening's
  NAV on its two simulated positions starts at its peak — it should pass; that is a reading, not
  a measurement.
- **The 2017→ backtest has still not run over real bars** (SW9); the re-planted fixture is the
  only book that has been through the new rules end to end.
- `services/worker/tests/test_swing_eod.py`, `test_swing_premarket.py` and
  `services/api/tests/test_api_swing_journal.py` are SW5/SW6/SW9's files; each took a one-line
  re-pin (the fixture ADR, the card's 4.0) so that G5 could be green, and nothing else in them
  moved.

## SW9.6 — The backtest carries the index rule, the drawdown on its constant-sleeve curve, and gate-on against gate-off ✅

**Goal, from STANDING-ANSWERS A12 (MD14) and B1–B5:** "Constant ₹10 lakh sleeve + the index
rule. NIFTY 500 from `index_snapshot_daily` (NIFTY 50 fallback), 10/20 SMAs in-frame, no
look-ahead. Drawdown lock-out on the constant-sleeve equity curve (realised + open marked at
close). Report gate-on vs gate-off per year and per setup, with breadth's and the index rule's
contributions labelled separately." SW9.1's third choice (breadth-only, "an `index` keyword is
the reversal") is reversed here, and the four SW9.6.x decisions are the judgement calls.
Everything below was **measured on this machine on 2 Sep 2026**. (Numbering note: the
DECISIONS-SW entries `SW9.6`–`SW9.8` are leaf 1.3.2's, from SW9; this module's are
`SW9.6.1`–`SW9.6.4`, the way SW9.5's are `SW9.5.x`.)

### What changed, rule by rule

| Rule | Where it now lives | Proven by |
|---|---|---|
| The index rule in the backtest (A12): NIFTY 500's closes, the 10/20 averages from the closes **on or before** the session, `market_gate` as the desk reads it | `run_backtest(..., index=(date, close))`; `_IndexSeries.reading_on` mirrors `load_index_reading` (the last twenty rows on or before the date; fewer → ignored); `BacktestParams.index_slug` labels the series | `test_swing_backtest`: a crash in the detection day's close turns the gate RED that evening and the flag is refused; **the look-ahead test** shifts the same series one session later and asserts the gate changes on the boundary session only, never earlier; nineteen closes are ignored and twenty are read; a bad frame or a slug with no series is refused |
| The runner's series: NIFTY 500 with the NIFTY 50 fallback, once per run, at `params_for` time | `swing_backtest.resolve_index_slug` (the first of the two with ≥ `index_ma_slow` levels in the window; neither → `None`), `load_backtest_index`, `params.index_slug` on the row from the start | `test_swing_backtest_task::TestTheIndexRule` (4): NIFTY 500 read in-frame through the database and the stored ladder RED on the crash day; nineteen NIFTY 500 rows fall back to NIFTY 50; no index at all is breadth-only with the caveat on the row; the frame covers the window oldest first |
| The drawdown lock-out on the constant-sleeve curve, peak-to-trough as % of the sleeve, the live 15 % / 10 % hysteresis | `_sleeve_drawdown_pct` → the same `drawdown_locked` / `exposure_tier`; `DrawdownSummary` (deepest drawdown, peak, trough, date, sessions locked) on the result and per book per year in the comparison | `test_drawdown_is_peak_to_trough_as_a_percentage_of_the_constant_sleeve` (by hand from the curve: 0.61 % on the losing tail); **`test_drawdown_of_sixteen_percent_locks_out_new_entries_until_back_within_ten`** at the real numbers — two 20 % positions, a gap to 22 on day 8, the sleeve **16.99 %** under its peak, `FLAGMID` refused `DRAWDOWN_LOCKOUT`, the lock held through 10.84 % on day 14 and lifted at 9.52 % on day 15, `FLAGLATE` entered on day 16 at rung 0, seven locked sessions counted; SW9.5's scaled case unchanged |
| Gate-on against gate-off (A12): three books over one detection pass | `GateMode` (`gate_off` = GREEN every session, ladder and lock-out still in force; `breadth_only`; `full`), `_Book`, `GateComparison` / `GateCell` / `GateContribution`; `comparison` in `to_json()` | `test_gate_off_never_enters_fewer_than_gate_on_and_a_red_tape_shows_what_breadth_costs` (four frames; the RED tape: breadth's contribution is −1 entered, −0.28R), `test_comparison_reports_the_three_books_per_year_and_per_setup` (1/1/0 entered; the index rule's contribution −1, −0.28R, on the year and on FLAG), `test_contribution_is_with_minus_without_on_every_scope`, the wire shape, the primary book unchanged |
| B1 costs from params; circuits from `upper_circuit` where present, no lock assumed where absent, **and the caveat says so** | `CAVEATS[3]`, verbatim in `04` §11 | `test_circuit_lock_is_read_from_upper_circuit_where_present_and_never_assumed_where_absent` (a band at the high locks; above it does not; null does not); the three existing cost tests |
| B2 only calendar sessions; delisted names sold at the last close and counted `DELISTED` | `run_backtest(..., delisted={instrument_id: delisted_on})`, `BacktestCloseReason.DELISTED`, `closed_delisted`; the runner's `load_delisted` | `test_calendar_holiday_bar_is_not_a_session_so_a_low_through_the_stop_on_it_never_fills`; `test_delisted_name_is_sold_at_its_last_close_on_its_last_bar_and_counted_delisted` (on the delisting day with a bar, on the first session without one, and `NO_BAR` with no map); the worker's `TestDelistedNames` through `instrument.delisted_on` |
| B3 the partial at the next open after the day-3–5 signal and the trail exit at the next open after the close below the MA, through `stops.manage` | unchanged (verified) | `test_partial_next_open_after_the_day_3_to_5_signal_is_stops_manage_own_decision` and `test_trail_next_open_after_the_close_below_the_ma_is_stops_manage_own_decision`: the same `manage` call on the same bar answers `SELL_PARTIAL` / `CLOSE_BELOW_TRAIL_MA`, and the fill is the next session's open, not the signal day's close |
| B4/B5 the stored stats carry the histogram, the max drawdown, by setup, by year, the funnel with skips by reason, the caveats, the params; byte-identical re-runs | `to_json()` keys `params, trades, stats, by_setup, by_year, equity_curve, funnel, ladder, drawdown, comparison, caveats`; `BacktestResult.caveats` = the standing four + `INDEX_ABSENT_CAVEAT` without an index | `test_determinism_holds_with_the_index_and_the_delisting_map` (core); `TestDeterminism` (worker: the engine's bytes twice, the two rows equal under JSONB); `test_to_json_is_plain_and_its_keys_are_in_a_fixed_order` |
| The card and the page | `swing_journal.backtest_stats` adds `max_drawdown_pct`, `drawdown`, `comparison` (numbers as `Decimal`, modes in the engine's order — JSONB keeps none); `backtest_caveats` composes the constant wording from the row's one fact; the page draws the drawdown tiles and two tables — "Entries and net R" and "Deepest drawdown of the curve" — by scope × book, with breadth's and the index rule's contributions as columns | `test_api_swing_journal` (+3: the comparison and the drawdown with numbers; no index → no index-rule contribution; a SW9 row still renders with the index-absent caveat); `page.test.tsx` (+6: the tiles, the lock-out count, the two tables' headers and rows, the absent column with its sentence, no comparison for an older run, no banned word) |

### The engine, restructured without changing a number

`_Run` became a shared detection pass over `_Book`s: detection, breadth and the index reading
are read once a session and every book takes its own gate, tier, fills, curve and funnel from
them. The primary book's numbers — the planted 492 shares, R = 0.28 to the paisa, every SW9 and
SW9.5 case — are byte-for-byte what the single-book engine produced; the funnel identity
`entered + Σ skipped_* == candidates` holds per book. `_liquidate` now counts its own reason
(`closed_no_bar` / `closed_delisted` / `closed_end_of_run`).

### Numbers

| Suite | Result |
|---|---|
| `packages/core/tests/test_swing_backtest.py` | **68 passed** (49 + 19), 91 s |
| `test_speed_300_instruments_over_8_years_runs_under_60s` | **26.9 s** with three books (27.5 s with one, measured before the change) — the detectors are the cost and they run once |
| `packages/core/tests` less SW10's `test_swing_safety_properties.py` (G7) | **2683 passed, 2 skipped**, 166 s |
| `services/worker/tests/test_swing_backtest_task.py` on `baskfy_sw_t2` | **24 passed** (18 + 6) |
| `services/api/tests/test_api_swing_journal.py` + `test_swing_readonly.py` | **43 passed** (27 + 16) |
| `apps/web` `tsc --noEmit` + `vitest run "src/app/(app)/swing/journal"` | **42 passed** (36 + 6); `no-jargon`, `jargon-ban`, `no-any` green |
| `make lint` | clean (the one pre-existing React-compiler warning in `data-table.tsx`) |

### Decisions

SW9.6.1 (the index read in-frame, the series resolved once at `params_for`, `index_slug` on the
row — A12), SW9.6.2 (the drawdown's denominator is the constant sleeve, ⚠ UNREVIEWED on that
one point — A12), SW9.6.3 (three books; what "gate-off" means; the columns; years by entry; the
"never fewer" claim is a fixture property, not a theorem — A12, ⚠ UNREVIEWED on the
definitions), SW9.6.4 (`DELISTED` beside `NO_BAR`; the fourth caveat; the card composes the
caveats from the row's one fact — B1–B5). `04` §11 amended with the rule names.

### What SW9.6 did NOT do

- **The 2017→ run has still not run over real bars** (SW9, SW9.5); the comparison and the
  drawdown on the page are proven on the planted year through the database, not on history.
  The dev database holds ten sessions and no index rows.
- **`tools/swing/backtest.py --fixture` prints the standing `CAVEATS`,** not the run's own, so
  its fixture run (no index) does not print the index-absent sentence; the database mode stores
  the run's own on the row and the card shows them. One line in SW12's pass (the CLI is 1.3.2's
  file).
- **`baskfy_core.swing.__init__` does not re-export the new names** (`GateMode`,
  `GateComparison`, `INDEX_ABSENT_CAVEAT`, …); the API, the worker and the tests import them
  from `baskfy_core.swing.backtest`. The package's `__init__` is 1.3.1's file; adding them is
  additive.
- **The runner reads one index series for the whole run** (SW9.6.1); the nightly job falls back
  per day. A window in which NIFTY 500 has twenty levels but a gap of a month inside is read as
  NIFTY 500 throughout, with the gap ignored the way the engine ignores any day with fewer than
  twenty closes behind it.
- **"Gate-off never has fewer entries than gate-on"** is asserted on the fixtures (SW9.6.3.4);
  over real history the lock-out and the tier can invert it on a session, and the page shows
  the numbers rather than the claim.
- **No per-mode ladder trace or equity curve** is stored — only the primary book's; the
  comparison stores each book's deepest drawdown per year, not its curve. A reviewer who wants
  gate-off's curve re-runs with `index=None` and reads `comparison`.
- **The card is still the latest finished run; there is no run picker** (SW9); a comparison
  between two *runs* (say, `adr_min_pct` 4.0 against 5.0) is a diff of two `--json` rows.
- `docs/swing/05-ui-spec.md` is not updated with the two new tables (the page follows `04` §11
  and the C2 card; `05` is 1.2.2's / SW12's document).
- `reconciliation/MUTANTS.md` is not regenerated; the backtest's own survivor list (SW9) may
  have moved with the restructure. `tools/mutation.py`'s target is unchanged.

---

## SW10 — Gating and safety proof, and the DRY_RUN morning drill ✅

**Goal, from `06`:** "the Track-B and Track-C claims are theorems, not intentions." Each claim in
`02` Track B / Track C that the code makes about itself is now a test that fails the moment it
stops being true, and the whole paper session runs end to end through the production code
paths with the orders counted.

### The theorems, and where each lives

| Claim (`02`) | Asserted how | Where |
|---|---|---|
| Track C §1 — `PARABOLIC_SHORT` is never a plan line | **hypothesis**, 500 examples from a fixed seed (`SEED = 20260902`, in every assertion message): random watchlists mixing all three setups, any score, stops from 15 % below to 2 % *above* the trigger, locked flags, held names, any gate, the ladder's rungs and random tiers, sleeves from ₹0 to ₹5 cr. `build_entries` never emits a line carrying it, always answers it `NOT_TRADEABLE_SETUP` — that reason and no other, before the gate or the money are consulted — and every watched name is answered exactly once. Plus the strongest hand-built counter-example (perfect score, GREEN, top rung, ₹1 cr, deep turnover: no line) and the constant itself (`TRADEABLE_SETUPS == {FLAG, EP}`) | `packages/core/tests/test_swing_safety_properties.py` |
| `04` §6.5 — a stop never falls | over `apply` with random action sequences that include a `RAISE_STOP` *below* the resting stop: the stop after is ≥ the stop before, ≥ every level a RAISE asked for, never a level nobody named, idempotent under re-application; and over random walks of `manage` → `apply` day after day: every RAISE the rules emit is to the entry and above the resting stop, and the book's stop is monotone until the position is gone | same file |
| Track C §5 — a SELL never exceeds what the sleeve owns | over `manage` → `exit_lines`: every SELL is for shares the position holds, a partial is strictly less than the whole, a close-out is exactly the whole, `apply` never goes negative, a RAISE line never sits below the resting stop; `build_entries` has no SELL in its vocabulary and never lines a held name (`ALREADY_HELD`); the entries never spend more than the cash, never push the book over the rung's ceiling, never exceed the rung's position count, and each risks no more than the budget | same file |
| Track C §5, at the desk | `execute_line`'s guard as a seeded loop of **500** random books and SELL lines through the real dry-run gateway: `BLOCKED` iff the line asks for more than `quantity_open` (or nothing, or a name the book does not hold), the book untouched; otherwise one fill for exactly the line's quantity and `quantity_open` never below zero. And `_sell`'s code reads `open_position_for`, never a holdings call | `kite-momentum-rebalancer/tests/test_swing_track_c.py` |
| `04` §6.5, at the desk | 500 random resting stops and RAISE levels: at or below → `BLOCKED` with "never falls" and the trigger untouched; above and below the last price → re-armed at exactly that level | same file |
| Track C §4 — no web route under `/swing` reaches an order | from the API's side of the wire: **every** `.ts`/`.tsx` under `apps/web/src/app/(app)/swing` and `lib/swing` — nine files, counted, seven routes and two tests — read through a small TypeScript lexer that tells code from strings, regex literals and comments. A route's *code* names no execute/rearm path, placing verb, `kiteconnect`, `OrderGateway`, `baskfy_execution`, `confirm=true`, non-GET method, server action or form, and imports nothing whose specifier says execution/gateway/kite/broker/order/desk; a test file may *name* those words (they are the vitest's vocabulary) but with every string blanked calls no `fetch(` and declares no server action; every `readOrNull` path in `fetch.ts` is on the same seven-entry read whitelist the vitest keeps. The lexer has its own test | `services/api/tests/test_swing_track_c.py` |
| Track C §4 — the API has no path to the gateway | every `import` in `baskfy_api` scanned: the only `baskfy_execution` pieces it may import are the pure shapes it already does (`broker_ports`, `tenancy`, `client_ids`, and `TenantIds` / `mint_client_id` / `refuse_cross_tenant` from the root); the gateway, `gtt`, the adapters, the guards, `kiteconnect` and the desk's `app.` are offenders; and the code (docstrings stripped) never names `OrderGateway`, `place_order`, `place_gtt`, `.place(` | same file |
| Track B — with the flag false no path from `/swing/execute` reaches a non-dry-run adapter | a **spy** wraps the REAL swing gateway built by `build_swing_gateway` over an exploding broker client and records the gates every `place` / `place_gtt_stop` / `delete_gtt` ran under, **through the route** — a buy (LIMIT + GTT), a partial sell (MARKET + cancel + GTT), a raised stop (cancel + GTT) and a re-arm — with `DRY_RUN` true *and* false: eight calls, every one `dry_run=True`, every journal event a dry run, every row `simulated`. The truth table has exactly one live cell (flag on **and** `DRY_RUN=false`), and the spy is shown not to be blind: under that cell it records `dry_run=False` and the exploding fake is what stops the order (`REJECTED`, no position, no stop, an `error` in the journal). The gateway is built lazily and holds `swing_gates` as a callable | desk file |
| Track C §3 — the monitor's strategy has no place call | over the strategy's and the runner's code with docstrings stripped, a word-boundary scan (`\bplace\b`, `place_order`, `place_gtt`, `.place(`, `delete_gtt`, `OrderGateway`, `\border\b`; the strategy also `self.gw`, `kc.`, `kiteconnect`, `kite_client` — the runner may name the Kite wrapper because it *reads* quotes and candles through it); `main` hands `gateway=None`; `build_monitor(enabled=False)` builds nothing; `generate_targets` answers `[]` at runtime. And the scan is shown to read code, not prose: the docstring says "place", the code does not | desk file |
| Track C §6 — every `sw_` write carries `BASKFY_SOLE_USER_ID` | **schema:** every `sw_` table in the ORM (twelve, plus 0029's `sw_backtest_run`) has a NOT NULL `user_id`. **API + worker code:** every `SwX(...)` constructor, every `insert(SwX).values(...)` (a bulk `values(batch)` is accepted only if the enclosing function builds `"user_id"` into the payload) and every raw `INSERT INTO sw_` / `UPDATE sw_` — fourteen sites, counted, the two easy-to-miss shapes asserted present — names `user_id`; the tenant is resolved once in `providers.py` from `BASKFY_SOLE_USER_ID` and the swing tasks neither read the environment nor pass a literal; every `routers/swing.py` handler calls `scoped_sole_user_id`. **Desk code:** every `INSERT` / `UPDATE` in `swing_desk` / `swing_monitor` / `swing_execute` — statement by statement, f-strings rendered, a dynamic column list (`create_position`, `add_fill`) accepted only when the function builds it with `'user_id'` — names the user, with one whitelisted `UPDATE sw_signal … WHERE id = ?` and its reason (the row was inserted with the store's user four lines earlier and is addressed by the id that INSERT returned; the whitelist is asserted still real); the id is `C.SOLE_USER_ID` / `BASKFY_SOLE_USER_ID` and never a literal. **Runtime:** after a buy, a sell, a raise and a re-arm through the real module every row in every `sw_` table is the user's, and another tenant's store over the same file sees nothing | both files |
| Track C §1/§2 at the desk — CNC only, no leverage | both `gw.place(` calls in `swing_execute` pass `product="CNC"`, `exchange="NSE"`, `order_type` LIMIT or MARKET and no `variety`; the code names no `"MIS"`, `"NFO"`, `"BFO"`, `"co"`, `"bo"`, `MTF`; `swing_gates()` refuses intraday and options even with the weekly desk's `INTRADAY_ENABLED` / `OPTIONS_ENABLED` on; and a hand-edited `product="MIS"` is `BLOCKED` inside the gateway itself | desk file |

Numbers: `test_swing_safety_properties.py` **8 passed** (5 properties × 500 examples + 3 exact),
`services/api/tests/test_swing_track_c.py` **19 passed**, `kite-momentum-rebalancer/tests/test_swing_track_c.py`
**37 passed**; desk suite green; `make lint` clean; `test_no_escape_hatches` green over the two
new decile test files.

### The confirm-time gate (STANDING-ANSWERS A5 → SW10.4, SW10.5)

SW10.2's finding — two SIGNAL lines each inside rung 0's 25 % confirming together to 34 % —
is closed the way Maulik decided it: **the confirm is the gate**. Measured on 2 Sep 2026.

| What | Where | Proven by |
|---|---|---|
| **The lock.** Every confirm runs inside `PgSwingStore.lock_session_for_update(day)`: `BEGIN`; the day's `sw_session` row inserted if absent; `SELECT … FOR UPDATE` on it (sqlite twin: `BEGIN IMMEDIATE`); the line's state read again under the lock (a second tab answers 409); the `CONFIRMED` mark, the context, the re-size, **the gateway call**, the position, the fill, the GTT and the counters; `COMMIT` — an exception rolls it all back and the line is then marked `REJECTED` outside it. SELL and RAISE take it too | `app/swing_desk.py`, `app/swing_execute.py::execute_line` | desk: commits, keeps an existing row's counters, rolls back everything on an exception and stays usable, **is real across two connections** (the second's `BEGIN IMMEDIATE` fails while the first holds it, succeeds after); execute: the lock is entered before `place` and left after `place_gtt_stop` for every kind, a gateway exception leaves the line `REJECTED` and the click counted once, a line another request filled is 409 with nothing sent |
| **The context.** One reader for the monitor and the desk — `swing_monitor.load_context(conn, user_id, day, schema)`: the latest market row strictly before the day (gate, rung, `drawdown_locked`), the sleeve's capital, open positions at cost, today's `BUY_ON_TRIGGER` lines in `CONFIRMED`/`SENT`/`FILLED` (`ENTRY_TAKEN_STATES`; the ones not yet a position held at the trigger as `PendingLine`s), `entries_today`, each name's ADR/turnover/score — read **before** the line is marked so it is never counted against itself | `app/swing_monitor.py`, `PgSwingStore.session_context` | monitor: positions at cost + a `SENT` line at its trigger + a `FILLED` line counted once; no market row → RED; desk over the twin: rung 2 → (6, 75 %), ₹93,390 held + ₹30,050 `SENT`, `entries_today` 2, the ADRs, the latest row before the day never the day's own, the drawdown lock carried, another user sees nothing |
| **The re-size.** `resize_buy` → `swing_monitor.entries_now` → `build_entries` with the person's three sizing knobs (`sizing_config`) and `entries_already_today` (the additive `04` §5.3 argument); `min(planned, allowed)` goes — never more than the page showed; a smaller size is written back (`resize_line`: quantity, `risk_inr`, `position_value`, a note) and carried by the position, the fill and the GTT; when only the ceiling refuses, one more pass with the cash bounded by the headroom (SW10.5); no line → `BLOCKED` with the skip code leading the reason, the line `REJECTED`, nothing journalled | `app/swing_execute.py`, `app/swing_monitor.py::entries_now`, `packages/core/.../plan.py` | execute (20 new): 833 → 390 at 210 beside ₹1,67,932.80 → 24.98 %, `EXPOSURE_FULL` when ₹2,000 of headroom cannot buy ₹10,000, `TIER_FULL` by rung and by the person's cap of 1, `SESSION_CAP` on the fourth (`SENT` counts, `REJECTED` does not), a `SENT` line is exposure, 100 of an allowed 1,666 sends 100, 0.25 % risk halves 1,666 → 833, `GATE_RED` / `DRAWDOWN_LOCKOUT` / no ADR / 6 % stop on a 5 % ADR / `PARABOLIC_SHORT` refused, the partial-then-full sequence (whole, shrunk, `TIER_FULL`; ₹2,49,832.80); desk through the route: 1,666 → 1,553 against ₹93,390 held at rung 0 (₹2,49,932.40), `EXPOSURE_FULL` / `TIER_FULL` / `SESSION_CAP` as JSON with the row `REJECTED`; core: `entries_already_today` as a hypothesis property (no line added past the cap; `SESSION_CAP` for every eligible name once the session is full) and an exact case (default 0 is the same plan) |
| **The monitor re-reads per trigger.** `PgSignalStore._plan_for` → `read_context()` before every plan; `load_config` carries the person's sizing knobs; the `context=` handed in is the log's starting reading, never what a plan is sized from | `app/swing_monitor.py` | monitor (7 new): the 09:45 trigger after the 09:31 confirm is lined 390, not 833 (book ₹2,49,832.80 ≤ ₹2,50,000); the fourth trigger of a session is `SESSION_CAP`; ₹2,000 of headroom is `EXPOSURE_FULL` with the arithmetic in the detail; a stale GREEN context over a RED database is `GATE_RED`; the knobs reach `SizingConfig` |
| **The property.** 500 seeded sequences of 1–8 confirms on random sleeves (₹2–50 lakh), rungs, caps (1–10), risk knobs, 0–3 positions already held and lines sized by nobody in particular, through `execute_line` and the real dry-run gateway (unthrottled) | `tests/test_swing_track_c.py` | after every confirm that went: book ≤ ceiling, count ≤ `min(rung, cap)`, ≤ 3 entries, sent ≤ planned, the position/fill/GTT carry the size sent; after every refusal: the book exactly what it was, the reason a skip code, the line `REJECTED`; every case took the lock once per line; the journal is `dry_run, gtt_dry_run` per entry (band warnings aside) and nothing broker-shaped; the distribution asserted non-empty for every code — one run: 338 sized, 416 re-sized, 1,075 `TIER_FULL`, 317 `SESSION_CAP`, 25 `EXPOSURE_FULL`, 18 `SIZE_REFUSED` |

The drill's step 5 now confirms the same two lines and prints
`re-sized at confirm 833 → 389 (A5)` and `EXPOSURE after confirms ₹249,817.30 = 25.0% of the
sleeve (rung ceiling 25% = ₹250,000.00); 2 entries today, 2 of 2 positions at rung 0` — and
exits 1 if the book is over the ceiling, the session does not count exactly two entries, the
count is over the rung's, or the number of re-sized lines is not exactly one.

**Numbers, re-measured:** `tests/test_swing_execute.py` **94 passed** (74 + 20),
`tests/test_swing_desk.py` **90 passed** (73 + 17), `tests/test_swing_monitor.py` **27 passed**
(20 + 7), `tests/test_swing_track_c.py` **39 passed** (37 + 2); desk suite **1,599 passed, 17
skipped**; `packages/core/tests/test_swing_safety_properties.py` **10 passed** (8 + 2);
`test_swing_docs_parity` green; `make lint` clean over this leaf's files (the one mypy red in
the tree at the time of measurement was `services/worker/tests/test_swing_backtest_task.py`,
leaf 1.3.4's file, mid-edit).

### The DRY_RUN morning drill — `tools/swing/drill.py`

`RUN-AND-TEST.md` §"The swing book's DRY_RUN morning drill (SW10)" has the command, the
printed run and what "0 orders" is proven by. In one line: the evening before (`run_swing_eod`,
18 Aug — the watchlist fills itself from the detectors' rows) → 08:50 `LEVELS` → 09:09 the
`MORNING` plan (flag off, no quote) → the fixture morning replayed through
`SwingBreakout` + **`PgSignalStore` over the desk's Postgres adapter into the same database**
(four signals, exactly the fixture's; two `SIGNAL` lines; `monitor_ran` marked) → the two
`TRIGGERED` lines confirmed through **`execute_line` + `PgSwingStore` + the real gateway** over
an exploding broker client, each under the session's row lock (two `SIMULATED` positions with
`DRY-…` stops, the second re-sized 833 → 389 to the rung's ceiling, two simulated fills, the
swing journal exactly `dry_run, gtt_dry_run` twice, broker touched 0 times, the book 25.0 % of
the sleeve) → the close prints → 21:05 `run_swing_eod` (two positions managed; ALPHAFLAG up 1.23R, so `BREAKEVEN_AT_R`
raises its stop to the entry; the ladder settles; the preview is built; the second session is
counted) → the next morning's `LEVELS` and `MORNING` plan (the `RAISE_GTT_STOP` line, the two
held names `ALREADY_HELD`, `DELTAWAIT` `TIER_FULL` at rung 0, `GAMMALOCK` locked). Then every
`sw_` row is checked to be the sole user's (51 rows), and the counters print:

```
EXPOSURE after confirms ₹249,817.30 = 25.0% of the sleeve (rung ceiling 25% = ₹250,000.00); 2 entries today, 2 of 2 positions at rung 0
…
sw_session 2026-08-18: mode=DRY_RUN monitor_ran=False signals=0 confirms=0 fills=0 manage_actions=0 plans=1
sw_session 2026-08-19: mode=DRY_RUN monitor_ran=True  signals=4 confirms=2 fills=2 manage_actions=0 plans=1
orders that reached a broker: 0   (journal: dry_run, gtt_dry_run, dry_run, gtt_dry_run)
DRILL OK
```

It exits 0 in a few seconds against `baskfy_sw_t3` once the database is at head (about 20 s
the first time, most of it `alembic upgrade head`). It refuses
to start with `DRY_RUN=false` or the execution flag on, refuses to reset a database whose name
does not say it is disposable, and exits 1 with the reason on any step that does not do what
the rules say. **This is the first time the desk's `PgSwingStore` and `PgSignalStore` have
written a real Postgres** — SW7's "no morning has been rendered against Postgres" is now false
for the store (the page itself is still rendered over sqlite only).

### What the drill found

- **A morning's SIGNAL plans did not see each other's confirms** (SW10.2) — the 09:45 `BETAEP`
  line was sized against a book that did not yet hold `ALPHAFLAG`, and the two confirmed lines
  together were ₹3.43 lakh, 34 % of the sleeve, over rung 0's 25 % ceiling. **Closed by
  SW10.4** (STANDING-ANSWERS A5): the confirm re-derives the book under the session's row
  lock and re-sizes, the monitor re-reads its context per trigger, and the drill now fails —
  not warns — on a book over the ceiling. The same run prints 25.0 %.
- `sw_session.plan_ids` records only the evening's plan (`plans=1` on a day that built a
  `MORNING` plan and two `SIGNAL` plans besides) — the premarket job and the monitor do not
  append theirs. Recorded, not fixed: the fields belong to SW5/SW6's files.

### Decisions

SW10.1 (how the drill fakes the broker and the clock, and why the monitor flag stays false),
SW10.2 (the SIGNAL plans sized against the 09:15 context — the finding, now closed),
SW10.3 (what the source scans admit and why: the API's three pure imports from
`baskfy_execution`, the runner's Kite reads, the one `UPDATE … WHERE id = ?`),
**SW10.4** (the confirm is the gate — the lock, the context, the re-size, the per-trigger
re-read, the proofs; Maulik's, STANDING-ANSWERS A5), **SW10.5** (a live trigger that does not
fit whole is taken at the size that fits; the evening and the morning plans still skip —
⚠ UNREVIEWED), **SW10.6** (what the sqlite twin, the census and three SW7 tests had to learn —
⚠ UNREVIEWED).

### What SW10 did NOT do

- **The page does not say a line was re-sized until it has been.** The desk's trigger row
  shows the SIGNAL plan's own size (which, since the monitor re-reads per trigger, is what the
  confirm will send unless the book moves between the trigger and the click); after a confirm
  the row shows the re-sized quantity and the `note` column carries the arithmetic on the
  plan panel. A "would be re-sized to N" preview on a stale line would need the view to run
  `entries_now` per line; not done — the confirm's outcome renders inline and says it.
- **`first_live_sessions_left` still halves the *sent* quantity, not the line** (SW7.2) — the
  re-size writes the row, the halving does not; MD12 moves the halving to plan time and is
  SW10.5's-module work (the ledger's next), not this gate's.
- **The lock is per user, per day, and the desk is one process.** Postgres serialises two
  confirms with `FOR UPDATE`; the sqlite twin with `BEGIN IMMEDIATE`; nothing here serialises
  the desk against the evening job's writes to the same tables (they run at 21:05, outside
  any confirm) or against the monitor's `INSERT`s (it writes plans, never positions).
- **The re-size's second `build_entries` pass computes the ceiling once more** (`equity ×
  max_exposure_pct / 100`) to bound the cash — the one place outside `build_entries` that
  knows the ceiling's formula. SW10.5 says why and how to remove it.
- **`sw_session.plan_ids`** (above) is still SW5/SW6's.
- **The drill does not run the detectors.** They need 125 sessions of history per name; the
  drill writes the two tables they would have produced (`sw_setup_daily`, `sw_market_daily`)
  by hand for four synthetic names and says so. The detectors have their own suite (SW3) and
  the nightly chain runs them for real.
- **The drill does not render the desk page or post through the route.** It calls
  `execute_line` the way the route does (a `PgSwingStore`, the swing gateway, a tz-aware
  `now`, no price for a buy). The route itself is proven through the sqlite twin (SW7's 73
  tests and the spy tests here); the page over Postgres remains unrendered.
- **The drill's morning is the synthetic fixture**, not a recorded one. `tools/swing/replay.py
  --journal` can replay a real `bus.last_tick` journal, and nothing writes one yet (SW6).
- **Property tests cover the pure core and `execute_line`'s guards**, not the evening job's
  SQL or the store's SQL; those are covered by their own database-backed suites (SW5, SW7) and
  by the drill end to end.
- **`hypothesis` is not in the desk's venv** (`docs/02` locks it for the screener), so the
  desk's two property loops are `random.Random(SEED)` over 500 cases rather than shrinking
  strategies. The seed is in every message; a failure reproduces exactly but does not shrink.
- **No Playwright, no Mailpit.** The evening email is handed to a recording transport and its
  subject printed.
- The desk test file imports `tests.test_swing_desk` and `tests.test_swing_execute` as modules
  for their fixtures (the sqlite DDL twin, `MemoryStore`, `ExplodingKC`); a change to either
  suite's fixtures is a change to this one. Deliberate — one DDL twin, not two.
- **The drill and the worker suite share `baskfy_sw_t3` and alternate cleanly** — proven by
  running drill → suites → drill → suite — but only after a defect the first pass hid: the
  drill's user sat on `@example.com` with a `broker_account` row, and the worker conftest's
  sweep of that domain then failed 17 tests with a foreign-key violation *behind a green
  `LINT_OK`*. The drill now owns its id outright and names its user outside the sweep
  (SW10.1 §5). The lesson for G8-shaped gates: an `EXPECT` that matches the last line of a
  chain can pass over a red suite earlier in it; the numbers above were re-measured by hand.

---

## SW10.5 — Maulik's review corrections: A7, A8, A9, A10, A14, B7 ✅

**Goal, from `STANDING-ANSWERS.md`:** apply the six answers that change behaviour, on top of
SW10 (`f17412f`), and record each as "Maulik, 2 Sep 2026 (STANDING-ANSWERS §n)". Everything
below was **measured on this machine on 2 Sep 2026**, on `baskfy_sw_t3`.

### What changed, answer by answer

| Answer | Where it now lives | Proven by |
|---|---|---|
| **A7 — `PENDING_RANGE`.** A live gap (trigger, no stop) is a line on the MORNING plan: no quantity, no stop, a preview at a 1-ADR stop, sorted by score, holding one of the session's three new-entry slots (not a seat in the rung — SW10.5.1); real only through the SIGNAL plan with `stop = min(range low, LOD)`; wider than one ADR → `STOP_TOO_WIDE` and the slot released the moment the name triggers (once); slots nothing claimed freed at 10:45; never executable | `plan.LineKind.PENDING_RANGE`, `plan.EXECUTABLE_KINDS`, `WatchItem.stop_ref: Decimal \| None`, `build_entries`; `swing_eod.watch_items` (keeps a stop-less row; the watch row's ADR/score when no detection row); `swing_premarket` (live gaps written with `score` + `adr_pct`, `pending_lines` on the report); `swing_monitor.load_context.reserved` / `entries_now` / `PgSignalStore.release_reservation`; `swing_execute.EXECUTABLE_KINDS` + `_validate`'s 400 + `cutoff_open_orders` → `expire_pending`; the desk route's own 400; the page's row with no button; `0031` rebuilt `ck_sw_plan_line_kind_known` | core `test_swing_pending_and_first_live.py::TestPendingRange` (12 incl. a hypothesis property: never a quantity, a stop or cash on a pending line, every name answered once); worker `test_swing_premarket` (a live EP is a pending line, not a buy; a gap outscoring three flags takes a slot and the third flag is `SESSION_CAP`); desk `test_swing_execute` (400 before the lock under both gate settings; the source set), `test_swing_desk` (no button, the route's 400 with the module never asked, the sqlite twin's `expire_pending` once), `test_swing_monitor` (a reserved slot counts for other names, not for its own; released exactly once before the plan is sized; `STOP_TOO_WIDE` releases) |
| **A8 — the fill gap.** Marketable LIMIT at `min(trigger × 1.005, range_high + 0.25 × ADR)`, snapped down, never MARKET; the request polls the order ≤ 10 s at 2/s (injectable `OrderSource`, clock and sleep); COMPLETE → position + fill + GTT in the request; partial/open → `SENT` with the filled quantity and a GTT for exactly it; `on_order_update` grows the position at the price that keeps the averages consistent and **modifies** the GTT, never a second one, idempotent on a repeat; 10:45 cancels the remainder through the gateway, GTT untouched; 15:15 hook re-arms naked positions; the dry-run path is the same bookkeeping with `simulated=true` and no poll | `plan.marketable_limit`; `OpeningRangeConfig.entry_limit_buffer_pct` [0.5], `entry_limit_max_adr` [0.25], `fill_poll_seconds` [10], `fill_poll_interval_seconds` [0.5]; `baskfy_execution.OrderGateway.modify_gtt_quantity` and `cancel_order` (additive; tenant, untouchables, stop check, kill-switch note, rate limit, journal, dry-run branch; the weekly paths byte-for-byte unchanged); `swing_execute._apply_buy_fill` / `_poll_fill` / `on_order_update` / `_grow_position` / `cutoff_open_orders` / `eod_gtt_sweep`; `swing_desk.KiteOrders` (order history — a read), `POST /swing/reconcile`, `POST /swing/cutoff`; `load_context` counts a resting remainder at the trigger | execution `test_gtt_modify_and_cancel.py` **18** (a modify addresses the resting id and never places; dry-run touches nothing; untouchables, cross-tenant, zero shares, a malformed id, a non-stop trigger refused before the broker; the kill switch does not stop a re-size; twice is not a duplicate; a cancel reaches the broker once, dry-run does not, the weekly files never name either); desk `test_swing_execute` (**117**: the limit 101.25 on a 100.80 break of a 100.00 range; the journal's dry-run price is the limit; 21 reads and 20 half-second sleeps in ten seconds, early stop on COMPLETE; COMPLETE → position/fill/GTT/`FILLED`; a partial is `SENT` with a GTT for 40 of 100; partial-then-complete ends with one GTT modified 40 → 100 and the 60 at 100.50; a repeated and a stale postback write nothing; the first fill by postback; modify never above the filled quantity; a naked partial armed for the whole; the cutoff cancels the remainder, leaves the GTT, applies a fill that arrived since, expires an unfilled line, reports a refused cancel, leaves a completed order alone, frees unclaimed slots once; a dry-run fill follows the same path without polling; a rejected order writes no book; the 15:15 stub) |
| **A9 — half risk at plan time.** `risk_multiplier` 0.5 on `risk_per_trade_pct` before `size_position` while `first_live_sessions_left > 0` **and** a confirm would be real (paper plans full size — SW10.5.3 records the reading); SELL/RAISE untouched; the evening decrements once when a LIVE session closes (`sw_session.first_live_counted`, audited `swing-eod`), never a request; a restart changes nothing; the fifth session → 0 and the sixth plans at full risk; header "first live sessions: N left · risk 0.250%"; `sw_position.half_risk` | `SizingConfig.risk_multiplier_first_live` [0.5], `first_live_sessions` [5]; `plan.first_live_multiplier`, `sizing_at`, `build_entries(risk_multiplier=)`; `swing_eod.count_first_live_session` + `first_live_header` + `risk_pct_in_force`; the premarket and the monitor size with it; `swing_execute.risk_multiplier_for` + `sizing_config(risk_multiplier=)`; `first_live_quantity` / `_FIRST_LIVE_COUNTED` / the desk's countdown write **removed**; `SYSTEM_OWNED_FIELDS["first_live_sessions_left"] = "swing-eod"`; the desk view's `first_live_header` / `half_risk` | core `TestRiskMultiplier` (7 incl. a hypothesis property: the risk scales linearly, every refusal still applies; SELL/RAISE lines byte-identical with and without); worker `test_swing_eod::TestTheFirstLiveCountdown` (**5**: decremented once across a re-run, audited `5 → 4`; a DRY_RUN session moves nothing; 833 on paper and 416 live from one countdown, the header text; six live evenings `[4,3,2,1,0,0]` planning `[416,416,416,416,833,833]`; a second call after a "restart" moves nothing), `test_swing_premarket` (the morning plan at half risk only when live); desk (1,666 → 833 sent, written back and tagged; never halved simulated; full size at 0; no request moves the count — asserted on the store's write log and the module's code; SELL/RAISE never halved), monitor (833 / 1,666 / 1,666 across the three flag states) |
| **A10 — real closes from day one.** The ladder, the NAV and the drawdown read `simulated = False`; PACK.6's paper clause and the book-switch peak reset are gone; the 09:09 job settles the previous session only when no settlement record exists, never twice | `tasks/swing.load_closed_trades`, `sleeve_nav`, `sleeve_drawdown`; `swing_eod.closed_r_multiples` (default `simulated=False`), `settle_ladder` (`reads = "real"`, `_book_switched` deleted); `swing_premarket.catch_up_ladder` (+ `ladder_caught_up` on the report) | worker `test_swing_ladder` (re-pinned: real closes move the rung with the flag off; five paper wins move nothing with the flag off and on; a paper close never counts toward the NAV the peak is measured on), `test_swing_premarket::TestTheCatchUpSettlement` (**4**: a missed evening is settled at 09:09 on real closes and the plan built on it; a settled session is never a second settlement; the catch-up itself runs once; paper closes move nothing at 09:09 either), `test_swing_detect` (`closed_trades_read == "real"`) |
| **A14 — the funnel.** Top 20 `SETTING_UP` flags by score + every EP auto-watched each evening (rows carry `score`, `adr_pct`); the monitor watches all; daily focus = top 5 by score + every EP (`sw_watch.focus`, recomputed by the evening and the premarket after the gap scan; the desk page puts focus triggers first); DETECTOR flags expire after 10 sessions or on trigger; MANUAL rows after 10 sessions unless re-confirmed (`PATCH /swing/watch/{id}` `{"reconfirm": true}`; a DETECTOR row is refused 400; a pre-0031 MANUAL row gets its clock once) | `WatchConfig.auto_watch_top_n` [20], `focus_top_n` [5], `manual_valid_bars` [10]; `swing_watch._top_flags`, `refresh_focus`, `reconfirm`, `NotReconfirmable`, `expire_stale`'s backfill, `add_manual`'s expiry; `SwingWatchOut.score/adr_pct/focus/reconfirmed_on`, `SwingWatchPatch.reconfirm`; `swing_desk.signals_for` joins `focus`; `openapi.json` + `schema.ts` regenerated (additions only) | worker `test_swing_eod::TestTheWatchFunnel` (**5**: 25 flags → the top 20 by score + the EP, with score and ADR on the row; focus = `EPCO` + the top five; focus recomputed, not accumulated; a MANUAL row expires on day 11 and a re-confirmed one does not; a pre-rule MANUAL row is given its clock once); API `test_api_swing` (a hand-added row now expires; re-confirm restarts the clock and moves no level; a DETECTOR row is refused; `focus` / `score` / `adr_pct` on the read model) |
| **B7 — the drill.** Five signals (flag break, EP, locked circuit, below-pivot, and the live gap's break skipped `STOP_TOO_WIDE` with its slot released), a late partial fill through the postback handler, two confirms (one re-sized 833 → 389 against a book that counts the resting remainder), the 10:45 sweep cancelling the 666 remainder with the GTT untouched, the 15:15 hook, EOD, the next morning's plan showing the pending line again; the counters; **0 orders reach a broker** | `tools/swing/drill.py` (`DrillQuotes`, `DrillOrders`, the live gap seeded liquid with 60 bars, `confirm_two_lines` restructured — SW10.5.6 on the one state the drill writes), `tools/swing/fixtures/morning-live-gap.{csv,watchlist.json,expected.json}` (the four names of `morning-synthetic.*` plus `EPSILONGAP`; the SW6 fixture untouched) | the drill itself, exit 0 (the printed run is below); `tools/swing/replay.py --expect` on the new fixture exits 0 |

### The schema (`0031_swing_review_corrections`)

`sw_watch.score` (5,2), `adr_pct` (10,2), `focus` bool, `reconfirmed_on` date;
`sw_position.half_risk` bool; `sw_session.first_live_counted` bool; `ck_sw_plan_line_kind_known`
rebuilt with `PENDING_RANGE` (the migration writes the list out; a test asserts it equals
`SW_LINE_KINDS`). Round-tripped `upgrade → downgrade 0030 → upgrade` on `baskfy_sw_t3`. `03`
§1, §1b, §4, §6, §7, §8 amended; `test_schema_matches_docs` names all six columns.

### The drill, as printed (2 Sep 2026, `baskfy_sw_t3`)

```
  3. 09:09 MORNING   MORNING plan 2026-08-19: universe 1, quotes pulled 1 (scripted), gaps ['EPSILONGAP'] (+1), entries 1, pending 1, exits 0, skips 3, focus 5, ladder caught up: False
                       SWING BUY ALPHAFLAG x1666 @ 100.00 stop 97.00 risk ₹4998.00 [PROPOSED]
                       SWING PENDING EPSILONGAP — range at 92.00, no stop yet; EP score 55.42; live gap, stop set by the opening range at window close; slot reserved; preview: ≈ 1063 shares (₹4996.10 at risk) if the stop lands 1 ADR (5.13%) below 92.00 [PROPOSED]
  4. 09:15-10:45     replayed morning-live-gap.csv through PgSignalStore: 5 signals, 2 SIGNAL lines, gate GREEN rung 0
                       09:38  TRIGGERED              EPSILONGAP entry=   94.60 stop=   88.00
                       EPSILONGAP: SIGNAL plan skipped it SIZE_REFUSED STOP_TOO_WIDE (…); the PENDING_RANGE slot is released — slot released at 09:38
  5. confirm         09:50 SWING BUY ALPHAFLAG x1666 @ 100.80 stop 97.80 → SENT as a live marketable LIMIT would be (limit 100.75 = min(100.80 x 1.005, range high 100.50 + 0.25 x 5% ADR)), order DRILL-ORD-1, nothing filled yet; the drill wrote this state — the dry-run branch cannot
                       09:55 SWING BUY BETAEP x833 @ 210.50 stop 204.50 → SIMULATED; position 1 x389 gtt DRY-… simulated=True
                         re-sized at confirm 833 → 389 (A5): … book ₹167,932.80 + ₹81,884.50 = 24.98% of the sleeve, ceiling 25% at rung 0, 1 entry today
                       10:20 postback: 1000 of 1666 ALPHAFLAG filled at 100.85 → position 2 x1000 gtt DRY-…:ALPHAFLAG:GTT (for exactly 1000, the real gateway's dry-run branch), line SENT — the remaining 666 still resting
                       EXPOSURE after confirms ₹249,867.30 = 25.0% of the sleeve — ALPHAFLAG ₹100,850.00 filled + ₹67,132.80 still resting at the trigger + BETAEP ₹81,884.50 (rung ceiling 25% = ₹250,000.00); 2 entries today, 2 of 2 positions at rung 0
  6. 10:45 sweep     reconciled 1, cancelled 1, refused 0, slots freed 0
                       ALPHAFLAG order DRILL-ORD-1: remainder 666 cancelled through the gateway (dry-run), line FILLED, position x1000 gtt DRY-… — 10:45 cutoff: 1000 filled, the remaining 666 cancelled (DRILL-ORD-1); GTT untouched
                       15:15 sweep hook: 0 naked position(s) re-armed
                       swing journal (swing_orders_journal.jsonl): dry_run, gtt_dry_run, gtt_dry_run, order_cancel_dry_run
                       broker client touched: 0
  8. 21:05 EOD       EOD 2026-08-19: gate GREEN rung 0→0 (real closes: none), watch +0 −0, managed 2, exits 1, entries 0, pending 1, skips 4, naked none, sessions logged 2, first live sessions left 5 (risk x1: 0.500%)
  sw_session 2026-08-18: mode=DRY_RUN monitor_ran=False signals=0 confirms=0 fills=0 manage_actions=0 plans=1
  sw_session 2026-08-19: mode=DRY_RUN monitor_ran=True  signals=5 confirms=2 fills=2 manage_actions=0 plans=1
  orders that reached a broker: 0   (journal: dry_run, gtt_dry_run, gtt_dry_run, order_cancel_dry_run)
  late partial fill: 1000 of 1666 ALPHAFLAG at 100.85 by postback, the rest cancelled at 10:45; one GTT, for exactly 1000
DRILL OK
```

### Numbers, re-measured

| Suite | Result |
|---|---|
| core `packages/core/tests` (the whole package; docs parity included) | green — `test_swing_pending_and_first_live.py` **26 passed** (2 hypothesis × 200), `test_swing_docs_parity` green with the nine new fields and the new kind named in `04` |
| execution `packages/execution/tests` | **188 passed** (170 + 18) |
| worker `test_swing_eod` / `test_swing_ladder` / `test_swing_premarket` / `test_swing_detect` | **green** on `baskfy_sw_t3` (+10 eod, +4 premarket A7/A9, +4 catch-up; ladder re-pinned, 3 replaced) |
| API `test_api_swing` / `test_swing_readonly` / `test_swing_schema_and_settings` / `test_swing_track_c` / `test_api_artifacts` / `test_schema_matches_docs` | **green** (+3 API, +2 schema, +6 docs) |
| desk `tests/` | **1,645 passed, 17 skipped** — `test_swing_execute` **118**, `test_swing_desk` **105**, `test_swing_monitor` **34**, `test_swing_track_c` **39** |
| `make lint` | clean (ruff, format, mypy) |
| the drill | `DRILL OK`, 0 orders, 58 `sw_` rows all the sole user's |

### Decisions

SW10.5.1 (A7 — a session slot, not a tier seat; the locked pending line; the provisional score
and the watch row's ADR — ⚠ UNREVIEWED on those three), SW10.5.2 (A8 — the pull transport
instead of an unauthenticated postback URL, the cancel's line state, the local simulated modify,
`cancel_order` on the gateway with named statuses, the resting remainder as exposure — ⚠
UNREVIEWED), SW10.5.3 (A9 — "execution is enabled" read as "a confirm would be real"; the
countdown moves on a LIVE evening with or without an order — ⚠ UNREVIEWED on the reading),
SW10.5.4 (A10 — no judgement call; the journal card's `reads` is leaf 1.3.4's file), SW10.5.5
(A14 — top 20 per evening, focus recomputed — ⚠ UNREVIEWED), SW10.5.6 (B7 — the one state the
drill writes by hand — ⚠ UNREVIEWED).

### A defect the re-read caught before it shipped

The desk's confirm applied the half-risk multiplier **twice** on a real Postgres context:
`sizing_config` scaled `risk_per_trade_pct` to 0.25 % and `entries_now` scaled it again from
the context's `first_live_sessions_left` (which `PgSwingStore.session_context` carries and the
in-memory test store did not) — 0.125 %, a 416-share line where 833 was right. The multiplier
is now applied in exactly one place (`entries_now`, from the context, when a real order would
go out); the test store's context carries the countdown; and
`test_first_live_risk_multiplier_is_applied_once_not_twice` pins it, including a scan that
`_buy` never scales the config itself.

### What SW10.5 did NOT do

- **No push transport for order updates.** The desk has no Kite postback URL: `websec` refuses
  a POST with no recognised Origin, and opening it for one path is a boundary this run did not
  draw on its own (SW10.5.2). Fills arrive by the confirm's own poll, by **Reconcile fills**
  on the page, by the 10:45 sweep — all pulls of the order book through the Kite wrapper. The
  KiteTicker's `on_order_update` websocket callback and a checksum-verified route are each a
  one-line call into `swing_execute.on_order_update`; SW11 chooses.
- **The 10:45 and 15:15 sweeps are routes and hooks, not scheduled.** `POST /swing/cutoff` and
  `eod_gtt_sweep` exist and are exercised; nothing fires them at 10:45 / 15:15 yet (a Beat
  entry cannot — the sweeps need the desk's gateway — so it is a launchd/cron POST or a desk
  timer, SW11's, with `SWING_ORDER_OPEN_AFTER_CUTOFF` and `SWING_GTT_MISSING_AT_1515`).
- **The journal card still says `reads: SIMULATED`** while the flag is false
  (`baskfy_api.swing_journal`, leaf 1.3.4's file this session); the ladder reads real closes
  regardless (A10). One keyword for SW11, and MD8′'s "paper sessions" wording with it.
- **`sw_position.half_risk` is a column only.** The journal's tag on the page is SW11's (the
  journal module is 1.3.4's).
- **The web hub has no "Still watching" control and does not show `focus`/`score`.** The API
  carries them; `05` §2 is the spec. The desk page sorts focus first and shows the pending row.
- **A live gap's provisional score is out of 70** and a 72-score flag always outranks it
  (SW10.5.1). If the funnel should let a strong gap crowd out a flag, the score's denominator
  is one line.
- **"Top 20" is per evening** (SW10.5.5): the list can hold more than twenty flags across
  evenings; nothing retires a row because a better one arrived.
- **The marketable limit's ADR comes from the context's detection row / watch row**; a BUY
  line for a name with neither (a hand-added row with no detection) is refused
  `SIZE_REFUSED` at the confirm gate before the limit is ever computed, as SW9.5.2 says.
- **`execute_line` polls only a live order.** A dry-run confirm never consults the order
  source (asserted); a live confirm with no Kite session answers `SENT` without polling.
- **The drill's late partial fill starts from a state the drill writes** (SW10.5.6): the line
  is marked `SENT` by hand, because the dry-run gateway fills whole. Everything after that —
  the handler, the GTT for the filled quantity, the re-sized confirm against the resting
  remainder, the cancel — is the production path over the real gateway.
- **`write_market_row` still takes `execution_enabled`** (unused since A10) so its callers in
  `celery_tasks` / `orchestrator` are untouched; SW11 may drop it.
- **No `hypothesis` on the desk** (as SW10); the new desk cases are exact.

---

## Not done (kept loud)

- **SW10 onward.** The desk page and `/swing/execute` are in (SW7, both halves) and write
  `sw_position` / `sw_fill` on confirm; the ladder write-back, `GET /swing/journal` and the
  journal page are in (SW8, both halves); the backtest engine, its runner, table, CLI and card
  are in (SW9, both halves) — but **no run over real bars has been stored**: the journal's
  backtest card says "not run yet" until `tools/swing/backtest.py` is run against a backfilled
  database. No goldens, no safety proof beyond each module's own tests.
- **`sw_position` has been written only by tests** (SW7). No confirm has run against a real
  database; the book is empty there, and every position test still builds its rows directly.
- **The dev database is at `0026`.** Nothing swing-shaped has run against real NSE bars; the ten
  sessions of 180 instruments it holds cannot feed the detectors' 200-session lookback anyway
  (SW3.3).
- No live morning has run the premarket scan or the monitor (SW6, above).
- ~~`sw_config.first_live_sessions_left` / `risk_multiplier` (`02` §3.5) has no core function
  yet~~ — closed by SW10.5 (A9): `plan.first_live_multiplier` / `build_entries(risk_multiplier=)`
  size at plan time; the evening counts the sessions down.
- `sw_config.exposure_level` is written back by the evening job (SW8), but the detection job's
  Saturday re-scan can still overwrite a settled market row one rung too high (SW8, "did NOT do").
- The watchlist page is read-only; the API's three writes have no form yet.
- Deferred to SW11: the Playwright check and p95 for `/swing/setups`; a tick journal for the
  replay harness. Deferred to SW12: mutation survivors not individually justified.
