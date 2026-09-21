# OP run — live status

The status page for the options run. Updated at the end of every module, loud about what is NOT
done. A fresh session resumes from the first module not marked ✅.

**Run state: not started.** Pack written 22 Sep 2026 on branch `developer`, absorbing the
never-started condor pack (`docs/condor/`) as sleeve O1. Nothing under
`packages/core/src/baskfy_core/options/` exists yet; OP1 writes it from `04`.

## Module ledger

| Module | State | One line |
|---|---|---|
| OP0 — Baseline, read-in, verified facts | ⬜ | |
| OP1 — The shared pure core `baskfy_core.options` | ⬜ | |
| OP2 — Schema, NFO master, settings, `options_gates()` | ⬜ | |
| OP3 — Provider reads, index minute bars, collector, limiter | ⬜ | |
| OP4 — Sleeve signal cores and the scans | ⬜ | |
| OP5 — API and the web Options tab (the scans ship) | ⬜ | |
| OP6 — O1 plan builder (monthly + weekly), costs pinned | ⬜ | |
| OP7 — O2 plan builder | ⬜ | |
| OP8 — O3 plan builder | ⬜ | |
| OP9 — Desk process `options_monitor` | ⬜ | |
| OP10 — Desk page + `/nifty-options/execute` (paper) | ⬜ | |
| OP11 — Journal, ledger, pauses, first-live multiplier | ⬜ | |
| OP12 — Backtests: Tier 1–2 per sleeve; Tier 3 ⛁ | ⬜ | |
| OP13 — Gating and safety proof | ⬜ | |
| OP14 — Hardening and observability | ⬜ | |
| OP15 — Verification, goldens, deploy, final report | ⬜ | |

States: ⬜ not started · 🔄 in progress · ✅ green · ⛔ blocked · 🟡 partial · ⛁ data-blocked.

## Paper periods (`02` §3.2) — start only after OP15 deploys

| Sleeve | Needed | Done | Traded |
|---|---|---|---|
| O1-M | 6 monthly expiries, ≥ 3 traded | 0 | 0 |
| O1-W | 12 weekly expiries, ≥ 6 traded | 0 | 0 |
| O2 | 60 sessions, ≥ 25 traded | 0 | 0 |
| O3 | 20 expiry days, ≥ 8 traded | 0 | 0 |

---

## OP0 — Baseline, read-in, verified facts

*(written by the run)*

## What is NOT done

Everything. Every money flag is false and stays false. The paper periods begin only after OP15, and
the longest (O1-M) takes about six months after that.
