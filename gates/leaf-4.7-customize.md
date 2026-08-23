# Gates: leaf-4.7-customize

Scope: Customize constituents path (API or page)

- [x] G1: customize surface exists
  CHECK: rg -n 'CUSTOMIZE|customize' decile-blueprint/services/api/src/baskfy_api -g '*.py' | head -5
  EXPECT: /
  EVIDENCE: curated_customize.py POST customize (2026-08-23T04:06Z)

<!-- integrity: security, performance, memory, accuracy required -->
