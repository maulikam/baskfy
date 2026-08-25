# Gates: 1.3.2 Mini-factsheet drawer

Scope: peek-drawer.tsx + tests.

- [x] G1: drawer has All numbers + Open the full factsheet
  CHECK: rg -n "All numbers|Open the full factsheet" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens/peek-drawer.tsx
  EXPECT: All numbers
  EVIDENCE: 186:                All numbers | 219:                Open the full factsheet

- [x] G2: encodings reused (ScoreBar, ReturnChip, BumpinessDots)
  CHECK: rg -n "ScoreBar|ReturnChip|BumpinessDots" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens/peek-drawer.tsx
  EXPECT: ScoreBar
  EVIDENCE: 159:                  <ScoreBar value={score} /> | 168:                    <BumpinessDots value={vol} />

- [x] G3: motion-safe duration ≤250ms; Esc via Dialog (radix)
  CHECK: rg -n "duration-200|Dialog" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens/peek-drawer.tsx
  EXPECT: duration-200
  EVIDENCE: 225:      </DialogPrimitive.Portal> | 226:    </Dialog>

- [x] G4: peek-drawer tests pass
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/components/screens/__tests__/peek-drawer.test.tsx 2>&1 | tail -12
  EXPECT: passed
  EVIDENCE: Start at  22:16:59 | Duration  5.92s (transform 330ms, setup 457ms, collect 1.04s, tests 643ms, environment 2.29s, prepare 364ms)

- [ ] G5: 1-year mini chart — only if a series is already on the row; otherwise ABANDON
  EVIDENCE: pending

ABANDON: G5 Preview rows have no 1-year series; a chart would N+1 the history endpoint. Reversible later if preview grows a sparkline field.
