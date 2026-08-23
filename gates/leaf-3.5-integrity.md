# Gates: leaf-3.5-integrity

Scope: Unlock does not add web execute; fee accuracy still holds

- [x] G1: brokers router has no place_order
  CHECK: rg -n 'place_order|OrderGateway|confirm=true' decile-blueprint/services/api/src/baskfy_api/routers/brokers.py; echo exit:$?
  EXPECT: exit:1
  EVIDENCE: rg_exit:1 (2026-08-23T01:18Z)

- [x] G2: fee tests
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_accounting.py -q --tb=no -k fee 2>&1 | tail -1
  EXPECT: [100%]
  EVIDENCE: ........... [100%] (2026-08-23T01:18Z)

<!-- integrity: security, performance, memory, accuracy required -->
