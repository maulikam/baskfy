# VB run — live status

The status page for the volume-breakout run. Updated at the end of every module, **loud about
what is NOT done**. A fresh session resumes from the first module not marked ✅.

**Run state: VB2 green — the core reproduces the study exactly. VB3–VB10 not started.** Started 10 Sep 2026 on branch
`developer`. The report will be `../../VB-FINAL-REPORT.md`; what needs Maulik's hands is
`../../NEEDS-MAULIK.md` § VBT.

## Module ledger

| Module | State | One line |
|---|---|---|
| VB0 — The pack | ✅ | Eight documents, the seven pre-taken decisions, the standing defaults, and the two measurements that settled how the strategy is read |
| VB1 — The pure core | ✅ | Nine modules, 43 named thresholds and **245 tests**; law 1 asserted over the source, and no rule module spells out a number that is not 0, 1, 2 or 100 |
| VB2 — Goldens: reproduce the study | ✅ | **All 761 trades, to the paisa.** 32,929 scan hits, 6,293 signals, CAGR 18.23%, drawdown −27.94%, the yearly table to one decimal, and eleven of twelve neighbourhood cases to a tenth of a point |
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

---

## VB1 — The pure core ✅ (10 Sep 2026)

`decile-blueprint/packages/core/src/baskfy_core/vbt/` — nine modules, DataFrames and dataclasses
in, DataFrames and dataclasses out.

| Module | What it holds |
|---|---|
| `config.py` | Eight frozen dataclasses, **43 fields**, every one with the reason for its value |
| `calendar.py` | The thin-session rule and session counting — a holiday consumes no session |
| `indicators.py` | The eleven per-bar columns, computed on a **densified** frame so a window counts sessions rather than rows |
| `signals.py` | Chartink's five lines, the six trend filters, `SIGNAL` / `SCAN_ONLY` and `failed_filters` |
| `breadth.py` | The share above the 200-day average, and the two-valued gate |
| `sizing.py` | Ten equal slots, four budgets, the cap that bound named |
| `exits.py` | The 12% stop from the fill, the 21-EMA queue, the precedence |
| `orders.py` | The limit that works three **sessions**, its expiry and its fill model |
| `plan.py` | `build_entries` in `04` §9.1's order, `exit_lines`, `assemble`, `plan_hash` |

**245 tests, all green**, in ten files. The ones that are load-bearing rather than routine:

* `test_vbt_purity.py` — law 1 over the source: no database, network, disk or clock import, no
  `baskfy_execution`, no `kite_client`, **no import of `baskfy_core.swing`**, and no setting whose
  name contains `AUTO_EXECUTE` (`02` Track C §3).
* `test_vbt_no_literals.py` — over the **syntax tree**, not the text: a rule module may spell out
  0, 1, 2 and 100 and nothing else. Every threshold is a field.
* `test_vbt_docs_parity.py` — `04` §12's table is regenerated from the code and compared **both
  ways**; a field added, renamed or re-valued without a doc edit is red, and so is a row with no
  field behind it.
* `test_vbt_plan.py::test_every_skip_reason_is_reachable_from_the_plan` — a reason nothing can
  produce is a reason a page will never explain. One is deliberately unreachable from a signal
  (`STOP_NOT_BELOW_ENTRY`) and the test says why in its own message.

### Two things settled while writing it

* **`# type: ignore` is not available** (house rule 3, enforced by `test_no_escape_hatches.py`
  across the tree). Two helpers exist because of it: `number()` in the signal tests and `at()` in
  the indicator tests, each of which asserts the cell's type rather than silencing the checker.
* **A bar exactly on 6.5% is a floating-point boundary and is deliberately not pinned.**
  `(95.85 / 90 - 1) x 100` is `6.499999999999995` in IEEE 754 — the research's arithmetic produces
  the same value, so the two agree about the edge whichever way it falls. What the test pins is
  the **sense** of the comparison.

### What is NOT done at VB1

* **Nothing has run against real bars.** The next module is the one that matters: VB2 reproduces
  the study's 761 trades and 18.2% CAGR, or explains why it cannot.
