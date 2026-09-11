# PC-INT — the branch node: five leaves become one screen

**Parent plan:** `docs/PORTFOLIO-COMMAND-CENTER.md` §6. Owns `command-center-screen.tsx`, every
`page.tsx`, the server reads, the e2e spec and this ledger. Thirty-two finished leaves can still
be a broken product; this is where that is caught.

- [x] I1: All five leaves are mounted and reachable from the Portfolios screen — performance
      workspace, regime panel, rebalance drawer, management drawer, and the detail workspace via
      a portfolio name.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/command/__tests__/command-center-screen.test.tsx --silent --reporter=basic -t "mounted" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE:  Test Files  1 passed (1) | Tests  5 passed | 58 skipped (63) — performance workspace,
      attribution, regime panel, management drawer and the detail workspace via a portfolio name.

- [x] I2: The four header controls PC1 deliberately deferred now exist, because the chart they
      scope exists: date range, benchmark, base currency and the overflow menu. Each either does
      something or states why it cannot — no dead control.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/command/__tests__/command-center-screen.test.tsx --silent --reporter=basic -t "header control" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE:  Test Files  1 passed (1) | Tests  12 passed | 51 skipped (63)
      **Scoped, and recorded rather than silently met.** Four of the five exist as controls: the
      portfolio selector, the benchmark statement, base currency and the overflow menu. The
      **date range does not appear in the header**, and that is a decision: `/portfolio/overview`
      declares `query?: never`, so the consolidated series arrives in ONE window and no control
      here could re-measure it. PC2's pills sit beside the chart they scope and say exactly that,
      and the brush narrows within the served window — which is real. A range control in the
      header that changed nothing would be the dead control every other entry here forbids.

- [x] I3: Monitoring views still never enter a capital total, with the whole screen assembled —
      the regression most likely to be introduced by adding four more panels.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/command --silent --reporter=basic -t "monitoring" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE:  Test Files  1 passed | 2 skipped (3) | Tests  6 passed | 120 skipped (126) — including a
      new assertion that with all five panels assembled, views mode renders neither the metric
      band nor the performance workspace.

- [x] I4: No bare dash reaches the assembled screen, from any fixture, in either mode.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio src/lib/portfolio --silent --reporter=basic -t "unavailable" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE:  Test Files  13 passed | 33 skipped (46) | Tests  75 passed | 798 skipped (873)

- [x] I5: The §2.2 blocked names — beta, VaR, Sharpe, Sortino, sector, momentum score, days to
      liquidate — appear nowhere as a rendered figure, across every new component.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio src/lib/portfolio --silent --reporter=basic -t "fabricat" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE:  Test Files  2 passed | 44 skipped (46) | Tests  4 passed | 869 skipped (873)

- [x] I6: Nothing anywhere in the portfolio tree can place an order.
  CHECK: cd decile-blueprint/apps/web && grep -rniE "place_?order|confirm=true" src/components/portfolio src/lib/portfolio | wc -l
  EXPECT: /^\s*0\s*$/
  EVIDENCE: 0

- [x] I7: The whole web suite passes with everything assembled.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run --silent 2>&1 | grep -E "Test Files|Tests "
  EXPECT: /Test Files .* passed/
  EVIDENCE:  Test Files  155 passed (155) | Tests  2825 passed (2825) — from a baseline of 135 files /
      2303 tests before the five leaves.

- [x] I8: `pnpm run lint` — tsc strict, eslint and the route-shadowing check — reports zero errors.
  CHECK: cd decile-blueprint/apps/web && pnpm run lint 2>&1 | tail -6
  EXPECT: /0 errors/
  EVIDENCE:  ✖ 1 problem (0 errors, 1 warning) | ok — 30 redirected source(s), no page file shadowed.
      The one warning is the pre-existing `data-table.tsx` React-compiler note, present at baseline.

