# Gates: 1.1 Own the book (integration)

Scope: children 1.1.1 mark-as-invested and 1.1.2 EXECUTED sync merged

- [x] N1: every child leaf's gates file is fully checked
  CHECK: node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs --status gates/inv-leaf-1.1.1.md gates/inv-leaf-1.1.2.md
  EXPECT: ALL MET
  EVIDENCE: gates/inv-leaf-1.1.2.md: 3 gates | ALL MET (8 met)

- [x] N2: mark path cannot set EXECUTED (source)
  CHECK: rg -n 'status.*=.*["'\'']EXECUTED["'\'']' /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/services/api/src/baskfy_api/routers/curated_investments.py /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/services/api/src/baskfy_api/curated_investments.py || echo NO_EXEC_IN_MARK
  EXPECT: NO_EXEC_IN_MARK
  EVIDENCE: NO_EXEC_IN_MARK

- [x] N3: both test files pass together
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest services/api/tests/test_curated_investments.py services/worker/tests/test_curated_batch_sync.py --tb=line 2>&1 | tail -6
  EXPECT: passed
  EVIDENCE: .............                                                            [100%] | 13 passed in 0.68s
