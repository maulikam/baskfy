# Gates: leaf-4.8-runbook

Scope: Friday operator apply steps in RUN-AND-TEST

- [x] G1: Friday / apply steps
  CHECK: rg -n 'Friday|apply plan|desk console|expires' RUN-AND-TEST.md | head -5
  EXPECT: /
  EVIDENCE: Friday operator checklist in §8 (2026-08-23T04:06Z)

<!-- integrity: security, performance, memory, accuracy required -->
