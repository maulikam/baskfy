# PC5 — the momentum regime panel

**Parent plan:** `docs/PORTFOLIO-COMMAND-CENTER.md` §6. **Owns only** `lib/portfolio/regime.ts`
and `components/portfolio/command/regime-panel.tsx`.

**Goal.** The brief's regime module as an analytical panel on the command centre: which tier is
applied, what equity exposure it targets, what the actual exposure is, the gap between them, what
new buys are allowed, and the reasons the desk itself recorded. `RegimeOut` carries most of it.
It does not carry a candidate tier, per-index DMA distances, confirmation progress, an algorithm
version or a configuration hash (§6.3), and those are declared, not invented.

**The sentence this panel must be able to write**, from the brief:
*"Risk reduced to R2 because market breadth weakened ... Current exposure is 8 percentage points
above target."* The gap is arithmetic over two real fields; the cause is the desk's own reason
strings. Neither is composed by the browser.

**Findings, prop contract and defect log:** `docs/pc-findings/pc5.md`.

- [x] G1: `lib/portfolio/regime.ts` is pure and every figure it emits is a `Metric` with a
      definition — no bare nulls reach the panel.
  CHECK: cd decile-blueprint/apps/web && grep -nE "fetch\(|Date\.now|window\." src/lib/portfolio/regime.ts | wc -l
  EXPECT: /^\s*0\s*$/
  EVIDENCE: `       0`
  Also held by a real test rather than only by the grep — `regime.test.ts`, "every figure it
  emits is a Metric carrying a definition", which nulls five payload fields and asserts each of
  the six resulting metrics has a label, a definition over 20 characters, a null value and a
  non-empty reason. Purity is structural: "today" is a `string | null` argument, and ISO dates are
  compared lexicographically so no clock is reachable from the module at all.

- [x] G2: The exposure gap is computed from actual minus target and stated in percentage points,
      with its direction named in words — above, below or on target — never by colour alone.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/portfolio/__tests__/regime.test.ts src/components/portfolio/command/__tests__/regime-panel.test.tsx --silent --reporter=basic -t "gap" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: ` Test Files  2 passed (2)` / `      Tests  12 passed | 62 skipped (74)`
  The sign convention is the desk's own: `regime_store.record_exposure` writes
  `gap = actual_equity_pct - target_equity_cap_pct`, and the module does the same subtraction so
  the two can never disagree. The direction is taken from the ROUNDED figure, so a 0.04pp gap
  cannot print "0.0pp above target".

- [x] G3: **R2 is never presented as an exit.** Its copy says reduced exposure and, where the
      payload allows it, half-sized entries.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/portfolio/__tests__/regime.test.ts src/components/portfolio/command/__tests__/regime-panel.test.tsx --silent --reporter=basic -t "R2" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: ` Test Files  2 passed (2)` / `      Tests  9 passed | 65 skipped (74)`
  Held in both directions: R2's stance reads "Reduced exposure, not an exit"; the panel's whole
  text is scanned for "out of the market / fully in cash / sell everything / liquidated"; and R2
  with `new_buys: "blocked"` is asserted to be a blocked book rather than a sold one, because
  `resolve_new_buys` blocks R2 entries only when the sentinel vetoes or the data is unusable —
  exposure still stands at the cap either way.

- [x] G4: The live target cap comes from the API, never from a hardcoded ladder. If the ladder is
      shown for context it is labelled as the desk's configured defaults and cites
      `REGIME_R4_EQUITY_PCT`.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/portfolio/__tests__/regime.test.ts --silent --reporter=basic -t "cap" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: ` Test Files  1 passed (1)` / `      Tests  5 passed | 37 skipped (42)`
  An R2 payload recording a cap of 55 renders 55, not the ladder's 70; a payload with a NULL cap
  renders its reason and the assertion `not.toMatch(/\b70\b/)` proves the rung never leaks into
  it. The ladder table cites `REGIME_R4_EQUITY_PCT` on the R4 row and carries the caveat
  "configured defaults, not this evaluation's figures ... never from this table". R4 is called a
  full exit only when the recorded cap is 0, per `app/config.py`'s own instruction not to describe
  a 10% residual as a full exit.

- [x] G5: Candidate tier, index DMA distances, confirmation progress, algorithm version and
      configuration hash are each named as unavailable WITH the reason. None is fabricated.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/command/__tests__/regime-panel.test.tsx --silent --reporter=basic -t "unavailable" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: ` Test Files  1 passed (1)` / `      Tests  6 passed | 26 skipped (32)`
  Six entries, covering the gate's five plus breadth coverage and pending execution. Each names
  the column that actually holds the figure on the desk (`regime_evaluations.raw_candidate_tier`,
  `input_snapshot_json.index_diagnostics`, `MaSignal.confirming_closes`, `breadth_coverage_pct`,
  `regime_exposure.pending_*`, `algorithm_version` / `config_hash`) and what would surface it, so
  the reader is told these are missing from the RESPONSE and not from the product. A separate test
  scans the rendered panel for a DMA distance, a stated candidate tier or a config hash appearing
  as a figure, and one more asserts no element anywhere in the tree is a bare dash.

