# Gates: C1 — holdings provenance: live is never labelled fixture

Scope: the money-safety defect. `holdings_for_broker` currently returns a bare list, so `routers/brokers.py:326` labels EVERY non-empty response "fixture holdings", including a real Kite fetch. Replace the return with a provenance-tagged result and report it verbatim.

Measured baseline before work: `rg -c "return _fixture_holdings\(\)" services/api/src/baskfy_api/broker_holdings.py` = 6 (the brief said seven; six is the measured number). One of those six is the DRY_RUN branch; the router cannot tell any of them from the live fetch at broker_holdings.py:174.

- [x] G1: holdings_for_broker returns a provenance-tagged result with source in {live, fixture, empty, unwired}
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run python -c "from baskfy_api.broker_holdings import holdings_for_broker; import inspect; r=holdings_for_broker('zerodha'); print('SOURCE='+str(getattr(r,'source',None))); print('HAS_ROWS='+str(hasattr(r,'rows')))"
  EXPECT: /SOURCE=(live|fixture|empty|unwired)\nHAS_ROWS=True/
  EVIDENCE: SOURCE=empty | HAS_ROWS=True

- [x] G2: an unwired broker reports source='unwired', never 'fixture' and never 'live'
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_broker_holdings_provenance.py -k "unwired" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: .......                                                                  [100%] | 7 passed, 57 deselected in 0.07s

- [x] G3: a live Kite fetch that returns rows reports source='live' — the defect being fixed, asserted directly
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_broker_holdings_provenance.py -k "live_not_fixture or live_labelled" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: .                                                                        [100%] | 1 passed, 63 deselected in 0.06s

- [x] G4: a fixture path reports source='fixture' and the router's note says fixture
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_broker_holdings_provenance.py -k "fixture" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: .................                                                        [100%] | 17 passed, 47 deselected in 0.07s

- [x] G5: a network / Kite error falls back to fixture BUT is labelled as a degraded fixture, distinguishable from a configured one
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_broker_holdings_provenance.py -k "error or degraded or fallback" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: .............                                                            [100%] | 13 passed, 51 deselected in 0.06s

- [x] G6: the endpoint response exposes source as a field, not only inside a prose note a client cannot parse
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run python -c "from baskfy_api.routers.brokers import SyncHoldingsOut; print('SOURCE_FIELD_OK' if 'source' in SyncHoldingsOut.model_fields else 'SOURCE_FIELD_MISSING')"
  EXPECT: SOURCE_FIELD_OK
  EVIDENCE: SOURCE_FIELD_OK

- [x] G7: no code path can produce rows with source='empty' (the enum stays truthful)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_broker_holdings_provenance.py -k "invariant or truthful or empty" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ...................                                                      [100%] | 19 passed, 45 deselected in 0.06s

- [x] G8: desk non-negotiable #2 still holds — quantity is quantity + t1_quantity + collateral_quantity
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests -k "holdings and (quantity or t1 or collateral)" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed|no tests ran/m
  EVIDENCE: .....                                                                    [100%] | 5 passed, 1075 deselected in 0.41s

- [x] G9: still no order path — this module reaches Kite read-only
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && echo "ORDER_HITS=$(rg -c 'place_order|place_gtt|OrderGateway|/execute' services/api/src/baskfy_api/broker_holdings.py services/api/src/baskfy_api/routers/brokers.py 2>/dev/null | awk -F: '{s+=$2} END {print s+0}')"
  EXPECT: ORDER_HITS=0
  EVIDENCE: ORDER_HITS=0

- [x] G10: the pre-existing broker suites still pass
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_broker_holdings_sync.py services/api/tests/test_brokers.py services/api/tests/test_broker_oauth.py --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ...................                                                      [100%] | 19 passed in 1.26s

- [x] G11: lint + mypy strict clean
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run ruff check services/api/src/baskfy_api/broker_holdings.py services/api/src/baskfy_api/routers/brokers.py services/api/tests/test_broker_holdings_provenance.py 2>&1 | tail -2 && uv run mypy services/api/src/baskfy_api/broker_holdings.py services/api/src/baskfy_api/routers/brokers.py 2>&1 | tail -2
  EXPECT: /Success: no issues/
  EVIDENCE: All checks passed! | Success: no issues found in 2 source files
