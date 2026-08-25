# Gates: B — API surfaces (integration)

Scope: children B1 (portfolios), B2 (sleeves), B3 (investment link) merged; the generated client regenerated once by the driver.

- [x] N1: every child leaf's gates file is fully checked, no pending evidence
  CHECK: node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs --status gates/pm-leaf-B1-portfolios-api.md gates/pm-leaf-B2-sleeves-api.md gates/pm-leaf-B3-investments-link.md 2>&1 | tail -6
  EXPECT: ALL MET
  EVIDENCE: gates/pm-leaf-B3-investments-link.md: 9 gates | ALL MET (32 met)

- [x] N2: the OpenAPI document regenerates cleanly and includes the new routes
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && make openapi >/dev/null 2>&1 && grep -cE "parent_id|/sleeves|portfolio_id" packages/api-client/openapi.json | awk '{print "OPENAPI_HITS="$1}'
  EXPECT: /OPENAPI_HITS=[1-9]/
  EVIDENCE: OPENAPI_HITS=44

- [x] N3: the TypeScript client regenerates and the api-client package typechecks against the new schema (the WEB app's typecheck is D1's G8 and root R4 — scoped here because 6 web files still name the pre-0019 schemas and fixing them is D1's owned work, not node B's)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && make client >/dev/null 2>&1 && cd packages/api-client && (pnpm exec tsc --noEmit 2>&1 | head -5); cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/packages/api-client && pnpm exec tsc --noEmit >/dev/null 2>&1 && echo GATE_OK || echo GATE_FAILED
  EXPECT: GATE_OK
  EVIDENCE: GATE_OK

- [x] N4: end to end — create a parent, nest a child, attach a basket sleeve, link an investment, read the roll-up
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_api_portfolios_tree.py services/api/tests/test_api_sleeves_basket.py services/api/tests/test_cb_investment_portfolio.py -m db --tb=line 2>&1 | tail -4
  EXPECT: /^\d+ passed/m
  EVIDENCE: ....................................                                     [100%] | 108 passed, 3 deselected in 22.31s

- [x] N5: every API suite this tree touched, plus the suites the schema change could plausibly reach, passes when run SERIALLY. Whole-suite green is deliberately NOT claimed: on this machine `pytest services/api` returns a DIFFERENT failure set each run (measured across four runs: csv-export, pool-bottleneck, p95-latency, api-key-digest), every one of which passes in isolation and greps 0 for portfolio/sleeve/broker/investment. Cause is shared-DB pollution plus load-sensitive latency budgets, NOT random ordering (pytest-randomly and xdist are both absent) and NOT this tree. Reported as an unresolved environment defect at root rather than absorbed.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_api_portfolios_tree.py services/api/tests/test_api_sleeves_basket.py services/api/tests/test_cb_investment_portfolio.py services/api/tests/test_api_portfolios.py services/api/tests/test_broker_holdings_provenance.py services/api/tests/test_broker_holdings_sync.py services/api/tests/test_brokers.py services/api/tests/test_broker_oauth.py services/api/tests/test_curated_investments.py services/api/tests/test_api_artifacts.py services/api/tests/test_ac_no_orders.py services/api/tests/test_desk_readonly.py services/api/tests/test_track_b_gates.py services/api/tests/test_api_keys.py --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: .............................................................            [100%] | 277 passed in 50.91s

- [x] N6: no execute route exists anywhere in the API after the merge (non-negotiable #1)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_ac_no_orders.py services/api/tests/test_desk_readonly.py services/api/tests/test_track_b_gates.py --tb=line 2>&1 | tail -4
  EXPECT: /^\d+ passed/m
  EVIDENCE: ..................                                                       [100%] | 18 passed in 0.92s
