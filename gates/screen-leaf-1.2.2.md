# Gates: 1.2.2 Story strip + action hierarchy

Scope: story-strip, story.ts, screen-editor, apply-filters-pill.

- [x] G1: story sentence names count + universe + ranking in plain English
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/lib/screens/__tests__/story.test.ts 2>&1 | tail -12
  EXPECT: passed
  EVIDENCE: Tests  6 passed (6) | Start at  22:24:21 | Duration  3.72s (transform 217ms, setup 303ms, collect 204ms, tests 60ms, environment 2.11s, prepare 248ms)

- [x] G2: read-only example primary is Duplicate & make it yours
  CHECK: rg -n "Duplicate" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens/screen-editor.tsx
  EXPECT: Duplicate
  EVIDENCE: 179:              Duplicate & make it yours | 212:            This is a demo screen — look, poke, sort. Duplicate it to make it yours.

- [x] G3: Apply pill only when dirty (component returns null at changeCount<=0)
  CHECK: rg -n "changeCount <= 0" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens/apply-filters-pill.tsx
  EXPECT: changeCount <= 0
  EVIDENCE: 22:  if (changeCount <= 0) return null;

- [x] G4: story tiles do not use a heavy per-tile border (whitespace / muted fill)
  CHECK: rg -n "border-border" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens/story-strip.tsx || echo NONE
  EXPECT: NONE
  EVIDENCE: NONE

- [x] G5: demo banner copy matches the brief
  CHECK: rg -n "This is a demo screen" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens/screen-editor.tsx
  EXPECT: This is a demo screen
  EVIDENCE: 212:            This is a demo screen — look, poke, sort. Duplicate it to make it yours.