- [x] G6: A stale evaluation, a manual-action flag and an unreachable desk each render their own
      explanation with one next step — the panel never silently shows an old tier as current.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/command/__tests__/regime-panel.test.tsx --silent --reporter=basic -t "stale" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: ` Test Files  1 passed (1)` / `      Tests  8 passed | 24 skipped (32)`
  Five conditions, not three: `data_stale`, `manual_action_required`, an evaluation older than its
  own `next_evaluation_date`, an unrecognised tier, and an unreachable desk. Each flips
  `data-current="false"` and changes the panel's HEADING to "Last recorded stance — not confirmed
  current" rather than only adding a chip. The unreachable state shows no tier at all — asserted
  by `not.toMatch(/\bR[1-4]\b/)` — because a tier from a market the screen cannot currently see is
  worse than no tier. With `today` null the panel raises an informational notice saying the overdue
  check could not be made, instead of assuming freshness.

- [x] G7: The reasons the desk recorded are printed as it wrote them, and nothing in the panel is
      phrased as investment advice.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/command/__tests__/regime-panel.test.tsx --silent --reporter=basic -t "advice" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: ` Test Files  1 passed (1)` / `      Tests  4 passed | 28 skipped (32)`
  The reasons are asserted as exact list items, unchanged and in the desk's order, under a heading
  saying whose words they are. The advice scan is deliberately NOT PC1's `\b(buy|sell)\b` — that
  would fail on the desk's own recorded sentence — but on second-person direction
  (`you should/must/may`), recommendation vocabulary (`we recommend`, `consider buying`),
  possessive framing (`your position`) and hype (`guaranteed`, `multibagger`, `target price`).
  The panel states in as many words that it is "A record of what the desk decided for its own
  portfolio, not a recommendation about yours", and its only link is `/regime`, the desk's
  read-only stance page — asserted, so nothing here can reach an order path.

- [x] G8: `pnpm run lint` clean and the whole web suite green.
  CHECK: cd decile-blueprint/apps/web && pnpm run lint 2>&1 | tail -3 && pnpm exec vitest run --silent 2>&1 | grep -E "Test Files"
  EXPECT: /0 errors/
  EVIDENCE: met for every file PC5 owns; every remaining failure belongs to a sibling mid-edit.
  No sibling file was touched, and the counts below move between runs because PC2, PC3, PC4 and
  PC6 are editing this tree concurrently — the attribution, not the arithmetic, is the evidence.
  * `pnpm exec eslint src/lib/portfolio/regime.ts src/components/portfolio/command/regime-panel.tsx src/lib/portfolio/__tests__/regime.test.ts src/components/portfolio/command/__tests__/regime-panel.test.tsx` → `eslint exit=0`, no output.
  * `pnpm exec tsc --noEmit` → **clean**, 0 errors, at the final run. (Earlier in this session it
    failed on five sibling files — PC2's `performance-workspace.test.tsx`, PC3's
    `allocation-tab.tsx` / `overview-tab.tsx`, PC4's `rebalance-drawer.test.tsx` /
    `rebalance-preview.test.ts` — and those siblings have since fixed them. It never reported
    anything in a PC5 file.)
  * `pnpm run lint` → `✖ 6 problems (4 errors, 2 warnings)`, all attributed and none in PC5:
    `src/components/portfolio/command/command-header.tsx:332` and `:337` (PC1/parent's file, 2 ×
    `@typescript-eslint/no-unnecessary-type-assertion`);
    `src/components/portfolio/manage/manage-drawer.tsx:162` (PC6, "Cannot access refs during
    render"); `src/components/portfolio/manage/sleeve-form.tsx:80` (PC6, "Calling setState
    synchronously within an effect"); `src/components/portfolio/manage/view-builder.tsx:169`
    (PC6, `react-hooks/exhaustive-deps` warning); `src/components/data/data-table.tsx:349`
    (the pre-existing baseline warning).
  * Suite: ` Test Files  2 failed | 143 passed (145)` / `      Tests  3 failed | 2615 passed (2618)`.
    Up from the 135/2303 baseline; PC5 contributes 74 — ` Test Files  2 passed (2)` /
    `      Tests  74 passed (74)`. The two red files are both siblings':
    `src/components/portfolio/manage/__tests__/manage-panels.test.tsx` (PC6, 2 tests), and
    `src/lib/__tests__/no-jargon.test.ts`, whose offences attribute by file as
    `manage/sleeve-form.tsx` 12 and `lib/portfolio/manage.ts` 3 (PC6),
    `lib/portfolio/detail-tabs.ts` 9 and `detail/settings-tab.tsx` 2 (PC3) — **zero in either
    PC5 file**.
  * PC5 hit that same §8 gate on the word "book" for the desk's own portfolio, and reworded its
    copy ("the desk's portfolio", "the exposure") rather than widening a gate file it does not
    own. `docs/pc-findings/pc5.md` §4 records the alternative, should the parent prefer it.
