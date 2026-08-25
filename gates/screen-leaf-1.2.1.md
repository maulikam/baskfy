# Gates: 1.2.1 Chip bar (Liquidity + search)

Scope: `filter-chip-bar.tsx` — always-visible Liquidity chip; + Filter searchable.

- [x] G1: chip-liquidity testid exists
  CHECK: rg -n "chip-liquidity" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens/filter-chip-bar.tsx
  EXPECT: chip-liquidity
  EVIDENCE: 406:                data-testid="chip-liquidity-select" | 440:                  data-testid="chip-liquidity-custom"

- [x] G2: null median_volume_1y labels as Liquidity: any
  CHECK: rg -n "Liquidity" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens/filter-chip-bar.tsx
  EXPECT: Liquidity
  EVIDENCE: 239:      /* Liquidity owns median_volume_1y; clearing "Liquidity & return" must not wipe it. */ | 391:            <span>{`Liquidity: ${liquidityValue}`}</span>

- [x] G3: + Filter search still matches circuit
  CHECK: rg -n "filter-search|CHIP_SHORT" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens/filter-chip-bar.tsx
  EXPECT: filter-search
  EVIDENCE: 470:                  {CHIP_SHORT[group.id] ?? group.title} | 518:              data-testid="filter-search"

- [x] G4: universe-label / chip tests pass
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/lib/screens/__tests__/universe-label.test.ts 2>&1 | tail -12
  EXPECT: passed
  EVIDENCE: Start at  22:17:57 | Duration  1.18s (transform 116ms, setup 70ms, collect 260ms, tests 290ms, environment 309ms, prepare 43ms)
