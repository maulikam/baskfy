# The kickoff prompt — the VB run

Paste the block below into a fresh CLI session at the repo root (`~/Documents/projects/baskfy`).
Nothing else is needed; the session bootstraps itself from the repo. Unlike the SW and OC runs
there is no docs pack yet — VB0 writes it, in the shape of `docs/swing/`, from
`research/volume-breakout/STRATEGY.md`, and only then does code start. The run is resumable: a
fresh session reads `docs/vbt/STATUS.md` and continues from the first module not marked ✅.

---

```
This is the VB run: build VBT-1 — the volume-breakout strategy researched in
research/volume-breakout/STRATEGY.md — into Baskfy as a third sleeve beside the
weekly momentum book and the swing book, so the nightly chain scans for it, the
desk plans it, and Maulik confirms every order. Paper-proven end to end, every
execution flag off.

Read, in this order, before writing any code:
1. CLAUDE.md (the root working agreement — it governs this run unchanged)
2. docs/README.md
3. research/volume-breakout/STRATEGY.md   ← the strategy: rules, gate, entry,
   exits, sizing, the numbers, and the caveats. It is the spec.
4. docs/swing/README.md, 02-scope-and-gating.md, 03-data-model.md,
   04-business-rules.md, 06-module-plan.md  ← the sibling sleeve; copy its
   shape, its gating and its discipline, not its rules
5. research/volume-breakout/vbt/{scan,sim,data}.py  ← the research code. It is
   the reference implementation of the rules and the backtest, not code to
   import: it is float, pandas-based and knows nothing about law 1.

VB0 — the pack, before any code. Write docs/vbt/ mirroring docs/swing/: README,
01-method (from STRATEGY §2–3, cite the tables), 02-scope-and-gating (Track
A/B/C exactly as the swing pack draws them; the §3 real-money gate verbatim:
20 DRY_RUN sessions, backtest on the page, written risk decision, half-risk
first live sessions), 03-data-model (vb_* tables, next free migration number),
04-business-rules (every threshold of VBT-1 as a named config field with its
value: the five Chartink lines, the six trend filters, breadth gate 40%,
limit entry valid 3 sessions, 12% stop, 21-EMA exit, 10 slots, 3 new/session,
1% of 20-day turnover, 25 bps/side; tests pin these literally), 05-ui-spec,
06-module-plan VB1–VB10 with acceptance criteria that are tests, STATUS.md,
DECISIONS-VB.md, QUESTIONS.md (standing defaults for anything only Maulik can
answer). Commit it as VB0 and continue without waiting for review.

The modules, in outline — 06 fixes the detail:
- VB1  pure core: packages/core/src/baskfy_core/vbt/ — config, indicators
  (50-day volume SMA, 200-DMA, prior-20-day high, 20-day return, close
  position, 20-day turnover, 21-EMA), the signal, the breadth series (% of the
  tradable universe above its 200-DMA), sizing, and the exit rules.
  DataFrames in, DataFrames out; Decimal money; test_vbt_purity.py green.
- VB2  goldens: the core must reproduce research/volume-breakout/out/
  final_trades.csv (761 trades) and final_metrics.json (CAGR 18.2%, max DD
  −27.9%) from the same bars to the tick; where it cannot, the difference is
  explained in DECISIONS-VB.md, never papered over. Reproduce the data-plant
  findings in STRATEGY §1 as tests: the six thin sessions are excluded from
  every rolling window; a 10% missing-bar tolerance inside a window.
- VB3  data model + migration: vb_signal_daily, vb_breadth_daily, vb_order
  (working limit orders with a 3-session expiry), vb_position, vb_backtest_run
  (append-only, like sw_backtest_run).
- VB4  nightly job compute_vbt after compute_swing, and the 21:00 IST retry;
  neither can fail the night. Published-session clock only (CLAUDE.md "which
  date the product shows"): signals and breadth come from the last completed
  session, never from a half-finished day.
- VB5  the sleeve's own cash and book; never sizes against the whole account,
  never sells a holding it did not buy; the 1%-of-turnover cap and the
  min-trade floor from 04.
- VB6  desk plan: /analyze produces the sleeve's limit entries (signal close,
  three-session expiry) and its GTT stops; /execute with confirm=true and the
  plan_id, plans expire in 30 minutes, DRY_RUN simulates end to end. The
  EMA-exit sells are next-open orders in the morning plan. Everything through
  OrderGateway — guards → risk → rate-limit → journal; client_id = plan_id:symbol.
- VB7  the working-order expiry: the one thing the desk lacks today. Model it
  on the swing book's pending-entry machinery; a limit that has not filled by
  the third session's close is cancelled in that evening's job.
- VB8  desk page + web page (read-only): today's candidates, the breadth gauge
  and whether the gate is open, the working orders, the book, the backtest
  numbers with STRATEGY §5's caveats as a component, not a footer.
- VB9  the backtest on the page, re-run from the plant's bars, appended to
  vb_backtest_run; the page shows the latest run beside the research numbers
  and flags a drift of more than 1 CAGR point.
- VB10 safety: with BASKFY_VBT_EXECUTION_ENABLED=false no VBT line can reach
  OrderGateway.place; the web app has no order route; no auto-execute flag
  exists for this sleeve and none is added (non-negotiable #1's exception is
  the swing sleeve's alone); the desk's weekly book and R1–R4 are untouched;
  the swing book still runs on any morning.

Execute VB0 through VB10 in order under the Autonomy charter in CLAUDE.md: run
long, decide-record-continue, questions to Maulik are the exception.
Specifically:
- One commit per module: "VB<N>: green — <one line>". No module ends with either
  tree's test suite broken; the desk must be able to rebalance on any Friday.
- Never place a live order. DRY_RUN=true in every environment you create.
  BASKFY_VBT_EXECUTION_ENABLED stays false; you never flip it.
- Track C is forbidden: no shorting, no MIS/F&O, no margin, no auto-execution,
  no web-app orders, no changes to the swing rules or the weekly book.
- Every threshold is a field of baskfy_core.vbt.config, never a literal in a
  detector, task, router or page. Tests assert docs/vbt/04, never current
  behaviour; never weaken a test to pass a module.
- When the code and a doc disagree, the decision wins (CLAUDE.md, 9 Sep 2026):
  git log -S before "fixing" anything, fix the stale half, pin to the commit.
- Judgement calls go in docs/vbt/DECISIONS-VB.md, numbered by module, tagged
  ⚠ UNREVIEWED, with the rejected alternatives and the reversal path.
- Anything only Maulik's hands can supply goes to NEEDS-MAULIK.md at the root
  under a "VBT" heading; keep working on everything not dependent on it. Two
  are known already and go there in VB0: the 262 missing instrument-days and
  the six thin sessions in STRATEGY §1 are the plant's to fill, and the sleeve's
  capital and risk per trade are his to set (QUESTIONS.md carries the defaults:
  ₹10 lakh, equal weight, 10 slots).
- Update docs/vbt/STATUS.md at the end of every module — loud about what is NOT
  done. If context runs long, write enough state there for a fresh session to
  resume losslessly, then continue.
- Deploy nothing. The box, the nightly window guard and tools/deploy/ are
  Maulik's; the run ends at a green tree and a report.

Stop only for the charter's stop conditions. When VB10 is green — or a hard
blocker ends the run — write VB-FINAL-REPORT.md at the repo root in the style
of SW-FINAL-REPORT.md: what was built, what was decided, what is NOT done, what
needs Maulik, and the exact steps for the first DRY_RUN morning. Then ask
Maulik to review.
```

---

## If the run is resumed

Same prompt. The session reads `docs/vbt/STATUS.md`, finds the first non-green module, and
continues. A half-built module is finished, not restarted: acceptance criteria are tests, and a
test that already passes is not rewritten.
