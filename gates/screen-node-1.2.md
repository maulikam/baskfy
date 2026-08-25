# Gates: 1.2 Frame reshape (integration)

Scope: children 1.2.1 + 1.2.2 — chips + story + one primary action

- [x] N1: child leaves fully checked
  CHECK: node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs --status gates/screen-leaf-1.2.1.md gates/screen-leaf-1.2.2.md
  EXPECT: ALL MET
  EVIDENCE: gates/screen-leaf-1.2.2.md: 5 gates | ALL MET (9 met)

- [x] N2: editor mounts chip bar + story + duplicate primary for read-only
  CHECK: rg -n "FilterChipBar|StoryStrip|Duplicate" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens/screen-editor.tsx
  EXPECT: FilterChipBar
  EVIDENCE: 250:      <StoryStrip | 260:          <FilterChipBar {...filterProps} />

- [x] N3: story + chip tests pass
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/lib/screens/__tests__/story.test.ts src/lib/screens/__tests__/universe-label.test.ts 2>&1 | tail -15
  EXPECT: passed
  EVIDENCE: Start at  22:18:03 | Duration  1.54s (transform 185ms, setup 240ms, collect 433ms, tests 359ms, environment 856ms, prepare 172ms)
