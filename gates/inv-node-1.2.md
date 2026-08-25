# Gates: 1.2 Hear about it (integration)

Scope: children 1.2.1 rebalance notify and 1.2.2 SIP persistence

- [x] N1: child leaves ALL MET
  CHECK: node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs --status gates/inv-leaf-1.2.1.md gates/inv-leaf-1.2.2.md
  EXPECT: ALL MET
  EVIDENCE: gates/inv-leaf-1.2.2.md: 3 gates | ALL MET (6 met)

- [x] N2: both test files pass
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest services/worker/tests/test_curated_rebalance_notify.py services/api/tests/test_curated_sip_api.py --tb=line 2>&1 | tail -6
  EXPECT: passed
  EVIDENCE: ........                                                                 [100%] | 8 passed in 0.89s
