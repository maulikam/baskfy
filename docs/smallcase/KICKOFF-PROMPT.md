# The kickoff prompt

Paste the block below into a fresh CLI session at the repo root. Nothing else is needed;
the session bootstraps itself from the repo.

---

```
This is the SC run: build Baskfy's smallcase-shaped product layer.

Read, in this order, before writing any code:
1. CLAUDE.md (the root working agreement — it governs this run unchanged)
2. docs/README.md
3. docs/smallcase/README.md
4. docs/smallcase/02-scope-and-gating.md  ← the law of this run
5. docs/smallcase/06-module-plan.md       ← the task list
Read docs/smallcase/01, 03, 04, 05 as the modules cite them. The full product
reference is smallcase-product-documentation.md at the repo root.

Then execute modules SC0 through SC12 in order, under the Autonomy charter in
CLAUDE.md: run long, decide-record-continue, questions to Maulik are the
exception. Specifically:

- One commit per module: "SC<N>: green — <one line>". No module ends with
  either tree's test suite broken; the desk must be able to rebalance on any
  Friday.
- Never place a live order. DRY_RUN=true in every environment you create. The
  web app gets no order route — SC11's test asserts it.
- Track C in docs/smallcase/02-scope-and-gating.md is forbidden — no
  multi-tenant execution, no payment collection, no public sign-up, no broker
  OAuth for third parties. Track B items are built dark behind flags that
  default off, with tests asserting they are unreachable.
- Judgement calls go in docs/smallcase/DECISIONS-SC.md, numbered by module,
  tagged ⚠ UNREVIEWED, with the rejected alternatives and the reversal path.
- Anything only Maulik's hands can supply goes to NEEDS-MAULIK.md at the root;
  keep working on everything not dependent on it.
- Update docs/smallcase/STATUS.md at the end of every module — loud about what
  is NOT done. If context runs long, write enough state there for a fresh
  session to resume losslessly, then continue.
- Do not weaken tests to pass modules. Tests assert the spec (docs/smallcase/04
  is the spec for all money math), never current behaviour.

Stop only for the charter's stop conditions. When SC12 is green — or when a
hard blocker ends the run — write SC-FINAL-REPORT.md at the repo root in the
style of FINAL-REPORT.md: what was built, what was decided, what is NOT done,
what needs Maulik. Then ask Maulik to review.
```

---

## If the run is interrupted

Paste the same prompt again. SC0's first duty in any session is to read
`docs/smallcase/STATUS.md` and resume from the first module not marked green — the status
page, not memory, is the source of truth for progress.
