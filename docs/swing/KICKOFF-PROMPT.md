# The kickoff prompt

Paste the block below into a fresh CLI session at the repo root. Nothing else is needed; the
session bootstraps itself from the repo. The run is resumable: a fresh session reads
`docs/swing/STATUS.md` and continues from the first module not marked ✅.

---

```
This is the SW run: configure Kristjan Kullamägi's swing-trading method into Baskfy
so Maulik can identify setups and take swing trades on NSE through Kite.

Read, in this order, before writing any code:
1. CLAUDE.md (the root working agreement — it governs this run unchanged)
2. docs/README.md
3. docs/swing/README.md
4. docs/swing/02-scope-and-gating.md   ← the law of this run
5. docs/swing/06-module-plan.md        ← the task list
6. docs/swing/04-business-rules.md     ← the numerical contract (tests assert it)
Read docs/swing/01, 03, 05 as the modules cite them.

The pure core already exists and is green: packages/core/src/baskfy_core/swing/
with tests packages/core/tests/test_swing_*.py. SW1 re-verifies it; do not
rewrite it. Extend it only where a module's acceptance criterion requires, and
keep it pure (test_swing_purity.py must stay green).

Then execute modules SW0 through SW12 in order, under the Autonomy charter in
CLAUDE.md: run long, decide-record-continue, questions to Maulik are the
exception. Specifically:

- One commit per module: "SW<N>: green — <one line>". No module ends with
  either tree's test suite broken; the desk must be able to rebalance on any
  Friday.
- Never place a live order. DRY_RUN=true in every environment you create.
  BASKFY_SWING_EXECUTION_ENABLED stays false; SW10's tests assert that with it
  false no swing line can reach OrderGateway.place. The web app gets no order
  route — SW10 asserts that too.
- Track C in docs/swing/02-scope-and-gating.md is forbidden: no shorting, no
  MIS/F&O, no margin/MTF, no auto-execution, no web-app orders, no touching the
  desk's weekly momentum book or its R1–R4 overlay. Track B items are built
  dark behind flags that default off, with tests asserting they are unreachable.
- The swing book is its own sleeve with its own cash. It never sizes against
  the whole account and never sells a holding it did not buy.
- Money is Decimal; prices round at write time; every threshold is a field of
  baskfy_core.swing.config, never a literal in a detector, task, router or page.
- Judgement calls go in docs/swing/DECISIONS-SW.md, numbered by module, tagged
  ⚠ UNREVIEWED, with the rejected alternatives and the reversal path.
- Anything only Maulik's hands can supply (a Kite login, a real-money flag flip,
  a legal answer) goes to NEEDS-MAULIK.md at the root under a "Swing" heading;
  keep working on everything not dependent on it.
- Update docs/swing/STATUS.md at the end of every module — loud about what is
  NOT done. If context runs long, write enough state there for a fresh session
  to resume losslessly, then continue.
- Do not weaken tests to pass modules. Tests assert the spec (docs/swing/04),
  never current behaviour.

Stop only for the charter's stop conditions. When SW12 is green — or when a
hard blocker ends the run — write SW-FINAL-REPORT.md at the repo root in the
style of FINAL-REPORT.md: what was built, what was decided, what is NOT done,
what needs Maulik, and the exact steps for the first DRY_RUN morning. Then ask
Maulik to review.
```

---

## If the run is resumed

Same prompt. The session reads `STATUS.md`, finds the first non-green module, and continues.
Modules are independent enough that a half-built one is finished, not restarted: each module's
acceptance criteria are tests, and a test that already passes is not rewritten.
