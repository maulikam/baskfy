# Gates: leaf-4.2-holdings-live

Scope: Zerodha holdings fetch path (live when token+!DRY_RUN); still no orders

- [x] G1: holdings tests pass
  CHECK: cd decile-blueprint && uv run pytest services/api/tests/test_broker_holdings_sync.py -q --tb=no 2>&1 | tail -1
  EXPECT: [100%]
  EVIDENCE: parent — holdings suite in combined run [100%] (2026-08-23T04:06Z)

- [x] G2: live fetch helper exists
  CHECK: rg -n 'fetch_kite_holdings|portfolio/holdings' decile-blueprint/services/api/src/baskfy_api/broker_holdings.py | head -3
  EXPECT: /
  EVIDENCE: fetch_kite_holdings + parse_kite_holdings_payload (2026-08-23T04:06Z)

<!-- integrity: security, performance, memory, accuracy required -->
