# FO run — live status

Updated at the end of every module, and loud about what is NOT done. A fresh session resumes from
the first module not marked ✅.

**Run state: FO0–FO12 ✅ (25 Sep 2026). Deployed `7c1124b`; the scan and the monitor are on on the box (M.4), every FO money flag false. `FO-FINAL-REPORT.md` at the root says what is true.** Branch `developer`. The OP run is complete (OP15 ✅; OP-M.1 turned the options monitor on).

## What exists already (built with the pack, 23 Sep 2026)

| Item | State |
|---|---|
| `NSEProvider.fo_bhavcopy(on)` + `FO_BHAVCOPY_SCHEMA` + `KIND_FO_BHAVCOPY` | ✅ in `packages/providers` (uncommitted with the pack). 7 tests in `test_nse_fo_bhavcopy.py`; `test_nse.py` green; ruff, format and mypy clean; escape-hatch scan green |
| Providers suite | Green except 6 tests in `test_kite_lanes.py` that need a live Redis (none on the dev Mac). They fail the same way without this change, and the change does not touch Kite |
| Research (`RESEARCH.md`) | ✅ 1,162 sessions, 2022-01-03 → 2026-09-22, fetched through the provider at the NSE limiter (1 req/s), in ~90 minutes. The raw zips are in the session scratchpad, **not in the repo**; FO2's backfill re-fetches them into the archive |
| Research scripts | `evidence/research/*.py`, run with the decile-blueprint venv from a directory holding `days/` (see `fetch.py`). They are scratch-quality: FO1/FO9 port them into `baskfy_core.fno.research` with tests |

## Module ledger

