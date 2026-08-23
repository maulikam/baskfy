# Gates: leaf-2.3-dividends

Scope: Pure dividend derivation from CA × holdings history

- [x] G1: dividend tests
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_dividends.py -q --tb=no 2>&1 | tail -1
  EXPECT: [100%]
  EVIDENCE: ........... [100%] (11 passed) 2026-08-23; derive_dividends CA×holdings; TATASTEEL RECOVERED-ACTIONS ex-dates; money() half-up; source=CORPORATE_ACTIONS

<!-- integrity: security, performance, memory, accuracy required -->
