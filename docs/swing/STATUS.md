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
| SW3 — Daily detection job | ⬜ | |
| SW4 — API + Setups/Market pages | ⬜ | |
| SW5 — Watchlist, plan preview, EOD, alert | ⬜ | |
| SW6 — Premarket EP scan + opening-range monitor | ⬜ | |
| SW7 — Desk page + `/swing/execute` (DRY_RUN) | ⬜ | |
| SW8 — Journal + ladder closes the loop | ⬜ | |
| SW9 — EOD backtest | ⬜ | |
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

## Not done (kept loud)

- **Everything from SW1 on.** No migration applied to the dev database, no worker task, no
  router, no page, no desk route, no monitor, no backtest, no goldens.
- The core's flag score does not yet include the `+5/+5` (young listing / hot sector) of `04`
  §2.6 — that is SW3's, because it needs `instrument.listed_on` and `index_member_daily`, which
  core must not read.
- `sw_config.first_live_sessions_left` / `risk_multiplier` (`02` §3.5) has no core function yet;
  SW7 multiplies `risk_per_trade_pct` before calling `size_position`.
- The dev database holds ten sessions of bars for 180 instruments and **no factor rows**. Every
  "on the dev stack" acceptance criterion in SW3–SW5 has to reckon with that.
