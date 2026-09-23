# The kickoff prompt

Paste the block below into a fresh CLI session at the repo root. The run is resumable: a fresh
session reads `docs/fno/STATUS.md` and continues from the first module not marked ✅. **Do not
start it while an OP module is mid-flight in the same tree** without reading `06`'s coordination
note.

---

```
This is the FO run: add overnight, defined-risk F&O to Baskfy — two paper
sleeves (F1, NIFTY/BANKNIFTY monthly iron condors carried overnight; F2,
stock-futures breakout long only, built by Maulik's choice against the research,
DECISIONS-FO M.1), the F&O data layer (nightly bhavcopy
ingest, measured option spreads, a quarterly re-test of every rejected family),
and a read-only Stock F&O information page — with every money flag off.

Read, in this order, before writing any code:
1. CLAUDE.md (the root working agreement — it governs this run unchanged)
2. docs/README.md
3. docs/fno/README.md
4. docs/fno/RESEARCH.md            ← why the pack builds so little; every number
5. docs/fno/02-scope-and-gating.md ← the law
6. docs/fno/07-data-reality.md
7. docs/fno/06-module-plan.md      ← the task list
8. docs/fno/04-business-rules.md   ← the numerical contract (tests assert it)
Read docs/fno/01, 03, 05 as modules cite them, and docs/options/ where a
module reuses its code (greeks, costs, the fill simulator, the NFO master).

Then execute FO0 through FO12 in order, under the Autonomy charter in CLAUDE.md.

- One commit per module: "FO<N>: green — <one line>". Never git add -A; commit
  only the module's files. The OP run may have uncommitted work in the tree.
- Never place a live order. DRY_RUN=true everywhere you create an env.
  OPTIONS_ENABLED, BASKFY_FNO_CARRY_ENABLED and every
  BASKFY_FNO_*_EXECUTION_ENABLED stay false; you never flip any of them. No
  auto-execute flag exists for any FO sleeve and you never add one. The web app
  gets no order route.
- Track C in docs/fno/02 is forbidden: no naked short option at any instant, no
  stock derivative into expiry day, no auto entry, no rolling/averaging, no
  sizing from margin, no touching other books or the SGB, no new provider, no
  trading a banned stock, no touching frozen/.
- The O-sleeves (docs/options) keep their Track C: FO6's gateway branch applies
  to fo_plan orders only, and every OP gating test stays green unedited.
- Start only when docs/options/STATUS.md shows OP15 ✅ (M.1).
- Do not build a family RESEARCH.md rejected, other than F2. If a module's evidence says
  otherwise, write it in DECISIONS-FO.md for Maulik and carry on with the plan.
- A backtest number carries its tier and caveat (07 §4) on the row and the page.
- Judgement calls go in docs/fno/DECISIONS-FO.md, numbered by module, tagged
  ⚠ UNREVIEWED. Anything only Maulik can supply goes to NEEDS-MAULIK.md under
  an "F&O" heading.
- Update docs/fno/STATUS.md at the end of every module, loud about what is NOT
  done. Tests assert docs/fno/04, never current behaviour.

When FO12 is green, or a hard blocker ends the run, write FO-FINAL-REPORT.md at
the repo root: what was built, what was decided, what is NOT done, what needs
Maulik, and the first paper entry day step by step. Then ask Maulik to review.
```
