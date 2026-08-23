# Gates: leaf-4.10-integrity

Scope: No execute; OAuth still signed; fee accuracy

- [x] G1: fee tests
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_accounting.py -q --tb=no -k fee 2>&1 | tail -1
  EXPECT: [100%]
  EVIDENCE: ...........                                                              [100%]
- [x] G2: signed_off still True
  CHECK: cd decile-blueprint && uv run python -c "from baskfy_core.broker_connections import BROKER_OAUTH_REVIEW; print(BROKER_OAUTH_REVIEW.signed_off)"
  EXPECT: True
  EVIDENCE: True

<!-- integrity: security, performance, memory, accuracy required -->
