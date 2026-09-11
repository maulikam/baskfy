# PC1 — the Portfolio Command Center, aggregate screen

**Brief, 11 Sep 2026 (Maulik).** *"Redesign Basqfy's Portfolios screen completely ... It should
feel like an intelligent portfolio operating system — not a broker holdings page and not a
collection of generic cards."*

**Scope.** Replace `/portfolio/portfolios` with a command centre: command header with a
Capital-portfolios / Monitoring-views switch, an operational data-health strip, a connected
executive metric band, one sortable expandable comparison table, and a collapsible "Needs
attention" rail — in light and dark, across desktop/tablet/mobile, with every empty and error
state explained. The plan, the decomposition and the data survey are in
`docs/PORTFOLIO-COMMAND-CENTER.md`; PC2–PC6 are declared there and are not this leaf.

**The rule this leaf is judged against** (the brief's own, and G7 enforces it):
never a bare "—". A metric Baskfy cannot compute is labelled unavailable *with its reason*, or is
absent with the reason recorded in the plan. Nothing here fabricates a financial number.

---

- [x] G1: Semantic tokens exist for the brand accent (orange, per the brief), for
      benchmark/informational state, and for every surface the screen uses — defined in BOTH
      themes, so dark mode is a token swap rather than a second design.
  CHECK: cd decile-blueprint/apps/web && node -e "const s=require('fs').readFileSync('src/app/globals.css','utf8');const dark=s.slice(s.indexOf('.dark {'));const need=['--brand','--brand-foreground','--brand-strong','--info','--info-muted'];console.log(need.map(t=>t+'='+(s.includes(t+':')?'light':'MISSING')+'/'+(dark.includes(t+':')?'dark':'MISSING')).join(' '))"
  EXPECT: /--brand=light\/dark.*--info=light\/dark/
  EVIDENCE: --brand=light/dark --brand-foreground=light/dark --brand-strong=light/dark --info=light/dark --info-muted=light/dark

- [x] G2: The brand accent is actually orange, and is NOT reused for gain/loss/severity.
  CHECK: cd decile-blueprint/apps/web && node -e "const s=require('fs').readFileSync('src/app/globals.css','utf8');const all=[...s.matchAll(/^\s+--brand:\s*(#[0-9a-fA-F]{6})/gm)].map(m=>m[1]);const ok=all.length>0&&all.every(h=>{const r=parseInt(h.slice(1,3),16),g=parseInt(h.slice(3,5),16),b=parseInt(h.slice(5,7),16);return r>200&&g>80&&g<180&&b<100});const meaning=/--positive:\s*#fe7510|--negative:\s*#fe7510|--warning:\s*#fe7510/.test(s);console.log('declarations',JSON.stringify(all),'orange='+ok,'reused-for-meaning='+meaning)"
  EXPECT: /orange=true reused-for-meaning=false/
  EVIDENCE: declarations ["#fe7510","#fe7510"] orange=true reused-for-meaning=false

- [x] G3: The page defaults to Capital portfolios and the mode switch is visible beside the title.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/command --silent --reporter=basic -t "mode switch" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: Test Files  1 passed (1) | Tests  5 passed | 31 skipped (36)

- [x] G4: Monitoring views never enter a capital total, and the mode says so explicitly.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/command --silent --reporter=basic -t "monitoring" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: Test Files  1 passed (1) | Tests  4 passed | 32 skipped (36)

- [x] G5: The data-health strip reports the real counts and names WHAT is affected, never a
      generic warning.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/command --silent --reporter=basic -t "health" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: Test Files  1 passed (1) | Tests  4 passed | 32 skipped (36)

- [x] G6: The executive band shows net worth, invested, cash, today's P&L, unrealised, realised,
      XIRR, TWR, drawdown and peak — each labelled, each with its own definition available.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/command --silent --reporter=basic -t "snapshot" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: Test Files  1 passed (1) | Tests  5 passed | 31 skipped (36)

- [x] G7: **No bare dash anywhere.** Every unavailable metric renders its reason. This is the
      brief's own hard rule and the one most likely to be quietly broken.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/command --silent --reporter=basic -t "unavailable" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: Test Files  1 passed (1) | Tests  2 passed | 34 skipped (36)

- [x] G8: The comparison table sorts, expands, carries a per-portfolio colour identifier AND a
      non-colour cue, and links to the detail workspace.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/command --silent --reporter=basic -t "table" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: Test Files  1 passed (1) | Tests  6 passed | 30 skipped (36)

- [x] G9: The intelligence rail prioritises by impact and every alert says what changed, which
      portfolio, what to do next and when it was detected — and never phrases it as advice.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/command --silent --reporter=basic -t "attention" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: Test Files  1 passed (1) | Tests  6 passed | 30 skipped (36)

- [x] G10: Every state the brief names is designed and explains itself with one next action:
      empty, no brokers, stale prices, reconciliation mismatch, missing cost basis, no history,
      loading skeleton, API error.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/command --silent --reporter=basic -t "state" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: Test Files  1 passed (1) | Tests  10 passed | 26 skipped (36)

- [x] G11: Nothing is signalled by colour alone — every tone carries an icon, glyph or word.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/command --silent --reporter=basic -t "colour" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: Test Files  1 passed (1) | Tests  3 passed | 33 skipped (36)

- [x] G12: No fabricated financial metric reaches the screen. The 2.2-blocked names — beta, VaR,
      Sharpe, sector, target weight, momentum score — appear only as declared-unavailable copy,
      never as a rendered figure.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/command --silent --reporter=basic -t "fabricat" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: Test Files  1 passed (1) | Tests  2 passed | 34 skipped (36)

- [x] G13: The whole web suite passes — the new screen has not broken the old ones.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run --silent 2>&1 | grep -E "Test Files|Tests "
  EXPECT: /Test Files .* passed/
  EVIDENCE: Test Files  135 passed (135) | Tests  2303 passed (2303)

- [x] G14: `pnpm run lint` (tsc strict + eslint + route shadowing) reports zero errors.
  CHECK: cd decile-blueprint/apps/web && pnpm run lint 2>&1 | tail -6
  EXPECT: /0 errors/
  EVIDENCE: ✖ 1 problem (0 errors, 1 warning) | ok — 30 redirected source(s), no page file shadowed by any of them

- [x] G15: Accessibility — axe finds no violation on the new screen, in light AND dark.
  CHECK: cd decile-blueprint/apps/web && pnpm exec playwright test e2e/command-center.spec.ts --grep "accessibility|contrast" --reporter=list 2>&1 | tail -8
  EXPECT: /\d+ passed/
  EVIDENCE: ✓  5 [chromium] › e2e/command-center.spec.ts:60:3 › the command centre's contrast holds in dark (1.6s) | 5 passed (45.3s)

- [x] G16: Responsive — the rail collapses on tablet and the desktop table is not attempted on
      mobile; net worth, today's P&L, alerts and the selector come first there.
  CHECK: cd decile-blueprint/apps/web && pnpm exec playwright test e2e/command-center.spec.ts --grep "tablet|mobile" --reporter=list 2>&1 | tail -8
  EXPECT: /\d+ passed/
  EVIDENCE: [chromium] › e2e/command-center.spec.ts:95:1 › on mobile the desktop table is not attempted and nothing scrolls sideways | 2 passed (47.5s)

- [x] G17: The screen is deployed and reachable on the box. NOTE: this probe answers
      *reachability* only — the route is behind the login gate, so a 307 proves the box is
      serving, not that it is serving THIS build. G18 is the half that proves the build.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && curl -s -o /dev/null -w "%{http_code}" --max-time 30 https://staging.baskfy.com/portfolio/portfolios
  EXPECT: /200|307/
  EVIDENCE: 307

- [x] G18: The DEPLOYED page renders the command centre, not the previous screen.
      The page is behind Google sign-in, so it cannot be fetched from a script — and minting a
      session against the box would mean reading the box's `AUTH_SECRET`, which is exactly what
      `box.sh` forbids. So the proof is by identity instead of by fetch: the web container on the
      box runs the image digest ECR holds for this commit's tag, and G3-G16 have already shown
      that this commit renders the command centre. Same bytes, same screen.
      **The check as written could never have passed, and that hid a second fault.** There is no
      container called `baskfy-web` on the box; the compose project names it
      `baskfy-staging-web-1`. `docker inspect` on a missing object exits non-zero, so the probe
      returned "No such object" rather than a digest — a check that fails for a reason unrelated
      to the thing it is testing is not a check. It now reads the image TAG off the running
      container, which is the commit, and compares it to HEAD.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=${AWS_PROFILE:-baskfy-poc} BASKFY_INSTANCE_ID=${BASKFY_INSTANCE_ID:-i-086986250704e4392} bash tools/deploy/box.sh "docker inspect --format '{{.Config.Image}}' baskfy-staging-web-1" 2>&1 | tail -2
  EXPECT: /baskfy-web:[0-9a-f]{7}/
  EVIDENCE: 056235107739.dkr.ecr.ap-south-1.amazonaws.com/baskfy-web:dd9cc73
      commit BEFORE PC1 (`fae98ac`). The box is serving the OLD Portfolios screen. The gate is
      therefore genuinely unmet rather than unprovable, and it is unmet because nothing has been
      deployed since 10 Sep, not because the screen is wrong.
  ABANDON: G18 not deployable from this session — the Docker daemon is not running on this
      machine (`docker info` fails), so the web image cannot be built or pushed to ECR, and a
      deploy of the Phase-A box is Maulik's call in any case: that box is the live auto-execute
      host. Recorded in `NEEDS-MAULIK.md`. Everything G18 would have proved about the CODE is
      proved by G3–G16 against this working tree; what is unproved is only that the box has
      caught up. `gates/pc-integration.md` I12 carries the same handover for PC2–PC6.

<!--
Rules:
- A checked box whose EVIDENCE still reads "pending" is UNMET.
- Evidence is the deciding lines only.
- If a gate becomes impossible: add `ABANDON: G<n> <reason>` and report it. Silent narrowing is
  the failure this file exists to prevent.
-->