- [ ] I9: Accessibility — axe finds no violation on the assembled screen, in light AND dark.
  CHECK: cd decile-blueprint/apps/web && pnpm exec playwright test e2e/command-center.spec.ts --grep "accessibility|contrast" --reporter=list 2>&1 | tail -6
  EXPECT: /\d+ passed/
  EVIDENCE: not run.
      **Blocked in this session, and not by the code.** `playwright.config.ts` starts the API,
      which runs `alembic upgrade head` against Postgres on 127.0.0.1:5433. There is no Postgres
      on this machine (`postgres` and `pg_isready` are both absent) and the Docker daemon is not
      running (`docker info` fails), so the database cannot be brought up and the browser suite
      cannot start: `Error: Process from config.webServer was not able to start. Exit code: 1`.
      The same missing daemon blocks I12's deploy.
  ABANDON: I9 the browser cannot be started here. What IS proved arithmetically:
      `contrast.test.ts` + `no-brand-as-text.test.ts` — **540 assertions, all passing** — check
      every token pair in BOTH themes, including `--brand` on its own foreground and `--info` on
      all three of its surfaces. What is NOT proved is what the browser actually painted:
      translucent surfaces can undo a passing token pair, and axe checks structure a token
      table cannot see. **PC1's G15 was green on 11 Sep against PC1's screen; it has NOT been
      re-run against the assembled one.** This is the single biggest caveat on this work.

- [ ] I10: Responsive — the rail collapses on tablet, the desktop table is not attempted on
      mobile, and nothing scrolls sideways, with all five panels present.
  CHECK: cd decile-blueprint/apps/web && pnpm exec playwright test e2e/command-center.spec.ts --grep "tablet|mobile" --reporter=list 2>&1 | tail -6
  EXPECT: /\d+ passed/
  EVIDENCE: not run — same blocker as I9.
      **Blocked in this session, and not by the code.** `playwright.config.ts` starts the API,
      which runs `alembic upgrade head` against Postgres on 127.0.0.1:5433. There is no Postgres
      on this machine (`postgres` and `pg_isready` are both absent) and the Docker daemon is not
      running (`docker info` fails), so the database cannot be brought up and the browser suite
      cannot start: `Error: Process from config.webServer was not able to start. Exit code: 1`.
      The same missing daemon blocks I12's deploy.
  ABANDON: I10 the responsive behaviour is asserted in jsdom where it can be (the phone list
      renders instead of the table below `md`, sharing ONE `RowDetail` renderer so the phone
      cannot drift into showing less), but jsdom runs no stylesheet, so the collapse itself —
      a CSS decision — is unverified for the assembled screen. PC1's G16 was green against
      PC1's screen and has not been re-run with five more panels in it.

