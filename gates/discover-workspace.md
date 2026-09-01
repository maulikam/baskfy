# Gates: Baskets → Discover workspace (tree 11, solo)

Scope: the slice of `PLAN-DISCOVER.md` executed in this run — N1 truth pass and defect fixes,
N2 information architecture, N3 the card, N4 save + select, N5 compare, N6 Discover composition,
N8 disclosure placement, N9 the metrics gap register. N7 (risk-return explorer) is attempted last.

Every gate below is about behaviour a person can see, or an invariant that keeps the product
honest. Percentages, advice language and invented metrics are gated, not trusted.

## N1 — truth pass

- [x] G1: every one of the brief's 16 stated UX problems has a written verdict backed by file:line
  CHECK: echo "CLAIMS=$(grep -cE '^\| C[0-9]+ ' docs/DISCOVER-AUDIT.md) CITED=$(grep -E '^\| C[0-9]+ ' docs/DISCOVER-AUDIT.md | grep -cE '\.(tsx|ts|py)')"
  EXPECT: /CLAIMS=16 CITED=16/
  EVIDENCE: CLAIMS=16 CITED=16

- [x] G2: a return renders with its unit wherever it comes back from the API as a string
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/lib/discover/__tests__/metrics.test.ts 2>&1 | grep -E "Tests +[0-9]+ passed" | tail -1
  EXPECT: /Tests +[1-9][0-9]* passed(?!.*failed)/
  EVIDENCE: Tests  17 passed (17)

- [x] G3: no surface labels a risk figure "Swing"; the risk measure names itself and says what it is
  CHECK: echo "SWING=$(grep -rniE 'swing' src --include='*.tsx' --include='*.ts' | grep -v __tests__ | grep -vE ':[0-9]+: *(\*|//|/\*)' | grep -viE 'lower-swing|smaller swings|bigger swings' | wc -l | tr -d ' ')"
  EXPECT: SWING=0
  EVIDENCE: SWING=0

- [x] G4: the December-2026 announcement links to the December-2026 page, not to the blog index
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && grep -c 'href: "/december-2026-update"' "src/app/(app)/layout.tsx"
  EXPECT: /^[1-9]/m
  EVIDENCE: 1

## N2 — information architecture

- [x] G5: the hub is Discover — primary nav, section tabs and page vocabulary all agree
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/lib/__tests__/nav.test.ts src/lib/discover 2>&1 | grep -E "Tests +[0-9]+ passed" | tail -1
  EXPECT: /Tests +[1-9][0-9]* passed(?!.*failed)/
  EVIDENCE: Tests  124 passed (124)

- [x] G6: every old `/baskets*` path still resolves, and holds nothing but a redirect
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && node scripts/check-shadowed-routes.mjs 2>&1 | tail -1
  EXPECT: /^ok/
  EVIDENCE: ok — 22 redirected source(s), no page file shadowed by any of them

- [x] G7: Create appears under Build and no longer inside the Discover hub
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && node -e "const s=require('fs').readFileSync('src/lib/nav.ts','utf8');const d=s.slice(s.indexOf('discover: ['),s.indexOf(']',s.indexOf('discover: [')));const b=s.slice(s.indexOf('build: ['),s.indexOf(']',s.indexOf('build: [')));console.log('DISCOVER_HAS_CREATE='+/create/.test(d),'BUILD_HAS_CREATE='+/create/.test(b))"
  EXPECT: DISCOVER_HAS_CREATE=false BUILD_HAS_CREATE=true
  EVIDENCE: DISCOVER_HAS_CREATE=false BUILD_HAS_CREATE=true

## N3/N4 — the card, save and select

- [x] G8: the card answers all four questions — what it does, how it performed, what can go wrong, what to do next
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/components/discover/__tests__/basket-card.test.tsx 2>&1 | grep -E "Tests +[0-9]+ passed" | tail -1
  EXPECT: /Tests +[1-9][0-9]* passed(?!.*failed)/
  EVIDENCE: Tests  20 passed (20)

- [x] G9: no two-letter monogram is rendered as a basket's identity anywhere
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && echo "MONOGRAM=$(grep -rn 'function monogram' src --include=*.tsx --include=*.ts | wc -l | tr -d ' ')"
  EXPECT: MONOGRAM=0
  EVIDENCE: MONOGRAM=0

- [x] G10: selection is capped at three, survives a reload, and holds slugs only
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/lib/discover/__tests__/selection.test.ts 2>&1 | grep -E "Tests +[0-9]+ passed" | tail -1
  EXPECT: /Tests +[1-9][0-9]* passed(?!.*failed)/
  EVIDENCE: Tests  19 passed (19)

- [x] G11: Save writes through the watchlist API that already exists — no second store
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && echo "WATCHLIST_CALLS=$(grep -rn '/watchlist' src/lib/discover src/components/discover --include=*.ts --include=*.tsx | grep -v __tests__ | wc -l | tr -d ' ')"
  EXPECT: /WATCHLIST_CALLS=[1-9]/
  EVIDENCE: WATCHLIST_CALLS=3

## N5 — compare

