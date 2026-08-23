# Gates: leaf-1.8.2-sec-tenant

Scope: Cross-user watchlist/investment isolation

- [ ] G1: isolation test exists
  CHECK: rg -l 'tenant|sole_user|another.user|cross.user' decile-blueprint/services/api/tests -g '*curated*' -g '*explore*' -g '*invest*' 2>/dev/null | head -1
  EXPECT: test
  EVIDENCE: pending

<!-- integrity: security, performance, memory, accuracy required -->
