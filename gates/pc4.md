# PC4 — the rebalance preview: a plan to review, never an order

**Parent plan:** `docs/PORTFOLIO-COMMAND-CENTER.md` §6. **Owns only**
`lib/portfolio/rebalance-preview.ts` and `components/portfolio/rebalance/*`.
**Findings:** `docs/pc-findings/pc4.md`.

**Goal.** "Review rebalance" opens a large drawer that shows what would change. The hard part is
what it must NOT do: `RebalanceOut` carries names and target weights and no quantities, no cash
delta, no turnover and no costs (§6.3). A derived quantity reads as an instruction, and this
product places live orders behind a flag that is currently true. So the drawer shows the real
comparison and states, on its own surface, which parts of the brief's list the API does not
produce.

- [x] G1: The drawer shows current weight against target weight per name, plus the four lists the
      API returns — entries, exits, holds, inside-window — each named and counted.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/rebalance src/lib/portfolio/__tests__/rebalance-preview.test.ts --silent --reporter=basic -t "weights" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: ` Test Files  2 passed (2)` / `      Tests  13 passed | 46 skipped (59)`
    Both sides are fractions the server computed (`DetailHoldingOut.weight`,
    `TargetWeightOut.weight`), so the comparison needs no price. A name held at two brokers is
    summed into one row and both accounts are named. Rounding to a percentage happens exactly
    once, at the end: six equal weights of `0.166666` add to `100.00`, not to `100.02`.

- [x] G2: **No quantity, cash figure, turnover or cost is derived.** Each is named as produced by
      the execution plan rather than by this preview, with the reason.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/rebalance --silent --reporter=basic -t "not derived" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: ` Test Files  1 passed (1)` / `      Tests  2 passed | 27 skipped (29)`
    `NOT_PRODUCED_HERE` carries ten figures, each with a reason and where it comes from instead;
    the test renders every one and asserts all three strings. The invariant behind it: **there is
    no rupee sign anywhere in the drawer**, asserted on the Adjust, Impact, Confirm and Plan steps
    — every money figure in this app renders through `formatRupees`, which always prefixes one, so
    no `₹` means no money figure was invented. Also asserted: no `Buy 214` / `Sell 214` /
    `214 shares` shape, and no `quantity` / `cash` / `turnover` / `cost` key on the preview object
    at all — a field that does not exist cannot be rendered by accident.

- [x] G3: The workflow is the brief's five steps — analyse, edit or exclude, review impact,
      confirm, generate plan — and the final step produces a plan, never a submitted trade.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/rebalance --silent --reporter=basic -t "workflow" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: ` Test Files  1 passed (1)` / `      Tests  3 passed | 26 skipped (29)`
    Step order asserted as `["1Analyse","2Adjust","3Impact","4Confirm","5Plan"]`. Step 4 is the
    brief's "confirm broker quantities" as it can honestly exist — there are none, so it shows
    which account holds each exit (real, from `DetailHoldingOut.broker`), says Baskfy does not
    choose an account for an entry, and takes an explicit acknowledgement. Step 5 is gated on it
    and produces `planText(...)`: a document containing `A PLAN, NOT AN ORDER`. The test walks
    every button in the open drawer asserting none is named send / submit / place / execute.

- [x] G4: Nothing in this tree can place an order. No call reaches an execute or order route.
  CHECK: cd decile-blueprint/apps/web && grep -rniE "place_?order|/execute|confirm=true|place-order" src/components/portfolio/rebalance src/lib/portfolio/rebalance-preview.ts | wc -l
  EXPECT: /^\s*0\s*$/
  EVIDENCE: `       0`
    The tree contains no fetch, no mutation and no route reference of any kind. The only callback
    out is `onAnalyse`, which asks the parent to run the rebalance diff — a read.

- [x] G5: Exclusions are reversible and visible — excluding a name updates the comparison and can
      be undone without reopening the drawer.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/rebalance --silent --reporter=basic -t "exclude" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: ` Test Files  1 passed (1)` / `      Tests  6 passed | 23 skipped (29)`
    The excluded row stays on screen (a name that vanishes cannot be put back), carries
    `aria-pressed`, and the header offers "Put them all back". Only an entry or an exit is
    excludable — a hold proposes no action, so there is nothing to take out of the plan. Two
    consequences are asserted because they are the honest ones: an excluded entry lowers
    **Target coverage** to 83.33% and leaves 16.67% **unassigned** rather than being re-spread over
    the names that remain; and **keeping** a name the screen exits makes the after-concentration
    unavailable, naming the symbol that did it, because the target set does not contain it and the
    weights after would no longer add to 100%. An exclusion also withdraws an acknowledgement
    already given.

