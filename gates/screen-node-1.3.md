# Gates: 1.3 Glanceable table (integration)

Scope: children 1.3.1 + 1.3.2 — encodings + mini factsheet

- [x] N1: child leaves fully checked
  CHECK: node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs --status gates/screen-leaf-1.3.1.md gates/screen-leaf-1.3.2.md
  EXPECT: ALL MET
  EVIDENCE: gates/screen-leaf-1.3.2.md: 5 gates | ALL MET (8 met, 2 abandoned)

- [x] N2: encodings used in both table and drawer
  CHECK: rg -n "ScoreBar|ReturnChip|BumpinessDots" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens/result-columns.tsx /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens/peek-drawer.tsx
  EXPECT: ScoreBar
  EVIDENCE: /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens/peek-drawer.tsx:159:                  <ScoreBar value={score} /> | /Users/maulikdave/Documents/projects/bas

- [x] N3: column-display + peek tests pass
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/lib/screens/__tests__/column-display.test.ts src/components/screens/__tests__/peek-drawer.test.tsx 2>&1 | tail -15
  EXPECT: passed
  EVIDENCE: Start at  22:18:05 | Duration  1.33s (transform 118ms, setup 270ms, collect 313ms, tests 410ms, environment 919ms, prepare 150ms)
