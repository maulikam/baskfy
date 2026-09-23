# FO run — live status

Updated at the end of every module, and loud about what is NOT done. A fresh session resumes from
the first module not marked ✅.

**Run state: pack written and research done (23 Sep 2026); FO0 not started.** Branch `developer`.
The OP run is unfinished on the same branch (OP8 next, OP7 uncommitted in the tree when this pack
was written). See `06`, "Coordination with the OP run".

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
| FO0 — Baseline and facts | ⬜ | |
| FO1 — Pure core `baskfy_core.fno` | ⬜ | |
| FO2 — Schema, nightly ingest, backfill, widened master | ⬜ | |
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

## Paper period (`04` §9); starts only after FO12 deploys

| Sleeve | Needed | Done | Opened |
|---|---|---|---|
| F1N (NIFTY) | 6 consecutive monthly cycles | 0 | 0 |
| F1B (BANKNIFTY) | 6 consecutive monthly cycles | 0 | 0 |
| Both together | ≥ 4 of the 12 structures actually opened | — | 0 |

## What is NOT done, and must not be forgotten

* **No F&O trading code exists.** No schema, no scan, no guard change, no desk page. The pack is
  design plus one provider read.
* **QUESTIONS Q1 and Q2 are unanswered.** Q2 (non-negotiable 4 for option legs) blocks any FO
  option order from leaving paper.
* The research's slippage for stock options (3 %) is **assumed**. FO3 measures it.
* The research's option marks are closes, not fills (`07` §2), and its lot sizes before
  8 Jul 2024 are approximations.
* **F1's +0.033R is not significant** (t = 1.36, best of three entry days). Paper exists to test
  it, and the real-money gate (`02` §3) is not met by the research alone.
