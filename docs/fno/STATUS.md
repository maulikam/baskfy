# FO run — live status

Updated at the end of every module, and loud about what is NOT done. A fresh session resumes from
the first module not marked ✅.

**Run state: FO0 ✅, FO1 ✅, FO2 ✅ (25 Sep 2026); FO3 in progress.** Branch `developer`. The OP run is complete (OP15 ✅,
deployed `acac17c`; the options monitor flag was turned on at Maulik's answer, DECISIONS-OP OP-M.1).

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
| FO2 — Schema, nightly ingest, backfill, widened master | ✅ | `0052_fno` (15 `fo_` tables + `fo_ingest_day`), `fo_ban_list`, the 18:30–23:30 ingest behind `BASKFY_FNO_SCAN_ENABLED`, the resumable backfill CLI with `--seed-archive`, the seed (F1 ₹10 L, F2 ₹0), the master widened with O-sleeve reads proved NIFTY-only. `fo_underlying_daily` derived each ingested night, levels anchored to the settle (FO2.8). **The backfill has not run** on the box (FO12) |
| FO3 — 15:00 spread sample | ⬜ | |
| FO4 — Nightly scan | ⬜ | |
| FO5 — API + `/options/overnight`, `/options/fno` | ⬜ | |
| FO6 — Gateway: NRML branch, covered-overnight guard, `fno_gates()` | ⬜ | |
| FO7 — Desk `fno_monitor` | ⬜ | |
| FO8 — Desk `/fno` + paper execute | ⬜ | |
| FO9 — Re-test engine + golden | ⬜ | |
| FO10 — Journal, ledger, pauses | ⬜ | |
| FO11 — Gating and safety proof | ⬜ | |
| FO12 — Hardening, deploy, report | ⬜ | |

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
