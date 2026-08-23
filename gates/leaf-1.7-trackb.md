# Gates: leaf-1.7-trackb

Scope: Track B flags off; paywall 404

- [x] G1: subscription flag default false
  CHECK: rg -n 'SUBSCRIPTIONS_ENABLED|subscriptions_enabled' decile-blueprint/services/api/src/baskfy_api/settings.py | head -3
  EXPECT: False|false
  EVIDENCE: `subscriptions_enabled: bool = False` (+ `fee_collection_enabled` / `public_signup_enabled` default False). Dark routes `/cb/paywall`, `/cb/public-signup`, `/cb/fees/collect` raise Problem 404 while off. Free Access via `track_b.free_access`. Tests: `tests/test_track_b_gates.py`.


<!-- integrity: security, performance, memory, accuracy required -->
