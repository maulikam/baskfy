# Gates: A2 — portfolio_graph.py, the pure graph layer

Scope: cycle detection, depth cap, tree assembly and per-broker roll-up as pure functions over plain data. Law 1: no DB, no network, no clock, no disk.

- [x] G1: the module exists in packages/core and imports without touching I/O
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run python -c "import baskfy_core.portfolio_graph as g; print('IMPORT_OK'); print(sorted(n for n in dir(g) if not n.startswith('_')))"
  EXPECT: IMPORT_OK
  EVIDENCE: IMPORT_OK | ['AmountTypeError', 'BrokerLine', 'BrokerRollup', 'CycleError', 'Decimal', 'DepthExceededError', 'DuplicatePortfolioError', 'Final', 'Forest', 'Holding', 'Iterable', 'MAX_DEPTH', 'Orphan',

- [x] G2: Law 1 holds — no sqlalchemy, no httpx/requests, no datetime.now, no open() in the module source
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && grep -nE "sqlalchemy|httpx|requests|datetime\.now|utcnow|(^|[^A-Za-z0-9_])open\(|Session" packages/core/src/baskfy_core/portfolio_graph.py | head -5; echo "HITS=$(grep -cE "sqlalchemy|httpx|requests|datetime\.now|utcnow|(^|[^A-Za-z0-9_])open\(|Session" packages/core/src/baskfy_core/portfolio_graph.py || true)"
  EXPECT: HITS=0
  EVIDENCE: HITS=0

- [x] G3: a self-parent is rejected
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest packages/core/tests/test_portfolio_graph.py -k "self_parent" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ....                                                                     [100%] | 4 passed, 68 deselected in 0.09s

- [x] G4: a multi-node cycle (A→B→C→A) is rejected, not merely a two-node one
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest packages/core/tests/test_portfolio_graph.py -k "cycle" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: .........                                                                [100%] | 9 passed, 63 deselected in 0.06s

- [x] G5: depth beyond the cap (6) is rejected, depth exactly at the cap is accepted
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest packages/core/tests/test_portfolio_graph.py -k "depth" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: .................                                                        [100%] | 17 passed, 55 deselected in 0.06s

- [x] G6: tree assembly returns children under parents, and orphans (parent outside the set) surface rather than vanish
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest packages/core/tests/test_portfolio_graph.py -k "tree or assemble or orphan" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: .........................                                                [100%] | 25 passed, 47 deselected in 0.06s

- [x] G7: per-broker roll-up sums a subtree's holdings by broker_account_id, and a node whose children span brokers reports each broker separately plus a total
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest packages/core/tests/test_portfolio_graph.py -k "rollup or broker" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ......................                                                   [100%] | 22 passed, 50 deselected in 0.07s

- [x] G8: roll-up arithmetic is Decimal end to end — no float appears in any returned money or quantity
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest packages/core/tests/test_portfolio_graph.py -k "decimal or float" --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ..........                                                               [100%] | 10 passed, 62 deselected in 0.06s

- [x] G9: the whole module's suite passes and the count is measured, not remembered
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest packages/core/tests/test_portfolio_graph.py --tb=line 2>&1 | tail -3
  EXPECT: /^\d+ passed/m
  EVIDENCE: ........................................................................ [100%] | 72 passed in 0.07s

- [x] G10: lint + mypy strict clean, no escape hatches
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run ruff check packages/core/src/baskfy_core/portfolio_graph.py packages/core/tests/test_portfolio_graph.py 2>&1 | tail -2 && uv run mypy packages/core/src/baskfy_core/portfolio_graph.py 2>&1 | tail -2 && uv run pytest packages/core/tests/test_no_escape_hatches.py --tb=line 2>&1 | tail -2
  EXPECT: /Success: no issues/
  EVIDENCE: ........                                                                 [100%] | 8 passed in 0.29s
