# Gates: leaf B5-grouping-suggestions

Scope: §6.6 activation — suggest groupings; §Phase 3 overlap + contribution

Written by the parent before fan-out. The leaf agent proves its own work; the parent
re-runs these checks independently — that verification hierarchy is the whole point of
orchestrated mode.

---

- [x] G1: The module exists, is pure, and its tests pass.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_grouping_suggestions.py -p no:randomly 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ..................................................................       [100%] | 66 passed in 0.09s

- [x] G2: Suggestions are deterministic — same input, same order out.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_grouping_suggestions.py -p no:randomly -k "determin or stable or order" 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ......                                                                   [100%] | 6 passed, 60 deselected in 0.07s

- [x] G3: Contributions sum EXACTLY to the total move — the property that makes the number trustworthy.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_grouping_suggestions.py -p no:randomly -k "contribut or sum" 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: .............                                                            [100%] | 13 passed, 53 deselected in 0.07s

- [x] G4: House rules clean.
  CHECK: bash tools/portfolio/leaf-hygiene.sh grouping_suggestions
  EXPECT: /HYGIENE OK/
  EVIDENCE: mypy clean | HYGIENE OK

<!--
A checked box with EVIDENCE still "pending" counts as UNMET.
If a gate becomes impossible: add `ABANDON: G<n> <reason>` and say so in the report.
-->
