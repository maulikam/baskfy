# Gates: node 1.1.1 — the allocation-ledger domain, pure

Scope: the accounting rules of `PORTFOLIO_REDESIGN.md` §4 and §5.2 as a pure module in
`packages/core`, with tests that assert the **acceptance criteria by name**. This is what §10
calls the spine and says to build before any UI.

Filed under `gates/` because `GATES.md` holds another tree's plan (a `/home` dashboard).

```
node ~/.claude/skills/unlazy/scripts/gate-check.mjs gates/portfolio-spine-1.1.1.md
```

## Declared scope — narrowed loudly, not quietly

The spec is three phases. **This node is 1.1.1 only.** Not built here, and recorded in `PLAN.md`
rather than left to be discovered missing: the schema migration (1.1.2), cash ledger (1.1.3),
nightly NAV job (1.1.4), reconciliation API (1.1.5), broker sync (1.1.6 — externally blocked,
`NEEDS-MAULIK.md` §16), and all of Phase 2's UI including the §8 renames.

Why this slice and not a slice of the page: the spec's own build order, and the fact that these
rules are *decidable without a broker, a price feed or a clock*. They can be finished, not
approximated. A page over an unbalanced ledger cannot.

**Of the eight v1 acceptance criteria, this node owns five and can prove them: 1, 2, 4, 5, 6.**
Criterion 3 (labels on screen) and 7 (no jargon in UI) are Phase 2; criterion 8 (empty state) is
Phase 2. G9 pins that split so it cannot later be misread as "all eight done".

## Facts measured before writing these gates

- No allocation ledger exists in `packages/core` (49 modules; none is one).
- `portfolio_holding`'s PK is `(portfolio_id, instrument_id, broker_account_id)`, so **criterion 2
  is structurally violable today** — the same holding in two portfolios is a legal row.
- `portfolio` has no `kind` and no `source` column.
- `curated_accounting.xirr` exists and is tested. Reuse it.

Working root for every CHECK: `/Users/maulikdave/Documents/projects/baskfy`.
Python CHECKs run from `decile-blueprint/` under `uv run`.

---

## A — the rules

- [x] G1: **Criterion 1.** Capital portfolios + Unallocated sum to consolidated net worth exactly,
      in `Decimal`, to the paisa — asserted with values chosen so a `float` implementation would
      drift.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_allocation_ledger.py -p no:randomly -k "criterion_1 or net_worth" 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ....                                                                     [100%] | 4 passed, 39 deselected in 0.09s

- [x] G2: **Criterion 2.** A holding can never be in two capital portfolios, and monitoring views
      never affect any total. Both halves asserted, including the case where a monitoring view
      overlaps a capital portfolio holding entirely.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_allocation_ledger.py -p no:randomly -k "criterion_2 or monitoring or double" 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ......                                                                   [100%] | 6 passed, 37 deselected in 0.06s

- [x] G3: **Criterion 4.** A detected sell either auto-attributes (whole-holding case) or produces
      a reconciliation item. There is no third outcome, and no path mutates a return series while
      an item is unresolved.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_allocation_ledger.py -p no:randomly -k "criterion_4 or sell or reconcil" 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ......                                                                   [100%] | 6 passed, 37 deselected in 0.06s

- [x] G4: **Criterion 5.** Model performance and actual performance are never combined into one
      figure — enforced by the return type, not by a naming convention, so the mistake does not
      typecheck.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_allocation_ledger.py -p no:randomly -k "criterion_5 or blend or model" 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ...                                                                      [100%] | 3 passed, 40 deselected in 0.06s

- [x] G5: **Criterion 6.** A split or bonus changes quantity and average price and produces
      **exactly zero** P&L — asserted on a ratio that does not divide evenly, where a naive
      implementation leaks a rounding remainder into P&L.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_allocation_ledger.py -p no:randomly -k "criterion_6 or corporate or split or bonus" 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ......                                                                   [100%] | 6 passed, 37 deselected in 0.06s

- [x] G6: **§5.2 metric labelling.** Every source has its own headline metric, each carrying its
      label and start date; a holding group without transaction history refuses to produce XIRR or
      since-purchase P&L, and says why rather than returning a plausible wrong number.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_allocation_ledger.py -p no:randomly -k "metric or headline or since_grouped" 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: .......                                                                  [100%] | 7 passed, 36 deselected in 0.06s

## B — it belongs in this codebase

- [x] G7: Law 1 holds — the module touches nothing. No database, network, disk or clock import,
      and no `datetime.now`/`today` call anywhere in it.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_allocation_ledger.py -p no:randomly -k "pure or law_1 or touches_nothing" 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ..                                                                       [100%] | 2 passed, 41 deselected in 0.06s

- [x] G8: House rules hold: no `# type: ignore`, no `Any`, no swallowed exception (rule 3, and the
      repo's own scanner agrees); money is `Decimal`, never `float` (rule 9); `ruff` and `mypy`
      clean on every file this node adds.
  CHECK: bash tools/portfolio/house-rules.sh
  EXPECT: /HOUSE RULES OK/
  EVIDENCE: mypy clean | HOUSE RULES OK

- [x] G9: The whole suite is green, and the criteria this node does **not** own are stated rather
      than implied. A reader must not be able to conclude "v1 acceptance is done".
  CHECK: cd decile-blueprint && uv run pytest packages/core -p no:randomly 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: .........................................                                [100%] | 1695 passed, 2 skipped in 63.98s (0:01:03)

## C — honest record

- [x] G10: `PLAN.md`'s status log says what was built and what remains, and this node's footprint
      is exactly the files it claims — nothing wandered into the web app or the desk.
      *Check restated after it failed for the wrong reason:* the first version asserted the web
      tree was pristine. It is not, and not because of this node — 87 files there carry the
      earlier stub-prune task's uncommitted work from the same session. Asserting a clean web
      tree would have been asserting something untrue about a different piece of work, so the
      gate now names this node's own files instead.
  CHECK: bash tools/portfolio/footprint.sh
  EXPECT: /FOOTPRINT OK/
  EVIDENCE: next node named | FOOTPRINT OK

- [x] G11: Every number in the report is re-measured at report time; the ledger is pasted with its
      N-of-N count.
  EVIDENCE: Re-measured immediately before reporting:
      ledger tests                43 passed
      whole core suite            1695 passed, 2 skipped (63s)
      allocation_ledger.py        692 lines
      test_allocation_ledger.py   506 lines
      public API surface          29 exported names
      acceptance criteria owned   5 of 8 (1, 2, 4, 5, 6) — 3, 7, 8 are Phase 2 and open
      §8 jargon in UI strings     unchanged, and deliberately so: Box 14, Book 17, Sleeve 21,
                                  Divide 9, File under 2, Run by hand 1, Nest 1. Criterion 7 is
                                  node 1.2.7, not this one.
    The one number the ask implied and this node does *not* deliver: none of the eight criteria
    is claimed beyond the five listed, and no UI was touched.

<!--
A checked box with EVIDENCE still "pending" counts as UNMET.
If a gate becomes impossible: add `ABANDON: G<n> <reason>` and say so in the report.
-->
