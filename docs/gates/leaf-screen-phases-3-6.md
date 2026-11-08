# Gates: Screen redesign Phases 3–6

Scope: Chip bar, story strip, drawer/microcopy/motion, share card + mobile cards; unit + e2e green.

- [x] G1: Chip bar replaces left rail when `NEXT_PUBLIC_SCREEN_CHIP_FILTERS` is on (default)
  CHECK: rg -n "FilterChipBar|NEXT_PUBLIC_SCREEN_CHIP_FILTERS" decile-blueprint/apps/web/src/components/screens/
  EXPECT: matches in screen-editor and chip-bar (or filter-chip-bar)
  EVIDENCE: filter-chip-bar.tsx exports FilterChipBar; screen-editor.tsx imports it; feature-flags.ts defaults ON

- [x] G2: Story strip + stat tiles exist and derive from results/definition
  CHECK: rg -n "StoryStrip|story-strip|buildStorySentence" decile-blueprint/apps/web/src/
  EXPECT: lib + component
  EVIDENCE: lib/screens/story.ts + story-strip.tsx + story.test.ts (4 passed)

- [x] G3: Peek drawer is mini factsheet (hero + encodings + All numbers + factsheet link)
  CHECK: rg -n "All numbers|Open the full factsheet|peek-drawer" decile-blueprint/apps/web/src/components/screens/peek-drawer.tsx
  EXPECT: All numbers + Open the full factsheet
  EVIDENCE: peek-drawer.tsx lines 162 / factsheet link / ScoreBar+ReturnChip+BumpinessDots

- [x] G4: Share card + Export menu hierarchy
  CHECK: rg -n "ShareButton|share-card|data-testid=\"share" decile-blueprint/apps/web/src/components/screens/
  EXPECT: share component + testid
  EVIDENCE: share-button.tsx data-testid=share-screen; export-button.tsx menu with export-csv nested

- [x] G5: Mobile cards below ~700px
  CHECK: rg -n "ResultCards|min-\\[700px\\]" decile-blueprint/apps/web/src/components/screens/
  EXPECT: mobile card layout present
  EVIDENCE: result-cards.tsx; results-panel min-[700px]:hidden / :block split; peek bottom sheet <700px

- [x] G6: Screen lib vitest passes
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/lib/screens/__tests__/ 2>&1 | tail -12
  EXPECT: passed
  EVIDENCE: 4 files, 39 tests passed (2026-08-23)

- [x] G7: E2e screens journey updated for chip UI and still runnable
  CHECK: rg -n "chip|index-select|FilterChip|add-filter" decile-blueprint/apps/web/e2e/screens.spec.ts
  EXPECT: chip-aware selectors or preserved testids
  EVIDENCE: screens.spec + sentinels + url-state use helpers/screen-chips.ts; export-menu → export-csv

- [ ] G8: Full make e2e (or documented blocker)
  CHECK: cd decile-blueprint && make e2e 2>&1 | tail -40
  EXPECT: passed OR ABANDON with reason
  EVIDENCE: ABANDON — agent sandbox blocks Playwright Chromium (SIGSEGV / missing browser binaries; `required_permissions: all` not granted). App + API webServers started successfully after `--hostname 127.0.0.1` + TS fixes. Re-run locally: `cd decile-blueprint && make e2e`.

ABANDON: G8 Cursor agent sandbox cannot launch Playwright Chromium (SEGV_ACCERR / EPERM on kill; browser path wants mac-x64 while host has arm64). Not a product defect. Unit tests green; e2e specs updated for chip UI.
