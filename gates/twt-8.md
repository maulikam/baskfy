# TW8 — the pages: the web app's read-only view and the desk's actionable one

**Plan:** `docs/twt/06-module-plan.md` § TW8. **Spec:** `docs/twt/05-ui-spec.md` §1–3.
**Built ahead of its data.** TW4 and TW5 are not finished, so this module is written against the
DOCUMENTED contract (`03-data-model.md`) and fed by fixtures, exactly as the portfolio panels were.
The parent wires the real reads when the producing modules land.

- [x] G1: Web `/twt` per `05` §1 — the gate, today's tight names with the point-in-time sentence,
      the open book with its highest-high, its trigger and **the distance to it**, and the
      half-size counter.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/twt src/app --silent --reporter=basic -t "twt" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: `Test Files  5 passed | 18 skipped (23)` / `Tests  54 passed | 183 skipped (237)`.
  Built: `src/app/(app)/twt/page.tsx`, `src/components/twt/{gate-card,tight-names,open-positions,
  half-size-counter,figure,disclosure}.tsx` over the pure `src/lib/twt/{copy,numbers,view}.ts`.
  Fed by `src/lib/twt/__tests__/fixtures.ts`, written to `03-data-model.md` — TW4/TW5 have not
  landed, so `fetchToday` answers null in production today and the page renders its empty state.
  The distance to the trigger is asserted at 17.5% from ₹441.20 against a ₹364.00 trigger.

- [x] G2: **Read-only.** Every non-GET route under `/twt` is a 405 except the two that change no
      money (a note and a dismissal). The web app has no order path and does not gain one.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/auth/__tests__ src/app --silent --reporter=basic -t "read-only|405" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: `Test Files  1 passed | 21 skipped (22)` / `Tests  8 passed | 265 skipped (273)`.
  `src/app/(app)/twt/__tests__/read-only.test.tsx` walks `(app)/twt`, `lib/twt` and
  `components/twt` (14 source files): no `use server`, no `<form`, no `type="submit"`, no
  fetch/token outside the one server-only reader, no order-shaped verb, and — the 405 test — no
  `route.ts` and no `actions.ts` anywhere under the tree, which is what makes every non-GET method
  Next's own 405. Neither money-free mutation is built; DECISIONS-TW TW8.7 says why.

