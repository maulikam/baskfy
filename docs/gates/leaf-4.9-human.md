# Gates: leaf-4.9-human

Scope: D7/D10/counsel recorded as UNREVIEWED or NEEDS

- [x] G1: D7 entry exists
  CHECK: rg -n '## D7 ' docs/DECISIONS-MERGE.md | head -2
  EXPECT: /
  EVIDENCE: ## D7 — Free vs paid (2026-08-23T04:06Z)

- [x] G2: D10 or NEEDS entry
  CHECK: rg -n 'D10|display licensing' docs/DECISIONS-MERGE.md NEEDS-MAULIK.md | head -3
  EXPECT: /
  EVIDENCE: ## D10 — Market-data display licensing (2026-08-23T04:06Z)

<!-- integrity: security, performance, memory, accuracy required -->
