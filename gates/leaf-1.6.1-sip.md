# Gates: leaf-1.6.1-sip

Scope: SIP REMINDER mode only; no AUTO orders

- [x] G1: sip domain + no AUTO write path
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_sip.py -q --tb=no 2>&1 | tail -1
  EXPECT: [100%]
  EVIDENCE: 19 passed [100%] 2026-08-23T00:25Z; REMINDER-only write path (AUTO refused); holiday roll-forward; fire key plan_id:YYYY-MM; pause/resume; SIP_DUE pending dict

<!-- integrity: security, performance, memory, accuracy required -->

