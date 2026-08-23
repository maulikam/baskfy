# Gates: leaf-4.3-peers

Scope: More brokers have authorize URL shapes wired

- [x] G1: more than one adapter_wired
  CHECK: cd decile-blueprint && uv run python -c "from baskfy_api.routers.brokers import _WIRED_AUTHORIZE; print(len(_WIRED_AUTHORIZE))"
  EXPECT: /[2-9]|[1-9][0-9]/
  EVIDENCE: 5 — angelone,dhan,fyers,upstox,zerodha (2026-08-23T04:06Z)

<!-- integrity: security, performance, memory, accuracy required -->
