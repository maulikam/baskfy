# The kickoff prompt

Paste the block below into a fresh CLI session at the repo root. Nothing else is needed; the
session bootstraps itself from the repo. The run is resumable: a fresh session reads
`docs/condor/STATUS.md` and continues from the first module not marked ✅.

---

```
This is the OC run: build one range-filtered, fully hedged NIFTY monthly-expiry
iron condor into Baskfy — planned, gated, sized, confirmed and managed through
the desk, paper-proven end to end with every product flag off.

Read, in this order, before writing any code:
1. CLAUDE.md (the root working agreement — it governs this run unchanged)
2. docs/README.md
3. docs/condor/README.md
4. docs/condor/02-scope-and-gating.md   ← the law of this run
5. docs/condor/07-data-reality.md       ← what a backtest may claim here
6. docs/condor/06-module-plan.md        ← the task list
7. docs/condor/04-business-rules.md     ← the numerical contract (tests assert it)
Read docs/condor/01, 03, 05 as the modules cite them; docs/condor/QUESTIONS.md
holds the standing defaults for everything only Maulik can answer.

There is no pre-built core this time. OC1 writes packages/core/src/baskfy_core/
condor/ from docs/condor/04, pure and typed (test_condor_purity.py must be green),
porting arithmetic from frozen/strangle/ by re-implementation only — never
git mv, import from, or edit anything under frozen/ (D4, PACK.1).

Then execute modules OC0 through OC12 in order, under the Autonomy charter in
CLAUDE.md: run long, decide-record-continue, questions to Maulik are the
exception. Specifically:

- One commit per module: "OC<N>: green — <one line>". No module ends with
  either tree's test suite broken; the desk must be able to rebalance on any
  Friday and the swing book must run on any morning.
- Never place a live order. DRY_RUN=true in every environment you create.
  OPTIONS_ENABLED, INTRADAY_ENABLED and BASKFY_CONDOR_EXECUTION_ENABLED stay
  false; you never flip any of them; OC10's tests assert that with any one of
  the four false no condor line can reach a broker. The web app gets no order
  route — OC10 asserts that too.
- Track C in docs/condor/02 is forbidden: no naked leg at any instant, no
  product other than MIS, no overnight, no re-entry, no rolling, no weekly
  expiries, no auto-executed entry, no web-app orders, no touching the weekly
  or swing books, no new data provider, no sizing from margin. Track B is built
  dark behind flags that default off, with tests asserting unreachability.
- NIFTY only in Track A. BANKNIFTY is built underlying-agnostically and backtested
  separately, and stays behind BASKFY_CONDOR_BANKNIFTY_ENABLED.
- A backtest number carries its tier and its caveat (docs/condor/07 §4) on the
  row and on the page. Never pool tiers; never pool real and simulated.
- Money is Decimal; prices round at write time; every threshold is a field of
  baskfy_core.condor.config, never a literal in a gate, builder, task, router
  or page. Lot sizes come from the instrument master, never from a constant.
- Judgement calls go in docs/condor/DECISIONS-OC.md, numbered by module, tagged
  ⚠ UNREVIEWED, with the rejected alternatives and the reversal path.
- Anything only Maulik's hands can supply (a Kite login, the F&O segment, the
  margin pool, a data purchase, a flag flip, the risk decision) goes to
  NEEDS-MAULIK.md at the root under a "Condor" heading; keep working on
  everything not dependent on it.
- Update docs/condor/STATUS.md at the end of every module — loud about what is
  NOT done. If context runs long, write enough state there for a fresh session
  to resume losslessly, then continue.
- Do not weaken tests to pass modules. Tests assert the spec (docs/condor/04),
  never current behaviour.

Stop only for the charter's stop conditions. When OC12 is green — or when a
hard blocker ends the run — write OC-FINAL-REPORT.md at the repo root in the
style of SW-FINAL-REPORT.md: what was built, what was decided, what is NOT
done, what needs Maulik, the exact steps for the first paper expiry morning,
and the six-expiry checklist from docs/condor/02 §3.2. Then ask Maulik to
review.
```

---

## If the run is resumed

Same prompt. The session reads `STATUS.md`, finds the first non-green module, and continues.
Modules are independent enough that a half-built one is finished, not restarted: each module's
acceptance criteria are tests, and a test that already passes is not rewritten.
