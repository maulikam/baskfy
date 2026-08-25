# Gates: ROOT — portfolio management is expressible

Scope: one person, many portfolios, portfolios inside portfolios, across brokers, across strategies — expressible in the schema, reachable through the API, visible in the app, and honestly labelled.

- [x] R1: every branch and leaf gates file is fully met
  CHECK: node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs --status gates/pm-node-A.md gates/pm-node-B.md gates/pm-node-C.md gates/pm-leaf-D1-web.md gates/pm-leaf-E1-docs.md 2>&1 | tail -8
  EXPECT: ALL MET
  EVIDENCE: gates/pm-leaf-E1-docs.md: 8 gates | ALL MET (31 met, 2 abandoned)

- [x] R2: the five gaps in the brief are each closed by a named artefact — stated one line per gap with file:line, not asserted in prose
  EVIDENCE: gap1 nesting: models/accounts.py:234 `parent_id` + portfolio_graph.py:612 `check_move` | gap2 broker attribution: accounts.py:238 (portfolio, nullable=spans) + accounts.py:281 (holding, NOT NULL) + accounts.py:267 PK(portfolio_id,instrument_id,broker_account_id) | gap3 the join: accounts.py:425 kind IN ('screen','manual','basket') + accounts.py:449 basket_id + curated_baskets.py:321 cb_investment.portfolio_id | gap4 broker honesty: broker_connections.py:133 is now the ONLY holdings_sync="ready" row (8 before) | gap5 units: portfolio_units.py:334 `allocate_units` | gap6 provenance: broker_holdings.py:92 `class HoldingsResult` + routers/brokers.py:419 `source=result.source`