- [x] I11: `docs/PORTFOLIO-COMMAND-CENTER.md` §2.2 is up to date — every blocked metric a leaf
      discovered is recorded, and every one a leaf UNblocked is corrected.
  EVIDENCE: `docs/PORTFOLIO-COMMAND-CENTER.md` §7, written from the five findings files. Eleven metrics
      added, four §2.2 claims corrected as too strong, one §2.1 row corrected as describing a
      field the API does not send (`NavSeriesOut.points[]` has no `invested` — those names
      belong to the desk's own schema), and a warning added at the head of §2.2 pointing at §7.

- [ ] I12: G18 from `GATES.md` — the deployed container runs this commit's build, so the screen
      proved green here is the screen the box serves.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=${AWS_PROFILE:-baskfy-poc} BASKFY_INSTANCE_ID=${BASKFY_INSTANCE_ID:-i-086986250704e4392} bash tools/deploy/box.sh "docker inspect --format '{{.Config.Image}}' baskfy-staging-web-1" 2>&1 | tail -2
  EXPECT: /baskfy-web:[0-9a-f]{7}/
  EVIDENCE: measured 11 Sep 2026 — `…/baskfy-web:bd78529`, the commit BEFORE PC1. The box serves
      the pre-redesign screen.
  ABANDON: I12 same handover as `GATES.md` G18 — the Docker daemon is not running on this machine,
      so no image can be built or pushed, and a Phase-A deploy is Maulik's call because that box is
      the live auto-execute host. Written up in `NEEDS-MAULIK.md` under "PC". Nothing about the
      CODE is unproved by this; only that the box has caught up.

---

## Added by the integration node, 11 Sep 2026 — the brief's requirements no leaf gate claimed

PC1–PC6's gates between them cover the brief's screens. Reading the brief against them line by
line turns up six requirements that belong to no leaf, mostly because they are properties of the
*assembled* product rather than of any one panel. They are gated here rather than dropped, which
is the whole reason this file exists.

- [x] I13: The top navigation is the brief's — wordmark, the five destinations, and the right-side
      utilities: search and command palette, the as-of stamp with market status, theme selector,
      profile. The Portfolios destination is active on this route.
      **Two deliberate divergences, recorded rather than "fixed":** the destinations read
      *Market* and *Portfolio*, not *Markets* and *Portfolios* — `lib/nav.ts` sets one-word
      destinations as a decision with its reasoning in the file, and CLAUDE.md's
      "the DECISION wins, not the doc" rule forbids reverting it from a brief's generic list. And
      there is no notification bell: this screen's notification surface is the "Needs attention"
      rail the brief itself specifies, and a second one would be two places to read the same
      alerts.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/command/__tests__/command-center-screen.test.tsx --silent --reporter=basic -t "navigation" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE:  Test Files  1 passed (1) | Tests  2 passed | 61 skipped (63)

- [x] I14: Each interaction the brief names either works or says why it cannot, and none is drawn
      as a dead control: the command palette reaches this screen's actions, export of the current
      view, a shareable read-only report, undo for organisational changes, and an audit log for
      every portfolio assignment and rebalance decision. Audit history has no endpoint (§6.3) and
      is therefore named with what would unblock it — that is a pass; a greyed button is not.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio src/lib/portfolio src/components/shell --silent --reporter=basic -t "interaction|command palette" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE:  Test Files  3 passed | 45 skipped (48) | Tests  6 passed | 885 skipped (891)
      Export of the current view is real and follows the display mode. **Undo is real for the one
      write that is exactly reversible** — a move, reversed by the same request with the
      portfolios swapped; a rename is reversed by renaming and a delete is not reversible at all,
      so neither is offered one. The command palette is the shell's existing ⌘K. Audit history
      and a shareable read-only report are each NAMED with what they need — both are C3
      multi-tenant work, and the second is also a privacy decision rather than an engineering one.

- [x] I15: Copy style. The brief bans a register, not just a phrase: no "amazing", "winning
      portfolio", "guaranteed", "buy now", "hot stock", and no generic warning without its
      subject. Asserted by scanning the whole portfolio tree, because one careless string in one
      alert is all it takes.
  CHECK: cd decile-blueprint/apps/web && grep -rniE "amazing|winning portfolio|guaranteed|buy now|hot stock|top pick|must.buy" src/components/portfolio src/lib/portfolio | grep -v "__tests__" | wc -l
  EXPECT: /^\s*0\s*$/
  EVIDENCE:         0

- [x] I16: The brief's output requirement 8 — a reusable component and design-token
      specification — exists as a document a second contributor could build against, naming every
      semantic token, its value in both themes, and every component the screen introduces with its
      props and its states.
  EVIDENCE: `docs/PORTFOLIO-DESIGN-SYSTEM.md`, 282 lines. Every semantic token with its value in BOTH
      themes and the contrast figure it was chosen for; radius, density, elevation and the three
      motion durations under 200ms that this screen is allowed to use; the `Metric` primitive;
      and every component the screen introduces with its states and the rule it encodes. It
      closes on the two contracts a new panel must not break — units, and ownership.
      Cross-checked against the code rather than written from memory: the tabular-numeral class,
      the shared `RowDetail` renderer and the `md:hidden` table swap were each grepped.

- [x] I17: The two states no leaf gate claimed are designed and explain themselves with one next
      action: permission restricted, and market closed. (Empty, manual, broker-disconnected,
      partial sync, stale prices, reconciliation mismatch, missing cost basis, no history,
      loading skeleton and API error are held by `GATES.md` G10 and `gates/pc3.md` G9.)
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio --silent --reporter=basic -t "restricted|market closed" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE:  Test Files  1 passed | 26 skipped (27) | Tests  4 passed | 527 skipped (531)
      A permission refusal gets its own sentence and its own next step: offering "Try again" to
      somebody who has lost access is a loop they cannot exit. The session label is rendered only
      when the caller supplies the answer — this component takes no clock, and a session guessed
      from the browser's zone would be wrong for half the day on a screen whose whole subject is
      which clock a figure is on.

- [x] I18: Drill-down: a chart series, an attribution row and an allocation slice each lead to the
      holdings they describe, rather than being a picture you cannot ask a question of.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio --silent --reporter=basic -t "drill" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE:  Test Files  3 passed | 24 skipped (27) | Tests  4 passed | 527 skipped (531) — a comparison
      row and an attribution row both open the portfolio they name; a security slice on the
      allocation tab opens that instrument, while a broker slice carries no link rather than a
      dead one.

- [ ] I19: **The design is actually looked at.** Every other gate on this screen is a proposition
      a test can hold. The brief's first paragraph is not: *"a premium, modern financial-
      intelligence workspace comparable in quality and information density to Linear, Stripe, Ramp
      and institutional portfolio terminals ... not a conventional broker dashboard or a generic
      collection of white statistic cards."* A suite of 2,300 green tests cannot tell you the
      screen looks cheap. So the assembled screen is rendered in a real browser at 1440, at tablet
      and at phone width, in both themes, and the screenshots are examined against the brief's
      named anti-patterns: every metric in its own isolated card, decorative gradients, colour
      without a second signal, a page so long it cannot be read.
  CHECK: cd decile-blueprint/apps/web && pnpm exec playwright test e2e/command-center.spec.ts --grep "screenshot" --reporter=list 2>&1 | tail -4
  EXPECT: /\d+ passed/
  EVIDENCE: not run — same blocker as I9.
      **Blocked in this session, and not by the code.** `playwright.config.ts` starts the API,
      which runs `alembic upgrade head` against Postgres on 127.0.0.1:5433. There is no Postgres
      on this machine (`postgres` and `pg_isready` are both absent) and the Docker daemon is not
      running (`docker info` fails), so the database cannot be brought up and the browser suite
      cannot start: `Error: Process from config.webServer was not able to start. Exit code: 1`.
      The same missing daemon blocks I12's deploy.
  ABANDON: I19 **nobody has looked at this screen.** It is the honest headline of this run: 2,825
      tests are green and not one of them can report that the result looks cheap, that the band
      reads as a row of isolated cards, or that the page is too long. The brief's first
      paragraph is a design proposition and it remains unverified. First thing to do when a
      database is available: `pnpm exec playwright test e2e/command-center.spec.ts`, then open
      `/portfolio/portfolios` at 1440, 1024 and 390 in both themes and read it.

- [x] I20: The adversarial re-read of PC1's own gates, run at integration rather than trusted
      from its ledger — `GATES.md` G7 (no bare dash), G11 (never colour alone) and G12 (nothing
      fabricated) re-verified against the code as it stands with five more leaves in it.
  EVIDENCE: re-run 11 Sep 2026 over the assembled tree, and it found one thing.
      · **G7.** `EMPTY_CELL` — the app's rendered em dash — appears **zero** times in
        `command/`, `detail/`, `rebalance/`, `manage/` or any of the five pure modules. It is
        used only by the three PRE-EXISTING components these screens supersede
        (`hero-metrics`, `inspector-drawer`, `detail-holdings`). Every formatter call in the new
        trees was read individually: all nine are inside a non-null branch or take a number.
      · **G11.** Every `text-positive` / `text-negative` / `text-warning` in all four new
        directories is paired with an icon, a word or a sign. `-t "colour"` over the whole
        portfolio tree: `Test Files 7 passed | 39 skipped (46)` / `Tests 12 passed`.
      · **G12.** `-t "fabricat"`: `Tests 4 passed | 869 skipped (873)`; the order grep returns 0.
      · **What it found.** PC1's G7 and G12 held, but its RATE rendering did not, and no gate was
        pointed at it. The band and the comparison table appended `%` to the API's stored
        fraction, so a 1.99% day would have read "0.0199%" and an 8.2% drawdown "-0.082%".
        Forty-five tests were green over it because the fixtures used values the server never
        sends. Found by PC2, confirmed by PC3 independently, fixed at both sites with the
        conversion moved into the pure layer, and now held by two tests that were each confirmed
        to go red when the fix is reverted.