| Module | State | One line |
|---|---|---|
| FO0 — Baseline and facts | ✅ | Baseline recorded; B4 N=15 and F2 reproduce to the digit; box reads the F&O bhavcopy, the ban list and BANKNIFTY's master (below) |
| FO1 — Pure core `baskfy_core.fno` | ✅ | Every `04` number a pure function (211 tests); `covered` property-tested over every prefix/partial fill; the research port reproduces B4 trade by trade and F2 (golden runs with `BASKFY_FNO_RESEARCH_DIR`) |
| FO2 — Schema, nightly ingest, backfill, widened master | ✅ | `0052_fno` (15 `fo_` tables + `fo_ingest_day`), `fo_ban_list`, the 18:30–23:30 ingest behind `BASKFY_FNO_SCAN_ENABLED`, the resumable backfill CLI with `--seed-archive`, the seed (F1 ₹25 L after M.2, F2 ₹0), the master widened with O-sleeve reads proved NIFTY-only. `fo_underlying_daily` derived each ingested night, levels anchored to the settle (FO2.8). **The backfill has not run** on the box (FO12) |
| FO3 — 15:00 spread sample | ✅ | `baskfy.fno.spread_sample` at 15:00 behind `BASKFY_FNO_SCAN_ENABLED`: one Kite quote (448 keys) over ATM ± 3 of the top 30 names + NIFTY/BANKNIFTY into `fo_spread_sample`; the median half-spread statistic for FO9; the forward results calendar (no table yet, FO3.6). **Not run on the box** |
| FO4 — Nightly scan | ✅ | `baskfy.fno.scan` (chained in `run_night` after each INGESTED night, in a savepoint; `fno_cli scan --date`; a flagged Celery task, no Beat): `fo_scan` per user for F1N/F1B (NOT_ENTRY_DAY with the next entry, the bhavcopy condor on the entry session, SKIPPED_EVENT, PAUSED, OPEN_POSITION, NO_DATA, REJECTED_*) and F2 (universe rows, CANDIDATE with stop/GTT/R/lots/cost, BLOCKED_BAN/REGIME/CAPACITY, NO_SIGNAL). Pure half `baskfy_core.fno.scan`. No results table (FO4.6). **Not run on the box** |
| FO5 — API + `/options/overnight`, `/options/fno` | ✅ | `GET /fno/overnight`, `/fno/info`, `GET|PATCH /fno/config` (money-free, audited, under the ceilings), every other verb 405; the Overnight and Stock F&O pages with `05`'s clock labels and banners, the Options sub-nav; root CLAUDE.md's clock table gained the two rows. Committed after the overlap session's OV1/OV2, per Maulik. **No browser check yet**; the paper tally reads FO10's checklist later |
| FO6 — Gateway: NRML branch, covered-overnight guard, `fno_gates()` | ✅ | NRML on NFO only with `OPTIONS_ENABLED` + `BASKFY_FNO_CARRY_ENABLED` + a `fo_plan` reference; `assert_overnight_option_is_covered` fail-closed (no rule wired → refused); F2 stock-future GTT only, option GTTs refused always; `fno_gates` four-flag AND; desk flags all false. Orders without `fo_plan` unchanged byte for byte. **Nothing calls it yet** (FO8); GTT modify for the trail is FO7's |
| FO7 — Desk `fno_monitor` | ✅ | `app/fno_monitor.py` behind `BASKFY_FNO_MONITOR_ENABLED`: F1 09:20 plans re-priced live (margin ceiling, cost share), 60 s profit-take/loss-close, E−1 15:00 exit, `LATE_EXIT`; F2 plans, same-session GTT, evening trail via a never-lower GTT modify, E−1 roll, 40-session exit, `NAKED_FUTURE`; nightly `fo_mark`; the executor FO8 calls (longs first, `ABANDONED_PARTIAL`); 19 replays on Postgres; spy broker 0 calls. The FO gateway is locked to dry run: all four flags on gives `LIVE_NOT_BUILT` (FO7.1) |
| FO8 — Desk `/fno` + paper execute | ✅ | `/fno` page (morning plan card, open structures, F2 candidates and futures with GTT state), the `F&O Overnight` nav tab with its badge, `POST /fno/execute` (confirm + unexpired plan, one send per plan, refusals by name) calling FO7's executor through the gateway's dry-run branch; 23 tests, spy broker 0 calls even with `DRY_RUN=false`. **FO8.5 wording is Maulik's**; no browser check yet |
| FO9 — Re-test engine + golden | ✅ | `baskfy_core.fno.retest`: 15 families from `RESEARCH.md`'s table, panels rebuilt exactly from the day files, **every family reproduces** (golden gate with `BASKFY_FNO_RESEARCH_DIR`); worker pages options a batch of symbols at a time (2.8 GB → 1.6 GB peak, FO9.3); quarterly Beat on the compute queue, `fno_cli retest`; measured slippage once FO3 has 20 sessions. **Not run on the box** (needs the backfill) |
| FO10 — Journal, ledger, pauses | ✅ | Pure `baskfy_core.fno.journal`/`ledger`/`checklist`; the desk's `app/fno_ledger.py` writes a `fo_journal` row at every close (profit take, loss close, hard exit, `LATE_EXIT`, stop, time exit, `NAKED_FUTURE`, `ROLL_INCOMPLETE`, abandoned entries via a closed position — FO10.4), rolls itemised inside the row (FO10.3), then `04` §7 per `(sleeve, simulated)` (FO10.1; F1 until lifted, FO10.2; ₹0 = ₹75,000, FO10.5) after each close and nightly; the monitor writes `REJECTED_PAUSED` (FO10.7); the scan reads paper only. Readers for FO5 in `baskfy_worker.fno.ledger` (`read_ledger`, `read_pauses`, `read_checklist`, `lift_f1_pause`); `FNO_WEEKLY` dark (FO10.8). No migration. **FO5's API does not read them yet** |
| FO11 — Gating and safety proof | ✅ | `packages/core/tests/test_fno_safety_proof.py` (Hypothesis never-naked by `covered.uncovered` over fill/refusal scripts, exits close exactly the open legs, no F1 past E−1 15:00 and no F2 past its roll over random calendars; scans: no `FNO*AUTO` name anywhere incl. compose/env files, no route to the gateway in `apps/web`/`services/api` (FO5 files included), no `place_order` outside `packages/execution`, `frozen/` clean, nothing schedules `/fno/execute`); `kite-momentum-rebalancer/tests/test_fno_safety.py` (16 rows × 2 `INTRADAY_ENABLED` per sleeve at the gateway with a spy, the LIVE row the only one an unpinned gateway would send; 48 rows through the real route/monitor/exits on Postgres, LIVE row `LIVE_NOT_BUILT`; `INTRADAY_ENABLED` never admits MIS/CNC/unreferenced NRML; Hypothesis fuzz over `POST /fno/execute`); `tools/fno/drill.py` prints `sleeve=F1N confirms=1 fills=8 orders_to_broker=0`, F1B the same, F2 `confirms=1 fills=4 orders_to_broker=0` with GTT, trail, roll and time exit. OP13's caller-list test was red since FO8 and now lists both books (FO11.3). FO11.1–FO11.6 |
| FO12 — Hardening, deploy, report | ✅ | compose `fno-monitor` with every FO money flag pinned false, `verify-fno.sh`, five alerts; deployed `7c1124b`, **FNO OK** on the box; seed F1 ₹25 L / F2 ₹0; scan + monitor on (M.4); `FO-FINAL-REPORT.md` |
| F3-0 — F3 designed (M.5) | ✅ | The directional index credit spread as sleeve F3 (Maulik's method and three answers, 28 Sep 2026): 01 §1c, 03 §9, 04 §11, 06, QUESTIONS Q10–Q12; root CLAUDE.md's third auto-execute exception (the exit flag) |
| F3-1 — Pure core `fno.directional` + `F3Config` | ✅ | Levels, direction, the 75-minute confirm, the intraday check, strikes, expiry, sizing, add, exits (49 tests); F3N/F3B, group F3, CREDIT_SPREAD, ADD; `BASKFY_FNO_F3_EXECUTION_ENABLED` in the gating |
| F3-2 — Migration 0059, `fo_index_daily`, NIFTY BANK minutes | ✅ | Enum labels in an autocommit block; the index history from Kite (`fno_cli index-daily`, Beat 18:15); the F3 seed at ₹0. **The index history has not been backfilled on the box** (F3-6) |
| F3-3 — EOD re-test | ✅ | `fno.directional_retest` over the 1,165 archived bhavcopies: after costs NIFTY n=206 −0.021R, BANKNIFTY n=166 −0.020R; the four intraday rules untested and named (`evidence/f3-retest.md`). Not in the quarterly families |
| F3-4 — Evening scan | ✅ | `fno.f3_scan` on the nightly `run_scan`: one row per underlying in 04 §8's states, the candidate with levels, confirm, expiry, legs, credit, max loss, lots (6 tests) |
| F3-5 — Desk | ✅ | The 09:20 plan, the minute watch, the exit held for the click or sent under `BASKFY_FNO_F3_AUTO_EXIT`, the add, the nightly mark, `/fno`'s F3 blocks; the guard admits F3N/F3B (17 replay tests). **No web card** |
| F3-7 — Web card + quarterly re-test | ✅ built, 🟡 not deployed | `/options/overnight`'s read-only F3 section (`GET /fno/overnight` `f3`); `F3N`/`F3B` in `retest.FAMILIES` on a raw loader with the weeklies (first run 3 Oct 2026 06:00 once deployed). DB-backed worker/API tests and the deploy not run from the cloud session (DECISIONS-FO F3-7) |

States: ⬜ not started · 🔄 in progress · ✅ green · ⛔ blocked · 🟡 partial · ⛁ data-blocked.

## FO0 — the baseline (24 Sep 2026, ~23:50 IST)

**Tree.** Branch `developer`, HEAD `bcefb12` (OP-M.1) at FO0's start. Nobody else's dirty files
except `holdings-status/` (Maulik's, untracked, not touched). Alembic head `0051_op_position_extremes`
(repo and box). Beat: **54 entries**, nine of them `options-*`; no `fno-*` entry yet.

**Flags on the box** (`/opt/baskfy/.env.staging.compose` and `.env.staging`, booleans only):
`OPTIONS_ENABLED=false`, `INTRADAY_ENABLED=false`, desk `BASKFY_DESK_DRY_RUN=false` (TW18),
`BASKFY_OPTIONS_{MONITOR,COLLECT,SCAN}_ENABLED=true`, every options `…_EXECUTION_ENABLED` unset
(false), swing and TWT execution/auto-execute true (Maulik's, SW25/TW18), `BASKFY_PUBLIC_API_ENABLED=false`.
**No `BASKFY_FNO_*` variable exists anywhere.** The OP run is finished (OP15 ✅).

**Facts `04` depends on, verified:**

| | Fact | Source, date |
|---|---|---|
| a | Zerodha's physical-delivery margin ramp for stock F&O. **Long ITM options:** E−4 10 %, E−3 25 %, E−2 45 % of VaR+ELM+adhoc; E−1 25 % of contract value; expiry day 50 % (ITM) / 25 % (OTM) of contract value; applies if an OTM position turns ITM. **Futures and short options:** on expiry day 50 % of contract value or 1.5 × NRML, whichever is lower. Zerodha may square off an unmet obligation (₹50 + GST). Reported, not asserted (07 §3); it supports PACK.4's E−1 exit | support.zerodha.com `…/policy-on-physical-settlement`, read 24 Sep 2026 |
| b | BANKNIFTY monthlies in Kite's NFO master: **2026-09-29 Tue, 2026-10-27 Tue, 2026-11-23 Mon** (the Tuesday is a holiday, so the expiry moves back a session), **2026-12-29 Tue**; lot **30** on all four; strike step **100** (500 on the outer strikes; December lists only 27 contracts so far, step 1,500). `calendar` must read the expiry from the master, never infer a weekday | box worker, `option_contracts("BANKNIFTY")`, 24 Sep 2026 |
| c | The box reads the F&O bhavcopy through `NSEProvider.fo_bhavcopy`: 2026-09-24, **36,933 rows, 216 underlyings**, archived by the provider on the way | box worker, 24 Sep 2026 |
| d | The ban list: `https://nsearchives.nseindia.com/content/fo/fo_secban.csv`, text `Securities in Ban For Trade Date 25-SEP-2026:` then `n,SYMBOL` lines (KAYNES, LICHSGFIN, MANAPPURAM, SAIL). Read through the NSE provider's limiter and client. **The provider has no public method for it yet**; the probe used its fetch path, and FO2 adds `NSEProvider.fo_ban_list(for_session)` with a wrong-date refusal like SW16's | box worker, 24 Sep 2026 |

**The research reproduces** (stored data, `evidence/research/*.py`, decile-blueprint venv):
B4 N=15 **n = 100, +0.033R** (t = 1.36, 86 % win), with `LOSS_MULT=1.5` **n = 100, +0.022R**
(t = 1.01, worst −0.73R); F2's spec **n = 2,334, +0.017R** (t = 0.71, 0.96 rolls a trade). Exactly
the pack's numbers.

**Where the research data is.** The 1,232 day files, the raw archive and the derived parquets
(1.9 GB) were in a session scratchpad under `/tmp` and are now at **`~/baskfy-research/fno`** on the
Mac, outside the repo. FO9's golden test reads from `BASKFY_FNO_RESEARCH_DIR` and skips loudly
without it; FO2's backfill can seed the archive from there instead of re-fetching.

## Paper period (`04` §9); starts only after FO12 deploys

| Sleeve | Needed | Done | Opened |
|---|---|---|---|
| F1N (NIFTY) | 6 consecutive monthly cycles | 0 | 0 |
| F1B (BANKNIFTY) | 6 consecutive monthly cycles | 0 | 0 |
| F1 both together | ≥ 4 of the 12 structures actually opened | — | 0 |
| F2 | 60 sessions, ≥ 15 positions closed, ≥ 3 rolls | 0 | 0 |

## What is NOT done, and must not be forgotten

* **No F&O trading code exists.** No schema, no scan, no guard change, no desk page. The pack is
  design plus one provider read.
* **The ban list has no provider method** (FO0 d). Until FO2 adds one, nothing can check Track C §9.
* Q1, Q2 and Q4 are answered (M.1). **Neither sleeve meets the real-money gate's Tier 2E item
  (`02` §3.4) as tested**: F1 with the loss close is −0.010R in 2022, and F2 is negative in four of
  five years. A flag needs Maulik's written waiver or forward evidence.
* F2's live sizing will refuse most names under the ₹25,000 per-trade ceiling (`04` §10), and its
  capital is ₹0.
* The research's slippage for stock options (3 %) is **assumed**. FO3 measures it.
* The research's option marks are closes, not fills (`07` §2), and its lot sizes before
  8 Jul 2024 are approximations.
* **F1's +0.033R is not significant** (t = 1.36, best of three entry days). Paper exists to test
  it, and the real-money gate (`02` §3) is not met by the research alone.
