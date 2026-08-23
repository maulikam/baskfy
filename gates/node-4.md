# Gates: node-4

Scope: Backlog Tree 4 integrated; Track C execute held

- [x] G1: no pending evidence on leaf-4.*
  CHECK: rg -n '^[[:space:]]*EVIDENCE: pending$' gates/leaf-4.*.md; echo exit:$?
  EXPECT: exit:1
  EVIDENCE: filled after parent verify (2026-08-23T04:06Z)

- [x] G2: OpenAPI no execute
  CHECK: cd decile-blueprint && uv run python -c "from baskfy_api.app import create_app; p=create_app().openapi()['paths']; bad=[(m,x) for x,o in p.items() for m in o if m in ('post','put','patch','delete') and 'execute' in x.lower()]; print('clean' if not bad else bad)"
  EXPECT: clean
  EVIDENCE: clean

ABANDON: push No origin remote — see NEEDS-MAULIK #14.

<!-- integrity: security, performance, memory, accuracy required -->
