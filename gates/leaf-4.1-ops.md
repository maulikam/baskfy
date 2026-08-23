# Gates: leaf-4.1-ops

Scope: Junk removed; STATUS mentions D3/Tree3; remote honesty

- [x] G1: diag junk gone
  CHECK: test ! -f decile-blueprint/apps/web/.diag.mjs && test ! -f decile-blueprint/apps/web/.e2ebt.mjs && echo clean
  EXPECT: clean
  EVIDENCE: clean (2026-08-23T04:06Z); gitignored

- [x] G2: STATUS mentions D3 unlock
  CHECK: rg -n 'D3|Tree 3|OAuth unlock' docs/smallcase/STATUS.md | head -3
  EXPECT: /
  EVIDENCE: Run state + Tree 3 / D3 unlock section (2026-08-23T04:06Z)

- [x] G3: remote situation recorded
  CHECK: rg -n 'origin|remote|ABANDON.*push' NEEDS-MAULIK.md | head -5
  EXPECT: /
  EVIDENCE: NEEDS-MAULIK #14 Git remote (2026-08-23T04:06Z)

ABANDON: push No git origin on this clone — cannot invent a remote URL. Recorded as NEEDS-MAULIK #14.

<!-- integrity: security, performance, memory, accuracy required -->
