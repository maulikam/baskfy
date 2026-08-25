# Gates: B1 — portfolios router learns the tree

Scope: create-with-parent, list as a tree, move a portfolio, per-broker roll-up. Read/record only — no order path, no execute route.

- [x] G1: POST /portfolios accepts an optional parent and rejects a parent owned by another user as NOT_FOUND (never leaks existence)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_api_portfolios_tree.py -m db -k "parent" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: .............                                                            [100%] | 13 passed, 40 deselected in 4.40s

- [x] G2: a cycle is refused with VALIDATION at the API boundary, not a 500
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_api_portfolios_tree.py -m db -k "cycle" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ....                                                                     [100%] | 4 passed, 49 deselected in 2.83s

- [x] G3: depth beyond 6 is refused with VALIDATION
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_api_portfolios_tree.py -m db -k "depth" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ......                                                                   [100%] | 6 passed, 47 deselected in 3.21s

- [x] G4: GET /portfolios returns nesting — a child appears under its parent, not as a flat sibling
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_api_portfolios_tree.py -m db -k "tree or nested" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: .....................................................                    [100%] | 53 passed in 11.53s

- [x] G5: moving a portfolio to a new parent works and re-parenting into its own subtree is refused
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_api_portfolios_tree.py -m db -k "move" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ............                                                             [100%] | 12 passed, 41 deselected in 4.42s

- [x] G6: a roll-up endpoint reports a subtree's holdings split by broker account, with a total
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_api_portfolios_tree.py -m db -k "rollup or broker" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: .................                                                        [100%] | 17 passed, 36 deselected in 5.12s

- [x] G7: deleting a parent does not orphan children into another user's view; the documented behaviour is asserted either way
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_api_portfolios_tree.py -m db -k "delete or cascade" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ......                                                                   [100%] | 6 passed, 47 deselected in 3.20s

- [x] G8: tenant isolation holds — user B can never read, move or re-parent user A's portfolio
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_api_portfolios_tree.py -m db -k "tenant or isolation or other_user" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: .........                                                                [100%] | 9 passed, 44 deselected in 3.73s

- [x] G9: no execute route, no order call, appeared anywhere in this router (non-negotiable #1, law 2)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && echo "ORDER_HITS=$(grep -cE 'place_order|OrderGateway|/execute|confirm=True' services/api/src/baskfy_api/routers/portfolios.py 2>/dev/null || echo 0)"
  EXPECT: ORDER_HITS=0
  EVIDENCE: ORDER_HITS=0 | 0

- [x] G10: the pre-existing portfolio suite still passes — nothing regressed
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_api_portfolios.py -m db --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ............................                                             [100%] | 28 passed in 6.83s

- [x] G11: the repo-wide no-order guard still passes
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_ac_no_orders.py services/api/tests/test_desk_readonly.py --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ...........                                                              [100%] | 11 passed in 0.47s

- [x] G12: lint + mypy strict clean
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run ruff check services/api/src/baskfy_api/routers/portfolios.py services/api/src/baskfy_api/portfolios.py services/api/tests/test_api_portfolios_tree.py 2>&1 | tail -2; cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run mypy services/api/src/baskfy_api/routers/portfolios.py services/api/src/baskfy_api/portfolios.py 2>&1 | tail -2; cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run ruff check services/api/src/baskfy_api/routers/portfolios.py services/api/src/baskfy_api/portfolios.py services/api/tests/test_api_portfolios_tree.py >/dev/null 2>&1 && uv run ruff format --check services/api/src/baskfy_api/routers/portfolios.py services/api/src/baskfy_api/portfolios.py services/api/tests/test_api_portfolios_tree.py >/dev/null 2>&1 && uv run mypy services/api/src/baskfy_api/routers/portfolios.py services/api/src/baskfy_api/portfolios.py >/dev/null 2>&1 && echo GATE_OK || echo GATE_FAILED
  EXPECT: GATE_OK
  EVIDENCE: Success: no issues found in 2 source files | GATE_OK

- [x] G13: the holdings writer attributes the broker account itself — the 0019 trigger is a backstop, not the mechanism
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_api_portfolios_tree.py -m db -k "attributes or replacing" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ......                                                                   [100%] | 6 passed, 47 deselected in 3.38s
