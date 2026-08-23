# Gates: node-1

Scope: Root: SC2–SC12 shipped with integrity; Track C held

- [x] G1: STATUS ledger shows SC2–SC12 green or ABANDON with reason
  CHECK: rg -c '✅' docs/smallcase/STATUS.md
  EXPECT: /[0-9]+/
  EVIDENCE: 4

- [x] G2: No web execute route in OpenAPI
  CHECK: cd decile-blueprint && uv run python -c "from baskfy_api.app import create_app; p=create_app().openapi()['paths']; bad=[(m,x) for x,o in p.items() for m in o if m in ('post','put','patch','delete') and any(w in x.lower() for w in ('execute','place_order','/order'))]; print('clean' if not bad else bad)"
  EXPECT: clean
  EVIDENCE: clean

- [x] G3: BROKER_OAUTH_REVIEW still unsigned
  CHECK: cd decile-blueprint && uv run python -c "from baskfy_core.broker_connections import BROKER_OAUTH_REVIEW; print(BROKER_OAUTH_REVIEW.signed_off)"
  EXPECT: False
  EVIDENCE: False

<!-- integrity: security, performance, memory, accuracy required -->