- [x] R3: the Python suite passes against the live database under a MINIMAL environment (only BASKFY_TEST_DATABASE_URL exported — sourcing all of .env sets BASKFY_KITE_API_KEY and breaks tests that assert unconfigured-provider behaviour, a defect in the driver's own harness, not in the code). One pre-existing failure is tolerated and named: `test_api_run.py::TestCsvExport::test_the_values_match_the_json_response_exactly`, proven pre-existing by an A/B with 0019 downgraded. Latency budgets under `TestFiftyConcurrentScreenRuns` and `TestFullUniverseCost` are load-sensitive on this machine and are reported as UNVERIFIED, not as passing.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL="$(grep -E '^BASKFY_TEST_DATABASE_URL=' .env | cut -d= -f2- | tr -d '"')" && OUT=$(uv run pytest --tb=no 2>&1); echo "$OUT" | tail -2; OTHER=$(echo "$OUT" | grep '^FAILED' | grep -vcE 'test_the_values_match_the_json_response_exactly|TestFiftyConcurrentScreenRuns|TestFullUniverseCost'); echo "UNEXPECTED=$OTHER"; [ "$OTHER" = 0 ] && echo GATE_OK || echo GATE_FAILED
  EXPECT: GATE_OK
  EVIDENCE: UNEXPECTED=0 | GATE_OK

- [x] R4: the full TypeScript suite passes
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && OUT=$(pnpm -r run test 2>&1); RC=$?; echo "$OUT" | grep -E 'Test Files|Tests ' | tail -2; [ "$RC" = 0 ] && echo GATE_OK || echo GATE_FAILED
  EXPECT: GATE_OK
  EVIDENCE: apps/web test:       Tests  1379 passed (1379) | GATE_OK

- [x] R5: every file THIS TREE owns is lint- and type-clean (ruff + ruff format + mypy strict on all 22 owned Python files; eslint + tsc on D1's three web paths), and the repo-wide residue is MEASURED and attributed rather than absorbed. `make lint` as a whole still FAILS and this tree does not claim otherwise: 13 ruff errors in 6 files no tree-5 leaf owns (test_curated_investments, celery_tasks, baskfy_api/curated_investments, test_deep_backfill, basket_sizing x2), 3 tsc errors inside another tree's uncommitted backtest feature, and 22 eslint errors in 14 non-D1 web files. mypy is clean across all 151 source files.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run ruff check packages/core/src/baskfy_core/portfolio_graph.py packages/core/src/baskfy_core/portfolio_units.py packages/core/src/baskfy_core/broker_connections.py packages/core/src/baskfy_core/models/accounts.py packages/core/src/baskfy_core/models/curated_baskets.py services/api/alembic/versions/0019_portfolio_graph.py services/api/src/baskfy_api/broker_holdings.py services/api/src/baskfy_api/portfolios.py services/api/src/baskfy_api/routers/portfolios.py services/api/src/baskfy_api/routers/sleeves.py services/api/src/baskfy_api/routers/curated_investments.py services/api/src/baskfy_api/routers/brokers.py packages/core/tests/test_portfolio_schema.py packages/core/tests/test_portfolio_graph.py packages/core/tests/test_portfolio_units.py packages/core/tests/test_broker_capability_honesty.py packages/core/tests/test_schema_matches_docs.py services/api/tests/test_api_portfolios_tree.py services/api/tests/test_api_sleeves_basket.py services/api/tests/test_cb_investment_portfolio.py services/api/tests/test_broker_holdings_provenance.py services/api/tests/test_api_artifacts.py >/dev/null 2>&1 && uv run ruff format --check packages/core/src/baskfy_core/portfolio_graph.py packages/core/src/baskfy_core/portfolio_units.py packages/core/src/baskfy_core/broker_connections.py packages/core/src/baskfy_core/models/accounts.py packages/core/src/baskfy_core/models/curated_baskets.py services/api/alembic/versions/0019_portfolio_graph.py services/api/src/baskfy_api/broker_holdings.py services/api/src/baskfy_api/portfolios.py services/api/src/baskfy_api/routers/portfolios.py services/api/src/baskfy_api/routers/sleeves.py services/api/src/baskfy_api/routers/curated_investments.py services/api/src/baskfy_api/routers/brokers.py packages/core/tests/test_portfolio_schema.py packages/core/tests/test_portfolio_graph.py packages/core/tests/test_portfolio_units.py packages/core/tests/test_broker_capability_honesty.py packages/core/tests/test_schema_matches_docs.py services/api/tests/test_api_portfolios_tree.py services/api/tests/test_api_sleeves_basket.py services/api/tests/test_cb_investment_portfolio.py services/api/tests/test_broker_holdings_provenance.py services/api/tests/test_api_artifacts.py >/dev/null 2>&1 && uv run mypy packages/core/src services/api/src >/dev/null 2>&1 && OWNED_OK=yes || OWNED_OK=no; cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec eslint src/lib/portfolios src/components/portfolios 'src/app/(app)/portfolios' >/dev/null 2>&1 && WEB_OK=yes || WEB_OK=no; cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && echo "OWNED_OK=$OWNED_OK WEB_OWNED_OK=$WEB_OK UNOWNED_RUFF=$(uv run ruff check packages services --output-format=concise 2>/dev/null | grep -c ':')"; [ "$OWNED_OK" = yes ] && [ "$WEB_OK" = yes ] && echo GATE_OK || echo GATE_FAILED
  EXPECT: GATE_OK
  EVIDENCE: OWNED_OK=yes WEB_OWNED_OK=yes UNOWNED_RUFF=13 | GATE_OK

- [x] R6: migration 0019 round-trips against the live database one final time
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && cd services/api && uv run alembic upgrade head >/dev/null 2>&1 && uv run alembic downgrade 0018_trading_path_tenancy >/dev/null 2>&1 && uv run alembic upgrade head >/dev/null 2>&1 && echo ROUNDTRIP_OK || echo ROUNDTRIP_FAILED
  EXPECT: ROUNDTRIP_OK
  EVIDENCE: ROUNDTRIP_OK

- [x] R7: the seven desk non-negotiables and the two laws still hold
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest -k "non_negotiable or no_orders or desk_readonly or tenant_isolation or track_b" --tb=line 2>&1 | tail -5
  EXPECT: /^\d+ passed/m
  EVIDENCE: ..........................                                               [100%] | 26 passed, 3285 deselected in 2.15s

- [x] R8: the desk's own suite is green — the desk can still rebalance on any Friday (safety rail)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && export BASKFY_TEST_DATABASE_URL="$(grep -E '^BASKFY_TEST_DATABASE_URL=' .env | cut -d= -f2- | tr -d '"')" && OUT=$(uv run pytest --tb=no 2>&1); echo "$OUT" | tail -2; OTHER=$(echo "$OUT" | grep '^FAILED' | grep -vcE 'test_the_values_match_the_json_response_exactly|TestFiftyConcurrentScreenRuns|TestFullUniverseCost'); echo "UNEXPECTED=$OTHER"; [ "$OTHER" = 0 ] && echo GATE_OK || echo GATE_FAILED
  EXPECT: GATE_OK
  EVIDENCE: -- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html | 1330 passed, 17 skipped, 33 warnings, 12 subtests passed in 43.73s

- [x] R9: kite-momentum-rebalancer/data/portfolio.db is byte-identical to its pre-tree state (unrebuildable evidence, untouched)
  CHECK: git status --porcelain kite-momentum-rebalancer/data/ | head -3; echo "DB_DIRTY=$(git status --porcelain kite-momentum-rebalancer/data/ | wc -l | tr -d ' ')"
  EXPECT: DB_DIRTY=0
  EVIDENCE: DB_DIRTY=0

- [x] R10: frozen/strangle/ is untouched
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && echo "FROZEN_DIRTY=$(git status --porcelain frozen/ | wc -l | tr -d ' ')"
  EXPECT: FROZEN_DIRTY=0
  EVIDENCE: FROZEN_DIRTY=0

- [x] R11: no namespace regression
  CHECK: (./tools/check-namespace.sh 2>&1 | tail -4); cd /Users/maulikdave/Documents/projects/baskfy && ./tools/check-namespace.sh >/dev/null 2>&1 && echo GATE_OK || echo GATE_FAILED
  EXPECT: GATE_OK
  EVIDENCE: and no occurrence of the old brand name outside the blog post about deciles. | GATE_OK

- [x] R12: every number in the final report was re-measured at report time, and each is traceable to a CHECK in these files
  EVIDENCE: all re-measured at report time: GATES_TOTAL=130 MET=127 ABANDONED=2 PENDING=1(this gate) | DECISION_ENTRIES=12 TAGGED=12 | core 1556 passed,2 skipped | B suites 111 passed | broker suites 83 passed | web 1379 passed/72 files | fixture-return paths pre-tree=6 (brief said 7) | holdings_sync="ready" rows 8->1 | web tsc 105(committed client)->3(regenerated) | UNOWNED_RUFF=13 | mypy clean 151 files
