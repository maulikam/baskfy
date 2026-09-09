# OC run — live status

The status page for the condor run. Updated at the end of every module, loud about what is NOT
done. A fresh session resumes from the first module not marked ✅.

**Run state: not started.** Pack written 9 Sep 2026 on branch `developer`. Nothing under
`packages/core/src/baskfy_core/condor/` exists yet — unlike the swing pack, this one ships no
pre-built core; OC1 writes it from `04`.

## Module ledger

| Module | State | One line |
|---|---|---|
| OC0 — Baseline, read-in, verified facts | ⬜ | |
| OC1 — The pure core `baskfy_core.condor` | ⬜ | |
| OC2 — Schema, NFO master, settings, `condor_gates()` | ⬜ | |
| OC3 — Minute bars, quotes with depth, margins, the collector | ⬜ | |
| OC4 — API + the web hub | ⬜ | |
| OC5 — Plan builder, costs verified, verdict alert | ⬜ | |
| OC6 — Desk process: observation, verdict, exit engine | ⬜ | |
| OC7 — Desk page + `/condor/execute` (DRY_RUN) | ⬜ | |
| OC8 — Journal, ledger, pauses, first-live multiplier | ⬜ | |
| OC9 — Backtest, three tiers | ⬜ | |
| OC10 — Gating and safety proof | ⬜ | |
| OC11 — Hardening and observability | ⬜ | |
| OC12 — Verification, goldens, deploy notes, final report | ⬜ | |

States: ⬜ not started · 🔄 in progress · ✅ green · ⛔ blocked · 🟡 partial.

---

## OC0 — Baseline, read-in, verified facts

*(written by the run)*

## What is NOT done

Everything. The paper period (six monthly expiries, `02` §3.2) begins only after OC12 deploys
the process to the box, and the earliest it can complete is six months after that.
