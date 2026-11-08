# Gates: Phase 4 start — multi-tenant (tree 3)

Scope: D3 is written (posture B), so P4 engineering may start. This sitting delivers P4.1 tenant columns on the Postgres trading-path ORM plus P4.3 gateway mismatch refusal. It does not ship two-account live OAuth, RLS, Track B flag flips, web execute, or paid launch.

- [x] G1: D3 is written as posture B in DECISIONS-MERGE.md
  CHECK: rg -n "^## D3 " /Users/maulikdave/Documents/projects/baskfy/docs/DECISIONS-MERGE.md
  EXPECT: regulatory posture: B
  EVIDENCE: 2975:## D3 — regulatory posture: B · publish baskets, users execute ⚠ UNREVIEWED

- [x] G2: P4.0 / P4.1 / P4.3 decisions recorded as UNREVIEWED
  CHECK: rg -n "^## P4\.[013] " /Users/maulikdave/Documents/projects/baskfy/docs/DECISIONS-MERGE.md | wc -l | awk '{print "P4_DECISIONS="$1}'
  EXPECT: P4_DECISIONS=3
  EVIDENCE: P4_DECISIONS=3

- [x] G3: A test enumerates every trading-path ORM table and requires a tenant column
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_tenancy.py --tb=line 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: .....                                                                    [100%] | 5 passed in 0.27s

- [x] G4: Order-shaped ORM tables carry both user_id and broker_account_id
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run python -c "from baskfy_core.models import Base; from baskfy_core.tenancy import ORDER_SHAPED_TABLES; missing=[t for t in sorted(ORDER_SHAPED_TABLES) if any(n not in Base.metadata.tables[t].c for n in ('user_id','broker_account_id'))]; print('ORDER_SHAPED_OK' if not missing else 'MISSING '+str(missing))"
  EXPECT: ORDER_SHAPED_OK
  EVIDENCE: ORDER_SHAPED_OK

- [x] G5: Gateway refuses a cross-tenant order with BLOCKED, not an exception
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/execution/tests/test_tenant_isolation.py --tb=line 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: ...                                                                      [100%] | 3 passed in 0.09s

- [x] G6: Track B flags still default false (subscriptions, fee collection, public signup)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest services/api/tests/test_track_b_gates.py::test_track_b_flags_default_false --tb=line 2>&1 | tail -6
  EXPECT: passed
  EVIDENCE: .                                                                        [100%] | 1 passed in 0.06s

- [x] G7: Web / API desk surfaces still have no execute route
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest services/api/tests/test_desk_readonly.py --tb=line 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: .......                                                                  [100%] | 7 passed in 0.48s

- [x] G8: Seven non-negotiables still hold, including DRY_RUN simulate
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/kite-momentum-rebalancer && .venv/bin/python -m pytest tests/test_seven_non_negotiables.py --tb=line 2>&1 | tail -10
  EXPECT: passed
  EVIDENCE: -- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html | ======================== 16 passed, 1 warning in 0.61s =========================

- [x] G9: Schema inventory still matches the documented table list (includes broker_account)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_schema_matches_docs.py --tb=line 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: ................                                                         [100%] | 160 passed in 0.23s

- [x] G10: Mark-as-invested still records PLANNED only, and stamps tenant ids on the batch
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest services/api/tests/test_curated_investments.py --tb=line 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: ......                                                                   [100%] | 6 passed in 0.58s
