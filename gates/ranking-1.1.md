# Gates: ranking-1.1 Phase 1 branch

Scope: Integration of registry additions, screen-definition modes/scopes, and desk-score adapter.

- [ ] B1: All Phase 1 leaf checks in root GATES.md G1–G6 pass together
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs GATES.md 2>&1 | tail -20
  EXPECT: /all gates passed|PASS/
  EVIDENCE: pending

- [x] B2: Stock quality remains separate from portfolio selection in the plan contract
  CHECK: rg -n "portfolio selection are separate" /Users/maulikdave/Documents/projects/baskfy/docs/ranking/PLAN.md
  EXPECT: portfolio selection are separate
  EVIDENCE: 21:  - Stock quality rank and portfolio selection are separate outputs. Selection must not mutate SCORE.