- [x] G12: two or three baskets compare like for like, and a metric nobody computes reads as absent rather than as zero
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/lib/discover/__tests__/compare.test.ts src/components/discover/__tests__/compare-table.test.tsx 2>&1 | grep -E "Tests +[0-9]+ passed" | tail -1
  EXPECT: /Tests +[1-9][0-9]* passed(?!.*failed)/
  EVIDENCE: Tests  32 passed (32)

- [x] G13: portfolio overlap is computed from real holdings, and says so when holdings are unavailable
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/lib/discover/__tests__/overlap.test.ts 2>&1 | grep -E "Tests +[0-9]+ passed" | tail -1
  EXPECT: /Tests +[1-9][0-9]* passed(?!.*failed)/
  EVIDENCE: Tests  14 passed (14)

## N6 — the Discover page

- [x] G14: the goal composer states preferences as a sentence and produces filter params, never a recommendation
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/lib/discover/__tests__/match.test.ts src/components/discover/__tests__/goal-composer.test.tsx 2>&1 | grep -E "Tests +[0-9]+ passed" | tail -1
  EXPECT: /Tests +[1-9][0-9]* passed(?!.*failed)/
  EVIDENCE: Tests  38 passed (38)

- [x] G15: the three starting choices each explain the match as "n of m preferences", naming them
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/components/discover/__tests__/starting-choices.test.tsx 2>&1 | grep -E "Tests +[0-9]+ passed" | tail -1
  EXPECT: /Tests +[1-9][0-9]* passed(?!.*failed)/
  EVIDENCE: Tests  12 passed (12)

- [x] G16: Discover uses the full desktop width the shell already allows, not a 5xl column
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && echo "NARROW=$(grep -c 'max-w-5xl' "src/app/(app)/discover/page.tsx")"
  EXPECT: NARROW=0
  EVIDENCE: NARROW=0

## N8/N9 — honesty surfaces

- [x] G17: advice language appears nowhere in Discover — D3 is unreviewed and this product is not an adviser
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && echo "ADVICE=$(grep -rniE 'best for you|recommended for you|we recommend|you should (buy|invest)|suitable for you' src/components/discover src/lib/discover "src/app/(app)/discover" 2>/dev/null | grep -v __tests__ | grep -vE ':[0-9]+: *(\*|//|/\*)' | wc -l | tr -d ' ')"
  EXPECT: ADVICE=0
  EVIDENCE: ADVICE=0

- [x] G18: no execute affordance reached any Discover surface (non-negotiable #1)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && echo "EXEC=$(grep -rnE 'place_order|/execute|confirm=true|OrderGateway' src/components/discover src/lib/discover "src/app/(app)/discover" 2>/dev/null | grep -v __tests__ | grep -vE ':[0-9]+: *(\*|//|/\*)' | wc -l | tr -d ' ')"
  EXPECT: EXEC=0
  EVIDENCE: EXEC=0

- [x] G19: performance disclosure is reachable where performance is shown, not only at the foot of the page
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/components/discover/__tests__/disclosure.test.tsx 2>&1 | grep -E "Tests +[0-9]+ passed" | tail -1
  EXPECT: /Tests +[1-9][0-9]* passed(?!.*failed)/
  EVIDENCE: Tests  10 passed (10)

- [x] G20: the metrics the brief asks for and this product does not compute are written down, each with where it would come from
  CHECK: awk -F'|' '/^\|/ && $2 !~ /Metric/ && $2 !~ /^ *-+ *$/ {n++; if (length($3)>4 && length($4)>4) s++} END {print "GAPS="n" SOURCED="s}' docs/DISCOVER-METRICS-GAP.md
  EXPECT: /GAPS=([1-9][0-9]*) SOURCED=\1/
  EVIDENCE: GAPS=12 SOURCED=12

## Integration

- [x] G21: the whole web unit suite is green
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run 2>&1 | grep -E "Tests +[0-9]+ passed" | tail -1
  EXPECT: /Tests +[1-9][0-9]* passed(?!.*failed)/
  EVIDENCE: Tests  1683 passed (1683)

- [x] G22: everything this work owns typechecks and lints clean
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec eslint src/lib/discover src/components/discover "src/app/(app)/discover" src/lib/nav.ts >/dev/null 2>&1 && OWN=$(pnpm exec tsc --noEmit 2>&1 | grep -E "error TS" | grep -cE "discover|nav\.ts" || true); echo "OWN_TS=$OWN"; [ "$OWN" = 0 ] && echo GATE_OK || echo GATE_FAILED
  EXPECT: GATE_OK
  EVIDENCE: OWN_TS=0 | GATE_OK

- [x] G23: the judgement calls are recorded as UNREVIEWED, per the autonomy charter
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && echo "DSC=$(grep -cE '^## DSC[0-9]+ ' docs/DECISIONS-MERGE.md) TAGGED=$(grep -E '^## DSC[0-9]+ ' docs/DECISIONS-MERGE.md | grep -c UNREVIEWED)"
  EXPECT: /DSC=([1-9][0-9]*) TAGGED=\1/
  EVIDENCE: DSC=5 TAGGED=5

ABANDON: N7 the risk-return explorer is not built this run — return against volatility puts all six
baskets in one corner of the plane, so the chart would be decoration rather than a way to choose.
It is worth building when the catalogue spreads or when maximum drawdown exists for the x-axis
(docs/DISCOVER-METRICS-GAP.md item 4). No gate above depended on it; it is named here so the
omission is visible rather than silent.
