# Gates: A — model layer (integration)

Scope: children A1 (schema), A2 (graph), A3 (units) merged into one working model layer.

- [x] N1: every child leaf's gates file is fully checked, no pending evidence
  CHECK: node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs --status gates/pm-leaf-A1-schema.md gates/pm-leaf-A2-graph.md gates/pm-leaf-A3-units.md 2>&1 | tail -6
  EXPECT: ALL MET
  EVIDENCE: gates/pm-leaf-A3-units.md: 10 gates | ALL MET (33 met)

- [x] N2: the pure layer composes with the schema — graph functions accept rows shaped like the new ORM columns
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest packages/core/tests/test_portfolio_graph.py packages/core/tests/test_portfolio_units.py packages/core/tests/test_portfolio_schema.py --tb=line 2>&1 | tail -4
  EXPECT: /^\d+ passed/m
  EVIDENCE: ..............                                                           [100%] | 158 passed in 0.33s

- [x] N3: Law 1 holds across packages/core after this merge — no network/disk client anywhere in core, importing it opens no socket, and the two new pure modules carry no I/O token
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && NET=$(grep -rlE 'import (httpx|requests)|from (httpx|requests)|kiteconnect|urllib\.request' packages/core/src/baskfy_core/ 2>/dev/null | wc -l | tr -d ' '); IO=$(grep -cE 'sqlalchemy|httpx|requests|kiteconnect|datetime\.now|utcnow|(^|[^A-Za-z0-9_])open\(|Session' packages/core/src/baskfy_core/portfolio_graph.py packages/core/src/baskfy_core/portfolio_units.py 2>/dev/null | awk -F: '{s+=$2} END {print s+0}'); IMP=$(uv run python -c "import socket;_r=socket.socket.connect;socket.socket.connect=lambda *a,**k:(_ for _ in ()).throw(AssertionError('net'));import baskfy_core,baskfy_core.portfolio_graph,baskfy_core.portfolio_units;socket.socket.connect=_r;print('ok')" 2>/dev/null); echo "NET=$NET IO=$IO IMP=$IMP"; [ "$NET" = 0 ] && [ "$IO" = 0 ] && [ "$IMP" = ok ] && echo GATE_OK || echo GATE_FAILED
  EXPECT: GATE_OK
  EVIDENCE: NET=0 IO=0 IMP=ok | GATE_OK

- [x] N4: the full core suite passes — no sibling regression
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest packages/core --tb=line 2>&1 | tail -4
  EXPECT: /^\d+ passed/m
  EVIDENCE: ..............................................                           [100%] | 1556 passed, 2 skipped in 80.72s (0:01:20)

- [x] N5: core coverage still clears its documented floor (>= 90%)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest packages/core --cov=baskfy_core --cov-report=term 2>&1 | grep -E "TOTAL" | tail -2
  EXPECT: /TOTAL/
  EVIDENCE: TOTAL                                                        7964   1486   1978    253    79%
