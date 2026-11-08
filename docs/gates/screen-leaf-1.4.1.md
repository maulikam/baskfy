# Gates: 1.4.1 Microcopy + motion + type (e2e copy)

Scope: result-cards stagger (already), e2e/screens.spec.ts + helpers. Do not edit nav.

- [x] G1: e2e still finds chip-index / filter-chip-bar
  CHECK: rg -n "chip-index|filter-chip-bar" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/e2e
  EXPECT: chip-index
  EVIDENCE: /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/e2e/critical-journeys.spec.ts:158:  await page.getByTestId("chip-index").click(); | /Users/maulikdave/Documents/projects/baskfy/de

- [x] G2: e2e asserts demo / duplicate or story if those testids exist
  CHECK: rg -n "demo-banner|story-strip|Duplicate|share-screen" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/e2e/screens.spec.ts
  EXPECT: share-screen
  EVIDENCE: 130:      page.getByTestId("demo-banner").or(page.getByRole("button", { name: /Duplicate/i })).first(), | 136:    await expect(page.getByTestId("story-strip")).toBeVisible();

- [x] G3: result-cards stagger uses motion-safe and delay cap 300ms
  CHECK: rg -n "motion-safe|300ms" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens/result-cards.tsx
  EXPECT: 300ms
  EVIDENCE: 218:                  "motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-bottom-1 motion-safe:duration-200", | 223:                      animationDelay: `min(${item.index * 18}ms, 3

- [x] G4: screens.spec.ts still compiles (tsc already covers; grep export-csv nested)
  CHECK: rg -n "export-csv|export-menu" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/e2e
  EXPECT: export
  EVIDENCE: /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/e2e/screens.spec.ts:102:    await page.getByTestId("export-menu").click(); | /Users/maulikdave/Documents/projects/baskfy/decile-bl
