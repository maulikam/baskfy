# Gates: Tree 7 leaf 7.2 — investment-creation contract

Scope: Deliberately choose (a) desk creates (b) mark-as-invested (c) Kite basket handoff; record; unblock.

- [x] G1: Decision written in docs/DECISIONS-MERGE.md as T7.2 with alternatives rejected
  CHECK: rg -n "^## T7\.2|Tree 7.*investment" docs/DECISIONS-MERGE.md | head -5
  EXPECT: /
  EVIDENCE: ## T7.2 — Investment creation contract · (b) mark as invested

- [x] G2: NEEDS-MAULIK updated if chosen path needs credentials / broker / counsel
  CHECK: rg -n "investment.creation|mark as invested|T7\.2|Mark-as-invested" NEEDS-MAULIK.md | head -5
  EXPECT: /
  EVIDENCE: NEEDS-MAULIK #15 Mark-as-invested UI/API

- [x] G3: PlanHandoffPanel remains the web terminus until (c); SC11 no-order tests still present
  CHECK: rg -n "no.order|PlanHandoff|OrderGateway" decile-blueprint/apps/web/src/lib/investments/__tests__/read-only.test.ts | head -5
  EXPECT: /
  EVIDENCE: read-only.test.ts still asserts PlanHandoffPanel / no OrderGateway
