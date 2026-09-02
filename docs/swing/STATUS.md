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
| SW8 — Journal + ladder closes the loop | 🔄 | ✅ ladder + API: the evening settles the rung and writes it to `sw_config` (audited, `swing-eod`) and the day's market row, and the plan is built with it; `GET /swing/journal` answers C2's shape — real and simulated cards apart, the six-bucket histogram, by setup, by month, the ladder card, 14-of-20 · (page: see 1.2.2) |
| SW9 — EOD backtest | 🔄 | Core half (1.3.1): `baskfy_core.swing.backtest` runs `04` §11 through the live book's own functions; a planted flag reproduces R = 0.28 to the paisa; 300 × 8y in 24 s; runner and card are 1.3.2's |
| SW10 — Gating and safety proof | ⬜ | |
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

## SW8 — The journal page and the ladder closing the loop 🔄

**This section covers the ladder write-back and `GET /swing/journal` (leaf 1.2.1). The page is
leaf 1.2.2's and is appended below it — (page: see 1.2.2).**

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
- The page — (page: see 1.2.2).

## SW9 — The EOD backtest 🔄

**This section covers the pure engine in `packages/core` (leaf 1.3.1). The runner task, the
`sw_backtest_run` table, the CLI and the journal card are leaf 1.3.2's and are appended below
it — (runner: see 1.3.2).**

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

### Tests — `packages/core/tests/test_swing_backtest.py`, 37 passed

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
gap day's rule); a gap through the stop filling at the open; an entry-day stop-out filling at
the stop; `END_OF_RUN` and `NO_BAR` closes; a same-symbol duplicate entering once; a candidate
on the last session counted, not entered; a Saturday bar never traded; the empty typed frame
running flat; byte-identical JSON across two runs; the frozen contract objects.

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
module is scored against). MUTATION_SCORE_PLACEHOLDER

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

---

## Not done (kept loud)

- **SW8 onward.** The desk page and `/swing/execute` are in (SW7, both halves) and write
  `sw_position` / `sw_fill` on confirm; no journal page, no backtest, no goldens, no safety
  proof beyond each module's own tests. The ladder write-back and `GET /swing/journal` are in
  (SW8, 1.2.1).
- **`sw_position` has been written only by tests** (SW7). No confirm has run against a real
  database; the book is empty there, and every position test still builds its rows directly.
- **The dev database is at `0026`.** Nothing swing-shaped has run against real NSE bars; the ten
  sessions of 180 instruments it holds cannot feed the detectors' 200-session lookback anyway
  (SW3.3).
- No live morning has run the premarket scan or the monitor (SW6, above).
- `sw_config.first_live_sessions_left` / `risk_multiplier` (`02` §3.5) has no core function yet;
  SW7 multiplies `risk_per_trade_pct` before calling `size_position`.
- `sw_config.exposure_level` is written back by the evening job (SW8), but the detection job's
  Saturday re-scan can still overwrite a settled market row one rung too high (SW8, "did NOT do").
- The watchlist page is read-only; the API's three writes have no form yet.
- Deferred to SW11: the Playwright check and p95 for `/swing/setups`; a tick journal for the
  replay harness. Deferred to SW12: mutation survivors not individually justified.
