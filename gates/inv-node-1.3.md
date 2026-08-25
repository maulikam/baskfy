# Gates: 1.3 Live with it (integration)

Scope: children 1.3.1 costs page and 1.3.2 drift-repair

- [x] N1: child leaves ALL MET
  CHECK: node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs --status gates/inv-leaf-1.3.1.md gates/inv-leaf-1.3.2.md
  EXPECT: ALL MET
  EVIDENCE: gates/inv-leaf-1.3.2.md: 3 gates | ALL MET (6 met)

- [x] N2: both API test files pass
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest services/api/tests/test_curated_costs.py services/api/tests/test_curated_drift_api.py --tb=line 2>&1 | tail -6
  EXPECT: passed
  EVIDENCE: .......                                                                  [100%] | 7 passed in 1.47s
