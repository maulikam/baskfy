# VB run — live status

The status page for the volume-breakout run. Updated at the end of every module, **loud about
what is NOT done**. A fresh session resumes from the first module not marked ✅.

**Run state: VB0 green (the pack). VB1–VB10 not started.** Started 10 Sep 2026 on branch
`developer`. The report will be `../../VB-FINAL-REPORT.md`; what needs Maulik's hands is
`../../NEEDS-MAULIK.md` § VBT.

## Module ledger

| Module | State | One line |
|---|---|---|
| VB0 — The pack | ✅ | Eight documents, the seven pre-taken decisions, the standing defaults, and the two measurements that settled how the strategy is read |
| VB1 — The pure core | ⬜ | |
| VB2 — Goldens: reproduce the study | ⬜ | |
| VB3 — Schema and settings | ⬜ | |
| VB4 — The nightly job | ⬜ | |
| VB5 — The sleeve's cash and book | ⬜ | |
| VB6 — Desk plan and `/vbt/execute` | ⬜ | |
| VB7 — The working order and its expiry | ⬜ | |
| VB8 — The pages | ⬜ | |
| VB9 — The backtest on the page | ⬜ | |
| VB10 — Safety, and the claims become theorems | ⬜ | |

States: ⬜ not started · 🔄 in progress · ✅ green · ⛔ blocked · 🟡 partial.

---

## VB0 — The pack ✅ (10 Sep 2026)

Everything below was **measured on this machine on 10 Sep 2026**, not recalled.

### Repository

| | |
|---|---|
| Branch | `developer`, HEAD `c68c57f` ("PF1: green — the allocation ledger learns to split a holding across portfolios") |
| Trees | `decile-blueprint/` (screener + API + worker + web), `kite-momentum-rebalancer/` (the desk), `frozen/strangle/` (untouched) |
| **Another session's staged files** | Nineteen paths of a portfolio-redesign change were **staged but not committed** before this run started (`PORTFOLIO_REDESIGN.md`, the portfolio web components, `0035_split_allocation.py`, `broker_holdings_sync.py`, …). **They are not this run's and are not committed under any VB module.** Every VB commit names its own paths explicitly (`git commit -- <paths>`), which leaves that index untouched |
| Alembic head | `0035_split_allocation` — so **`0036` is the next free number** (VB3 re-checks) |
| Core suite at baseline | `uv run pytest packages/core/tests` — **exit 0**, green before anything was written |
| Research tree | `research/volume-breakout/` was untracked; VB0 commits it minus `aws/` and the pickles (DECISIONS-VB **VB0.1**) |

### The data the study ran on

| | |
|---|---|
| Export | `research/volume-breakout/aws/` — the AWS Phase-A box's tables as of 10 Sep 2026, 68 MB gzipped, **gitignored** (regenerate with `export_bars_aws.sh`) |
| Panel | **4,186 instruments × 2,396 sessions**, 2017-01-02 → 2026-09-09 |
| Thin sessions dropped by the rule | **6**: 2017-10-19, 2018-11-07, 2024-01-20, 2024-03-02, 2024-05-18, 2025-02-01 |
| Raw Chartink scan | **32,929** signals |
| VBT-1 signals (six filters) | **6,293** |
| ETFs in the panel | 349, each with **7–8 bars in nine years** — none ever reaches a 200-DMA (VB0.3) |

### The two measurements that settled how the strategy is read

Both were run against the export before a line of `docs/vbt/04` was written, because both decide
what the numerical contract says.

1. **Six filters, not eight** (DECISIONS-VB **VB0.2**). The six-filter reading of STRATEGY §3
   gives **6,293 signals, 18.23% CAGR, −27.94% drawdown, 761 trades, PF 1.55** — the published
   numbers exactly. `grid2.py`'s eight-filter version gives 6,254 / 17.47% / 765 / 1.51. The note
   is the decision; the script is the stale half.
2. **Breadth is its own series** (DECISIONS-VB **VB0.3**). Recomputed from the export it matches
   `out/breadth200.csv` to **1.1e-16**; excluding ETFs explicitly changes it by **0.0000** and
   flips the 40% gate on **0 of 2,396** sessions.

### What is NOT done, and is not pretended to be

* **No code exists.** `baskfy_core/vbt/` does not exist; no migration, no task, no router, no
  page, no desk route. VB1 starts from a blank file.
* **Nothing has run against the plant's own database.** Every number above is from the research
  export. VB4 is the first module that touches `ohlcv_daily` through the worker.
* **No sleeve capital.** `vb_config` does not exist yet and will be seeded at ₹0 (`02` §3.4).
* **The 262 missing instrument-days and the pre-2024 corporate actions are still missing**
  (STRATEGY §1, §6). Every number in `01` and `04` is against the data as it stands.
* **`BASKFY_VBT_EXECUTION_ENABLED` does not exist yet, and when it does it is false.** No VBT
  order has ever been placed, simulated or otherwise.
* **0 of 20 DRY_RUN sessions** (`02` §3.1).
* Nothing is deployed. The box does not know this sleeve exists.

### Resume instructions for a fresh session

Read `/CLAUDE.md` → `docs/README.md` → `research/volume-breakout/STRATEGY.md` → `docs/vbt/README.md`
→ `02` → `06`, then start at VB1. `docs/vbt/04-business-rules.md` is the contract the tests
assert; `DECISIONS-VB.md` VB0.2 is the one thing that will otherwise be re-derived from scratch.
