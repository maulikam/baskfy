# Gates: leaf B4-cas-import

Scope: §5.3 CAS import parser — CDSL and NSDL, so holding groups can leave 'since grouped'

Written by the parent before fan-out. The leaf agent proves its own work; the parent
re-runs these checks independently — that verification hierarchy is the whole point of
orchestrated mode.

---

- [x] G1: The module exists, is pure, and its tests pass.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_cas_import.py -p no:randomly 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ....................................................                     [100%] | 52 passed in 0.12s

- [x] G2: Both formats parse and the format is detected from content, not passed in.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_cas_import.py -p no:randomly -k "cdsl or nsdl or detect" 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: .............                                                            [100%] | 13 passed, 39 deselected in 0.08s

- [x] G3: A corrupt statement raises rather than returning a partial parse that would backfill wrong buy prices.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_cas_import.py -p no:randomly -k "corrupt or truncat or partial or raise" 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: .............                                                            [100%] | 13 passed, 39 deselected in 0.09s

- [x] G4: House rules clean.
  CHECK: bash tools/portfolio/leaf-hygiene.sh cas_import
  EXPECT: /HYGIENE OK/
  EVIDENCE: mypy clean | HYGIENE OK

<!--
A checked box with EVIDENCE still "pending" counts as UNMET.
If a gate becomes impossible: add `ABANDON: G<n> <reason>` and say so in the report.
-->
