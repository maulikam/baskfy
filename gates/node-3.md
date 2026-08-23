# Gates: node-3

Scope: D3 written; OAuth unlock; Track C execute still held

- [x] G1: D3 decision exists
  CHECK: rg -n '## D3 |posture B' docs/DECISIONS-MERGE.md | head -3
  EXPECT: /
  EVIDENCE: ## D3 — regulatory posture: B (2026-08-23T01:18Z)

- [x] G2: OAuth review signed with reference
  CHECK: cd decile-blueprint && uv run python -c "from baskfy_core.broker_connections import BROKER_OAUTH_REVIEW; print(BROKER_OAUTH_REVIEW.signed_off, BROKER_OAUTH_REVIEW.decision_reference or 'NONE')"
  EXPECT: True
  EVIDENCE: True DECISIONS-MERGE.md §D3 (2026-08-23T01:18Z)

- [x] G3: OpenAPI still has no execute order route
  CHECK: cd decile-blueprint && uv run python -c "from baskfy_api.app import create_app; p=create_app().openapi()['paths']; bad=[(m,x) for x,o in p.items() for m in o if m in ('post','put','patch','delete') and any(w in x.lower() for w in ('execute','place_order'))]; print('clean' if not bad else bad)"
  EXPECT: clean
  EVIDENCE: clean (2026-08-23T01:18Z)

<!-- integrity: security, performance, memory, accuracy required -->
