# Gates: leaf-1.2.1-versions

Scope: Publish version + diff holdings vs target

- [ ] G1: version domain tests
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_versions.py -q --tb=no 2>&1 | tail -1
  EXPECT: [100%]
  EVIDENCE: pending

<!-- integrity: security, performance, memory, accuracy required -->
