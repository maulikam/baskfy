# Gates: leaf B1-cash-ledger

Scope: §4.4 cash ledger — Unallocated bucket, internal flows, pure per-portfolio XIRR

Written by the parent before fan-out. The leaf agent proves its own work; the parent
re-runs these checks independently — that verification hierarchy is the whole point of
orchestrated mode.

---

- [x] G1: The module exists, is pure (law 1), and its tests pass.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_cash_ledger.py -p no:randomly 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ...                                                                      [100%] | 75 passed in 0.11s

- [x] G2: ASSIGN/RELEASE are XIRR events; BUY/SELL/DIVIDEND are not. §4.4's whole distinction.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_cash_ledger.py -p no:randomly -k "xirr or assign or flow" 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ...................................................                      [100%] | 51 passed, 24 deselected in 0.08s

- [x] G3: House rules: no type: ignore, no Any, no float; ruff and mypy clean.
  CHECK: bash tools/portfolio/leaf-hygiene.sh cash_ledger
  EXPECT: /HYGIENE OK/
  EVIDENCE: mypy clean | HYGIENE OK

<!--
A checked box with EVIDENCE still "pending" counts as UNMET.
If a gate becomes impossible: add `ABANDON: G<n> <reason>` and say so in the report.
-->