- [x] G3: `/twt/backtest` per `05` §3, with `01` §8's caveats rendered as a component rather than
      a footnote (house rule 9's second half).
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/twt --silent --reporter=basic -t "backtest|caveat" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: `Test Files  2 passed | 2 skipped (4)` / `Tests  10 passed | 38 skipped (48)`.
  `src/app/(app)/twt/backtest/page.tsx` over `components/twt/backtest-card.tsx`: the latest
  FINISHED run per source side by side (never mixed), the eleven metrics, the yearly table, the
  equity curve, the gate-on/gate-off comparison and the drift warning naming both numbers.
  `caveats.tsx` is asserted **by document position** — `compareDocumentPosition` says the first
  figure FOLLOWS the caveats — because a caveat block at the foot of the page satisfies presence
  and fails the rule.

- [x] G4: The point-in-time sentence and the caveats appear **verbatim** — this is the sentence
      that tells a reader why the live scan shows fewer names than Chartink on some days.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/twt --silent --reporter=basic -t "point-in-time" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: `Test Files  1 passed | 3 skipped (4)` / `Tests  2 passed | 46 skipped (48)`.
  The sentence is asserted as one string, including the em dash: "Computed point-in-time from the
  close of 10 Sept 2026. Chartink's own backtest export uses the week's final close on every day
  of that week, so it names some stocks this screen does not — see the method note." A second test
  asserts it survives a session in which no name held the pattern. The caveats' numbers are pinned
  in `backtest.test.tsx`; DECISIONS-TW TW8.2 records the one phrase deliberately not transcribed
  ("in this repository" is a fact about where code is kept).

- [x] G5: **No bare dash**, and nothing internal on screen. Every unavailable figure carries its
      reason, in a reader's words — no route, no column name, no file path, no host. The portfolio
      work established both rules and `no-internals.test.tsx` is the pattern to copy.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/twt --silent --reporter=basic -t "unavailable|internal" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: `Test Files  1 passed | 3 skipped (4)` / `Tests  10 passed | 38 skipped (48)`.
  `src/components/twt/__tests__/no-internals.test.tsx`, copied from the portfolio scan and
  extended: it renders every twt surface — full session, nothing computed, an unprotected line, the
  backtest with and without a run, the desk page — and reads the result as text. Seven banned
  patterns, including one the portfolio scan does not have: an UPPER_SNAKE setting or alert name,
  which is why the desk badge reads "DRY RUN" and a skip says "every position slot is taken". The
  no-dash half checks every `td` and `dd`: none may be "—", "–", "-", "", "N/A", "null" or "NaN".

- [x] G6: The desk page per `05` §2 — exits first, then entries, then the book, then Confirm, and
      the 15:15 strip. `DRY_RUN` is a **badge**, and an expired plan's buttons are **absent**
      rather than disabled: a disabled Confirm on an expired plan invites a reload and a retry.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/twt --silent --reporter=basic -t "desk|expired" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: `Test Files  2 passed | 2 skipped (4)` / `Tests  16 passed | 32 skipped (48)`.
  `components/twt/desk/desk-plan.tsx` over the pure `lib/twt/desk.ts`. Order asserted by
  `compareDocumentPosition`: exits before entries before the open positions; inside the exits, a
  missing stop before one that only needs raising. The mode is a badge inside the session strip.
  **Expired**: `queryAllByRole("button", {name: /^Confirm /})` is 0 AND `button[disabled]` is 0 AND
  `[aria-disabled='true']` is 0 — absence is asserted, never a disabled attribute, because
  asserting `disabled` would pass on the implementation the rule forbids. An unparseable expiry is
  treated as expired. ⚠ The page is mounted on NO web route: DECISIONS-TW TW8.1.

- [x] G7: Three states render and explain themselves: an empty sleeve, a SHUT gate, and a naked
      line (an open position with no resting GTT).
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/twt --silent --reporter=basic -t "state|empty|shut|naked" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: `Test Files  4 passed (4)` / `Tests  7 passed | 41 skipped (48)`.
  Empty: `twt-open-empty` explains that nothing is open and why that is ordinary; the page-level
  test asserts a null payload reads as "nothing has been read yet", never as a quiet market.
  SHUT: the gate badge reads SHUT and the same sentence says "no new entries" AND "managed exactly
  as always ... the stops stay where they are" — and `/sell everything/i` is asserted absent.
  Naked: "No stop resting" in the negative tone, the stop and the distance both carrying their
  reason, `0.0%` asserted absent, plus the desk's 15:15 band naming NAKEDCO and one re-arm slot.

- [x] G8: Light and dark both pass the existing contrast check, and `--muted-foreground` on
      `--muted` is among the pairs asserted — it failed AA until 11 Sep 2026.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/__tests__/contrast.test.ts --silent --reporter=basic 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: `Test Files  1 passed (1)` / `Tests  55 passed (55)`. `--muted-foreground` on
  `--muted` is in `TEXT_ON_SURFACE` (added 11 Sep 2026) and passes in both themes. No new token
  was introduced: every twt component uses the semantic tokens the file already asserts —
  `positive`/`negative`/`warning`/`accent`/`muted` and their `-muted` surfaces — and no raw
  Tailwind palette colour (the sibling sleeve's `amber-600/40` was deliberately not copied).

- [x] G9: The whole web suite is green and `pnpm run lint` reports zero errors.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run --silent 2>&1 | grep -E "Test Files" && pnpm run lint 2>&1 | tail -3
  EXPECT: /0 errors/
  EVIDENCE: `Test Files  165 passed (165)` / `Tests  2933 passed (2933)` — up from the
  157 / 2,832 baseline, so TW8 adds 8 test files and 101 tests and breaks none of the existing
  ones. `pnpm run lint` (`tsc --noEmit && eslint . && check-shadowed-routes`) ends
  `✖ 1 problem (0 errors, 1 warning)` and `ok — 30 redirected source(s), no page file shadowed`:
  **zero errors**. The one warning is the pre-existing `react-hooks/incompatible-library` on
  `src/components/data/data-table.tsx:349`, untouched by this module.
  Worth recording, because it was true for most of this module's run and is no longer: `tsc`
  failed at HEAD on `src/components/portfolio/__tests__/no-internals.test.tsx(68,7)` (TS2739,
  missing `cash` and `dividends`) — a file TW8 does not own, proven pre-existing by re-running
  `tsc -p` with all three `twt` trees excluded and getting the same single error. The portfolio
  owner fixed it mid-run in commit `1f6de10`, and this evidence was re-taken afterwards rather
  than left claiming a green that had not been re-run — which is the same mistake that commit
  message names.

<!-- A checked box whose EVIDENCE reads "pending" is UNMET. ABANDON: G<n> <reason> is the honest exit. -->
