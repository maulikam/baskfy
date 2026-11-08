# Gates: leaf-1.8.2-sec-tenant

Scope: Cross-user watchlist/investment isolation

- [x] G1: isolation test exists
  CHECK: rg -l 'tenant|sole_user|another.user|cross.user' decile-blueprint/services/api/tests -g '*curated*' -g '*explore*' -g '*invest*' 2>/dev/null | head -1
  EXPECT: test
  EVIDENCE: 2026-08-23 — `test_curated_tenant_isolation.py` (5 passed [100%]).
  Track A sole-tenant: `scoped_sole_user_id` + `watchlist_items_for_user_stmt` /
  `investments_for_user_stmt` always `user_id == sole`; foreign principal collapses to
  sole; another user id cannot see sole watchlist rows.

<!-- integrity: security, performance, memory, accuracy required -->
