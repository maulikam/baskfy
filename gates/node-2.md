# Gates: node-2

Scope: AC-closure tree integrated; Track C held

- [x] G1: all leaf-2.* gates checked (no pending evidence)
  CHECK: rg -n '^[[:space:]]*EVIDENCE: pending$' gates/leaf-2.*.md; echo exit:$?
  EXPECT: exit:1
  EVIDENCE: parent — no EVIDENCE: pending lines on leaf-2.*; all boxes [x]

- [x] G2: OpenAPI still clean of execute routes
  CHECK: cd decile-blueprint && uv run python -c "from baskfy_api.app import create_app; p=create_app().openapi()['paths']; bad=[(m,x) for x,o in p.items() for m in o if m in ('post','put','patch','delete') and any(w in x.lower() for w in ('execute','place_order','/order'))]; print('clean' if not bad else bad)"
  EXPECT: clean
  EVIDENCE: clean (parent re-check)

# D3 OAuth unlock moved to Tree 3 (gates/node-3.md) — no longer abandoned.

<!-- integrity: security, performance, memory, accuracy required -->
