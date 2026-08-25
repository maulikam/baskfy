# Gates: C — broker truth (integration)

Scope: children C1 (provenance) and C2 (catalog honesty) merged. After this branch, nothing in the product tells the user a broker syncs when it does not, or that live holdings are a fixture.

- [x] N1: both child gates files are fully checked, no pending evidence
  CHECK: node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs --status gates/pm-leaf-C1-provenance.md gates/pm-leaf-C2-capabilities.md 2>&1 | tail -5
  EXPECT: ALL MET
  EVIDENCE: gates/pm-leaf-C2-capabilities.md: 9 gates | ALL MET (20 met)

- [x] N2: catalog and wiring agree — every broker claiming ready has a wired adapter, and every wired adapter has a provenance path
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run python -c "from baskfy_core.broker_connections import broker_catalog; from baskfy_api.broker_holdings import _HOLDINGS_WIRED, holdings_for_broker; over=[b.id for b in broker_catalog() if b.capabilities.holdings_sync=='ready' and b.id not in _HOLDINGS_WIRED]; under=[b for b in _HOLDINGS_WIRED if getattr(holdings_for_broker(b),'source',None) is None]; print('AGREE_OK' if not over and not under else 'DISAGREE over='+str(over)+' under='+str(under))"
  EXPECT: AGREE_OK
  EVIDENCE: AGREE_OK

- [x] N3: an unwired broker end to end returns source='unwired' from the HTTP endpoint, with no order path touched
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest services/api/tests/test_broker_holdings_provenance.py services/api/tests/test_broker_holdings_sync.py services/api/tests/test_brokers.py --tb=line 2>&1 | tail -4
  EXPECT: /^\d+ passed/m
  EVIDENCE: ....                                                                     [100%] | 76 passed in 0.89s

- [x] N4: no sibling regression across core + api broker surfaces
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && set -a && . ./.env && set +a && uv run pytest packages/core/tests/test_broker_connections.py packages/core/tests/test_broker_capability_honesty.py services/api/tests/test_broker_oauth.py --tb=line 2>&1 | tail -4
  EXPECT: /^\d+ passed/m
  EVIDENCE: .......................                                                  [100%] | 23 passed in 0.47s
