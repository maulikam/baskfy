# Gates: node-1.8

Scope: SC11 hardening: security, perf, memory, accuracy

- [x] G1: all integrity leaf tests referenced in STATUS
  CHECK: rg -n 'SC11|integrity|hardening' docs/smallcase/STATUS.md
  EXPECT: SC11|integrity|hardening
  EVIDENCE: 2026-08-23 — STATUS module ledger + SC11 integrity section. Leaves 1.8.1–1.8.5
  green (orders 2; tenant 5; perf 3; memory CHUNK=50; accuracy 21 accounting).

<!-- integrity: security, performance, memory, accuracy required -->