- [x] G6: Warnings the payload DOES support — delisted names, unpriced instruments, pending
      reconciliation, a stale `as_of` — are surfaced by name, not as a generic caution.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/rebalance --silent --reporter=basic -t "warn" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: ` Test Files  1 passed (1)` / `      Tests  5 passed | 24 skipped (29)`
    Seven warnings, each naming its subjects: `DHFL is delisted`, `SUZLON has no price today`,
    the unreconciled holding, `The screen ran on 2026-09-10; your holdings are marked at
    2026-09-11`, a `requested_as_of` the diff could not use, a screen that returned fewer names
    than `top_n`, and a `holdings_count` that disagrees with the portfolio on screen. Severity is
    a word ("Critical" / "Review") as well as a colour and an icon. A separate test scans the
    rendered copy for advice vocabulary — Baskfy is not registered to give it.

- [x] G7: A portfolio with no screen behind it cannot rebalance, and the drawer says exactly that
      with one next action rather than opening empty.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/rebalance --silent --reporter=basic -t "state" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: ` Test Files  1 passed (1)` / `      Tests  5 passed | 24 skipped (29)`
    `screens: []` renders "There is no screen to rebalance against", explains that Baskfy stores
    no target allocation for a portfolio so there is nothing to diff against, and offers exactly
    one action — **Build a screen** → `/build`. The workflow and the comparison are *absent*, not
    blank. Three neighbouring states are held too: before the first diff the later steps are
    disabled with "Run the diff first"; a `detail` of `null` degrades to "Holdings not loaded, so
    there is no weight to compare." and reports the name count as unknown rather than 0; and a
    disabled primary control always states what would enable it — PC1 shipped exactly that defect
    once and it reached a user.

- [x] G8: The drawer traps focus, closes on Escape, restores focus to its trigger, and is
      announced as a dialog.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio/rebalance --silent --reporter=basic -t "keyboard" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: ` Test Files  1 passed (1)` / `      Tests  2 passed | 27 skipped (29)`
    `role="dialog"`, an explicit `aria-modal="true"` (Radix marks the rest of the tree
    `aria-hidden` but does not set the attribute — both are now true, and the test asserts the
    trigger really is inside an `aria-hidden` subtree while the drawer is open), an accessible
    name from `DialogTitle`. Twenty-four consecutive Tab presses never put focus outside the
    drawer; Escape closes it and focus returns to the trigger. A second test drives the whole
    workflow from the keyboard alone.

- [x] G9: `pnpm run lint` clean and the whole web suite green.
  CHECK: cd decile-blueprint/apps/web && pnpm run lint 2>&1 | tail -3 && pnpm exec vitest run --silent 2>&1 | grep -E "Test Files"
  EXPECT: /0 errors/
  EVIDENCE: **PC4's own files are clean under both; the repo is not, and every remaining failure is
    in a file PC4 does not own and did not touch.** Four sibling leaves were mid-edit in the same
    working tree throughout.
    * `pnpm exec tsc --noEmit` — **exit 0, no errors** (PC3's `allocation-tab.tsx` /
      `overview-tab.tsx` errors, seen mid-run, were fixed by PC3 before this capture).
    * `pnpm exec eslint src/components/portfolio/rebalance src/lib/portfolio/rebalance-preview.ts
      src/lib/portfolio/__tests__/rebalance-preview.test.ts` — **no output: 0 errors, 0 warnings.**
    * `pnpm exec eslint .` — `✖ 15 problems (13 errors, 2 warnings)`, in seven files, none PC4's:
      `components/portfolio/manage/{manage-drawer,sleeve-form,view-builder}.tsx` and
      `manage/__tests__/{manage-panels,manage-structure}.test.tsx` (**PC6**),
      `app/(app)/regime/__tests__/page.test.tsx` (**PC5**), `components/data/data-table.tsx`
      (pre-existing, a `Compilation Skipped` warning from the React compiler plugin).
    * `pnpm exec vitest run --silent` — ` Test Files  1 failed | 147 passed (148)` /
      `      Tests  1 failed | 2666 passed (2667)`. The single failure is
      `src/lib/__tests__/no-jargon.test.ts`, whose offences attribute as: `manage/sleeve-form.tsx`
      ×12 and `lib/portfolio/manage.ts` ×3 (**PC6**), `lib/portfolio/detail-tabs.ts` ×9 and
      `detail/settings-tab.tsx` ×2 (**PC3**). **Zero from PC4** — this leaf did hit that gate
      (six uses of "book" in user-visible prose) and fixed its own before this capture.
    * PC4's own 59 tests: ` Test Files  2 passed (2)` / `      Tests  59 passed (59)`.
    Nothing in a sibling's file was touched to make this read better.

## What this leaf changed outside the gates

* `docs/pc-findings/pc4.md` — the prop contract, the three decisions worth reviewing, and two
  corrections the **parent** should fold into `docs/PORTFOLIO-COMMAND-CENTER.md` §2.2: a target
  weight *is* real for a screen-backed rebalance (§2.2's blanket "no target weights" is about a
  *stored* target), and turnover is blocked for a specific reason — it is a share of value, and
  counting names instead would be a different number under the same label.
