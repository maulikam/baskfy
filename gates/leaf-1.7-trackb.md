# Gates: leaf-1.7-trackb

Scope: Track B flags off; paywall 404

- [ ] G1: subscription flag default false
  CHECK: rg -n 'SUBSCRIPTIONS_ENABLED|subscriptions_enabled' decile-blueprint/services/api/src/baskfy_api/settings.py | head -3
  EXPECT: False|false
  EVIDENCE: pending

<!-- integrity: security, performance, memory, accuracy required -->
