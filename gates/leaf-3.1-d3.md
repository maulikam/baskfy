# Gates: leaf-3.1-d3

Scope: Written D3 posture B; gate flipped

- [x] G1: signed_off True with decision_reference
  CHECK: cd decile-blueprint && uv run python -c "from baskfy_core.broker_connections import BROKER_OAUTH_REVIEW; assert BROKER_OAUTH_REVIEW.signed_off; assert BROKER_OAUTH_REVIEW.decision_reference; print('ok', BROKER_OAUTH_REVIEW.decision_reference)"
  EXPECT: ok
  EVIDENCE: ok DECISIONS-MERGE.md §D3 (2026-08-23T01:18Z)

- [x] G2: broker connection tests pass
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_broker_connections.py services/api/tests/test_brokers.py -q --tb=no 2>&1 | tail -1
  EXPECT: [100%]
  EVIDENCE: ........... [100%] (2026-08-23T01:18Z)

<!-- integrity: security, performance, memory, accuracy required -->
