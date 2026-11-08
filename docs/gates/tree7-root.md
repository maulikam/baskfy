# Gates: Tree 7 root — investment completion

Scope: Catalog has numbers; investment creation is a recorded contract; SEBI furniture or ABANDON with reason.

- [x] G1: Live DB has cb_metrics rows for published baskets (or ABANDON with measured zero + why)
  CHECK: docker exec baskfy-postgres psql -U baskfy -d baskfy -tAc "SELECT COUNT(*) FROM cb_metrics"
  EXPECT: /^[1-9]/
  EVIDENCE: COUNT=1 (momentum-scan, 2026-08-21)

- [x] G2: Investment-creation contract recorded in DECISIONS-MERGE (a/b/c choice)
  CHECK: rg -n "T7\.2|investment.creation|mark as invested|PlanHandoff" docs/DECISIONS-MERGE.md | head -5
  EXPECT: /
  EVIDENCE: T7.2 chooses (b) mark as invested

- [x] G3: TREE7-PLAN.md status log reflects leaf outcomes
  CHECK: rg -n "leaf verified|ABANDON|7\.1|7\.2|7\.3" TREE7-PLAN.md
  EXPECT: /
  EVIDENCE: status log appended 24 Aug 2026
