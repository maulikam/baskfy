# Gates: leaf B3-reconciliation

Scope: §4.3 inbox + §4.5 atomic corporate-action fan-out + §6.4 needs-attention

Written by the parent before fan-out. The leaf agent proves its own work; the parent
re-runs these checks independently — that verification hierarchy is the whole point of
orchestrated mode.

---

- [x] G1: The module exists, is pure, and its tests pass.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_reconciliation.py -p no:randomly 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ...............................................................          [100%] | 63 passed in 0.25s

- [x] G2: An open item FREEZES the affected holding rather than guessing (§4.3's central rule).
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_reconciliation.py -p no:randomly -k "freeze or pending or open" 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ...............                                                          [100%] | 15 passed, 48 deselected in 0.25s

- [x] G3: A corporate action fans out to every view of the holding atomically, with zero P&L (§4.5).
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_reconciliation.py -p no:randomly -k "corporate or fan or atomic" 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ...........                                                              [100%] | 11 passed, 52 deselected in 0.31s

- [x] G4: allocation_ledger still passes — proof no file this leaf does not own was edited.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_allocation_ledger.py -p no:randomly 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ...........................................                              [100%] | 43 passed in 0.15s

- [x] G5: House rules clean.
  CHECK: bash tools/portfolio/leaf-hygiene.sh reconciliation
  EXPECT: /HYGIENE OK/
  EVIDENCE: mypy clean | HYGIENE OK

<!--
A checked box with EVIDENCE still "pending" counts as UNMET.
If a gate becomes impossible: add `ABANDON: G<n> <reason>` and say so in the report.
-->
