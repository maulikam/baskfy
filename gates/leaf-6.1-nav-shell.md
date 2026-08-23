# Gates: leaf-6.1 — Nav shell + IA (Market · Baskets · Build · Me)

Scope: Collapse primary consumer nav to 4 items; desktop sliding-pill; mobile bottom tab bar; a11y.

- [x] G1: `PRIMARY_NAV` has exactly 4 ready items labeled Market, Baskets, Build, Me
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/__tests__/nav.test.ts -t "four primary" 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: vitest 13 passed incl. "exposes exactly four primary destinations"

- [x] G2: TopNav renders `aria-label="Primary"` and `aria-current="page"` on active
  CHECK: rg -n 'aria-label="Primary"|aria-current' decile-blueprint/apps/web/src/components/shell/top-nav.tsx
  EXPECT: Primary
  EVIDENCE: top-nav.tsx aria-label="Primary"; aria-current on active Link

- [x] G3: Mobile bottom tab bar component exists with safe-area padding
  CHECK: rg -n "safe-area-inset-bottom" decile-blueprint/apps/web/src/components/shell/bottom-tab-bar.tsx
  EXPECT: safe-area-inset-bottom
  EVIDENCE: bottom-tab-bar.tsx paddingBottom env(safe-area-inset-bottom)

- [x] G4: Sliding active pill on desktop primary nav
  CHECK: rg -n "data-active-pill|transition-\[left,width\]" decile-blueprint/apps/web/src/components/shell/top-nav.tsx
  EXPECT: data-active-pill
  EVIDENCE: absolute marker-control track with left/width transition

- [x] G5: Theme toggle and search keep aria-labels
  CHECK: rg -n 'aria-label' decile-blueprint/apps/web/src/components/shell/theme-toggle.tsx decile-blueprint/apps/web/src/components/shell/search-button.tsx
  EXPECT: aria-label
  EVIDENCE: Theme "Change theme"; Search "Search stocks, indices, baskets, and screens"