* No `backtest.py` yet — the engine of `04` §11 is VB2's.
* No migration, no task, no router, no page, no desk route. `vb_config` does not exist.
* The mutation harness (`make mutants`) does not yet include `baskfy_core.vbt`.

---

## VB2 — The goldens ✅ (10 Sep 2026)

**The core is the study.** `baskfy_core.vbt.backtest` runs `04` §11's sequencing over the VB1
functions and, against `research/volume-breakout/aws/`, produces:

| | The study | The core |
|---|---|---|
| raw Chartink scan | 32,929 | **32,929** |
| VBT-1 signals | 6,293 | **6,293** |
| trades | 761 | **761**, every one matching on symbol, entry, exit, quantity and reason |
| worst price gap | | **₹0.0000** |
| CAGR | 18.23% | **18.23%** |
| max drawdown | −27.94% | **−27.94%** |
| win rate / profit factor | 37.8% / 1.55 | **37.84% / 1.55** |
| exits | 688 EMA · 62 stop · 1 no-bar · 10 end-of-run | **identical** |
| yearly table (`01` §4) | +17.3 −19.3 −3.8 +20.4 +45.6 +16.1 +64.7 +35.1 +1.2 +6.3 | **identical to one decimal** |

The neighbourhood reproduces too: entry window 2/3/5 → 11.4 / 18.2 / 17.1; stop 10/12/15 →
16.4 / 18.2 / 16.6; slots 8/10/15 → 15.5 / 18.2 / 12.9; gate 30/40/45/50 → 15.6 / 18.2 / 15.4 /
10.9; no gate → 18.5% at −48.8%; the raw scan traded the same way → 0.8%. **The one case that
does not is a 35% gate, by 0.2 points**, and DECISIONS-VB VB2.2 measures exactly why.

### What was written

| | |
|---|---|
| `packages/core/src/baskfy_core/vbt/backtest.py` | the `Panel`, the sequencing, the statistics, the yearly table |
| `tools/vbt/research_panel.py` | the export loader — **the same one VB9's plant run will use**, so a difference between the two runs is a difference in the data and never in the loader |
| `tools/vbt/reproduce.py` | the comparison as a command; exits 0 on PASS, and prints where it does not |
| `packages/core/tests/vbt_backtest_fixtures.py` | a planted trade with its arithmetic worked by hand in the docstring |
| `packages/core/tests/test_vbt_backtest.py` | 22 tests over that trade — runs everywhere, no export needed |
| `packages/core/tests/test_vbt_goldens.py` | 21 tests over the real bars; **skips loudly** when the 68 MB export is absent |

### The one thing that had to be found

The first full run matched 172 of 761 trades. The cause was `limit × 1` in the fill test: a bar
price from a `float` carries ~50 significant digits, Decimal rounds a product to 28, and a limit
that a low touched *exactly* came back a hair above it. Yesterday's close being today's low is
what a pullback looks like, so the case is common rather than exotic. One branch fixed it
(DECISIONS-VB **VB2.1**), and the lesson is general: an exact comparison against a tape price
must not pass through Decimal arithmetic first.

### What is NOT done at VB2

* **Nothing has touched the plant's own database.** Every number above is from the research
  export, which is gitignored; on a machine without it `test_vbt_goldens.py` skips.
* The reproduction takes **76 seconds** and about 2 GB of memory (a 3,837 × 2,396 dense panel).
  VB9 will need the same on the plant's bars, where the universe is larger.
* No migration, no task, no router, no page, no desk route. `vb_config` still does not exist.
* The **262 missing instrument-days** and the sparse pre-2024 corporate actions are unchanged
  (NEEDS-MAULIK § VBT, V2). Reproducing the study exactly reproduces its data gaps exactly.

### Resume instructions for a fresh session

Read `/CLAUDE.md` → `docs/README.md` → `research/volume-breakout/STRATEGY.md` → `docs/vbt/README.md`
→ `02` → `06`, then start at VB1. `docs/vbt/04-business-rules.md` is the contract the tests
assert; `DECISIONS-VB.md` VB0.2 is the one thing that will otherwise be re-derived from scratch.
