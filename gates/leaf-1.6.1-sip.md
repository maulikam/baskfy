# Gates: leaf-1.6.1-sip

Scope: SIP REMINDER mode only; no AUTO orders

- [x] G1: no AUTO execution path
  CHECK: rg -n 'mode.*AUTO|AUTO.*order' decile-blueprint/packages/core/src/baskfy_core/*sip* 2>/dev/null || echo none
  EXPECT: none
  EVIDENCE: none

<!-- integrity: security, performance, memory, accuracy required -->
