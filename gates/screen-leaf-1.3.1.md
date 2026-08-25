# Gates: 1.3.1 Diet, encodings, tabular nums

Scope: result-columns, cell-encodings, column-display (+ tests).

- [x] G1: default diet hides series, marketcap, ma_200, beta, sharpe_12m
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/lib/screens/__tests__/column-display.test.ts -t "hides legacy" 2>&1 | tail -10
  EXPECT: passed
  EVIDENCE: Start at  22:18:00 | Duration  756ms (transform 59ms, setup 80ms, collect 77ms, tests 1ms, environment 350ms, prepare 64ms)

- [x] G2: ScoreBar / ReturnChip / BumpinessDots / RankBadge exist and use tabular-nums
  CHECK: rg -n "tabular-nums" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens/cell-encodings.tsx
  EXPECT: tabular-nums
  EVIDENCE: 151:    return <span className="w-full text-right tabular-nums text-muted-foreground">{rank}</span>; | 164:        "inline-flex size-6 items-center justify-center rounded-full text-xs font-semibold ta

- [x] G3: symbol+name merged in result-columns
  CHECK: rg -n "truncate font-medium" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens/result-columns.tsx
  EXPECT: truncate font-medium
  EVIDENCE: 252:              <div className="truncate font-medium">{symbol}</div>

- [x] G4: bumpiness % available (visible or title/hover)
  CHECK: rg -n "title=" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens/cell-encodings.tsx
  EXPECT: formatFraction
  EVIDENCE: 127:      title={Number.isFinite(value) ? formatFraction(value) : undefined}

- [ ] G5: 7-day sparkline — only if preview already has a series; otherwise ABANDON
  EVIDENCE: pending

ABANDON: G5 Preview payload has no 7-day close series; a sparkline column would require a new API field or N+1 history fetches. Reversible if preview grows a sparkline.
