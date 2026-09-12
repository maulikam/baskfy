# Gate — "Scan now" on the Volume Breakout and Three Weeks Tight pages (Leaf 5)

**12 Sep 2026.** `PLAN-SCAN-SYNC.md` leaf 5. The swing hub has had a "Scan now" button since
SW15; `/vbt` and `/twt` have not. This ledger is the UI half, built to the contract in
`PLAN-SCAN-SYNC.md` §"The contract" — `POST /vbt/scan` and `POST /twt/scan` answering **202**
queued, **409** one already in flight, **429** one a minute, with
`GET /{sleeve}/scan/{run_id}` carrying `QUEUED | RUNNING | DONE | FAILED`.

**Leaves 3 and 4 build those routes; this leaf does not wait for them.** Every test here mocks
the server action exactly as `app/(app)/swing/__tests__/page.test.tsx` mocks `scanNow`, so the
UI is green before a route exists and stays green when one lands.

**What this leaf reverses, deliberately, and where it is recorded.** Both target trees carried a
test asserting they have *no server action at all* (DECISIONS-VB **VB8.4**, DECISIONS-TW
**TW8.7** — "the stronger assertion that is available"). Maulik asked for the button, so the
stronger assertion is no longer available; it is replaced by the swing hub's own census shape —
**exactly one** action, named `scanNow`, and nothing else, ever. Recorded in
`docs/vbt/DECISIONS-VB.md` §VB14 and `docs/twt/DECISIONS-TW.md` §TW11.

**What this leaf must not do:** place an order, reach the order gateway, set a sleeve capital,
flip an execution flag, write to the box, or deploy. A scan queues a detector; it is money-free
by construction and rows S8/S9/S14 below say so with runnable checks.

**Paths are relative to the repo root** `/Users/maulikdave/Documents/projects/baskfy`;
`W=decile-blueprint/apps/web`.

**Test ids, for the record:** `vbt-scan-now` / `vbt-scan-status` and
`twt-scan-now` / `twt-scan-status`.

---

- [x] S1: **The control exists on both trees, in the template's shape** — a client component per
      tree, an `actions.ts` per tree, a server-only write helper per sleeve, and copy in a
      `copy.ts` rather than inline in JSX.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && for f in "src/app/(app)/vbt/_components/scan-now.tsx" "src/app/(app)/vbt/actions.ts" "src/app/(app)/vbt/copy.ts" "src/lib/vbt/write.ts" "src/app/(app)/twt/_components/scan-now.tsx" "src/app/(app)/twt/actions.ts" "src/app/(app)/twt/copy.ts" "src/lib/twt/write.ts"; do printf '%s=%s ' "$(basename "$(dirname "$f")")/$(basename "$f")" "$(test -f "$f" && echo 1 || echo 0)"; done; echo
  EXPECT: /^_components\/scan-now.tsx=1 vbt\/actions.ts=1 vbt\/copy.ts=1 vbt\/write.ts=1 _components\/scan-now.tsx=1 twt\/actions.ts=1 twt\/copy.ts=1 twt\/write.ts=1 $/m
  EVIDENCE: `_components/scan-now.tsx=1 vbt/actions.ts=1 vbt/copy.ts=1 vbt/write.ts=1 _components/scan-now.tsx=1 twt/actions.ts=1 twt/copy.ts=1 twt/write.ts=1`. Eight files, the swing template's shape twice: a client component, a `use server` actions file, a copy module, a server-only write helper per sleeve.

- [x] S2: **Exactly one server action per tree and it is `scanNow`.** The census is structural —
      it walks the files, so an action added in a new file fails the moment it exists.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && printf 'vbt=%s twt=%s\n' "$(grep -rlE '^\s*"use server"' 'src/app/(app)/vbt' | wc -l | tr -d ' ')/$(grep -hoE '^export (async )?function [A-Za-z]+' 'src/app/(app)/vbt/actions.ts' | sed 's/.*function //' | paste -sd, -)" "$(grep -rlE '^\s*"use server"' 'src/app/(app)/twt' | wc -l | tr -d ' ')/$(grep -hoE '^export (async )?function [A-Za-z]+' 'src/app/(app)/twt/actions.ts' | sed 's/.*function //' | paste -sd, -)"
  EXPECT: /^vbt=1\/scanNow twt=1\/scanNow$/m
  EVIDENCE: `vbt=1/scanNow twt=1/scanNow`. One `use server` module per tree, one exported function in each, and the census test (S8) fails on any second one the moment the file exists.

- [x] S3: **The write helper admits one path and one method.** The path is a closed union type,
      not a pattern: a pattern that admits `/vbt/scan` would admit the desk's confirm route too,
      and that route is precisely what Track C §4 keeps out of this application.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && printf 'vbt_paths=%s vbt_methods=%s twt_paths=%s twt_methods=%s\n' "$(grep -c 'export type VbtWritePath = "/vbt/scan";' src/lib/vbt/write.ts)" "$(grep -coE 'method: "(POST|PUT|PATCH|DELETE)"' src/lib/vbt/write.ts)" "$(grep -c 'export type TwtWritePath = "/twt/scan";' src/lib/twt/write.ts)" "$(grep -coE 'method: "(POST|PUT|PATCH|DELETE)"' src/lib/twt/write.ts)"
  EXPECT: /^vbt_paths=1 vbt_methods=1 twt_paths=1 twt_methods=1$/m
  EVIDENCE: `vbt_paths=1 vbt_methods=1 twt_paths=1 twt_methods=1`. `export type VbtWritePath = "/vbt/scan";` and `export type TwtWritePath = "/twt/scan";` — a closed union of one literal, not a template pattern, and POST the only method in either file.

- [x] S4: **The button says which of 202 / 409 / 429 happened, in a reader's words.** No status
      code, no server `detail` string, and no raw run state reaches the screen — the four
      sentences are named constants and a unit test asserts each one against its status.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run "src/app/(app)/vbt/__tests__/scan-now.test.tsx" "src/app/(app)/twt/__tests__/scan-now.test.tsx" -t "says" 2>&1 | tail -8
  EXPECT: /Tests {2}\d+ passed/
  EVIDENCE: `Tests  20 passed | 22 skipped (42)` across both suites. 202 -> "Scan started. This page updates on its own while it runs.", 409 -> "A scan is already running. This page updates when it finishes.", 429 -> "A scan has just been run. The next one can start in about a minute.", 401/403 -> "You are signed out, so nothing was run. Sign in and try again.", anything else -> "The scan could not be started just now, so nothing changed. Try again in a moment." One test asserts no sentence contains a three-digit number; another asserts a FAILED run shows none of its own reason (`ScanNotRunnable: ... ohlcv_daily ...` stays off the screen).

- [x] S5: **A real button, with a focus ring and a disabled state while in flight.** It is a
      `<button type="submit">` inside a form bound to the action — never a div, never a link.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run "src/app/(app)/vbt/__tests__/scan-now.test.tsx" "src/app/(app)/twt/__tests__/scan-now.test.tsx" -t "accessible" 2>&1 | tail -8
  EXPECT: /Tests {2}\d+ passed/
  EVIDENCE: `Tests  12 passed | 30 skipped (42)`. `button.tagName === "BUTTON"`, `type="submit"`, inside a real `<form>`, focusable (`document.activeElement` is the button after `.focus()`), no `outline-none` on its class list — the ring is `globals.css`'s `:focus-visible`. Disabled with the label "Scanning…" while the last run is QUEUED or RUNNING.

- [x] S6: **The result is announced.** One `role="status" aria-live="polite"` region per control,
      present in every state so a screen reader hears the change rather than a new node.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && printf 'vbt=%s twt=%s\n' "$(perl -0777 -pe 's{/\*.*?\*/}{}gs' 'src/app/(app)/vbt/_components/scan-now.tsx' | grep -c 'aria-live="polite"')" "$(perl -0777 -pe 's{/\*.*?\*/}{}gs' 'src/app/(app)/twt/_components/scan-now.tsx' | grep -c 'aria-live="polite"')"
  EXPECT: /^vbt=1 twt=1$/m
  EVIDENCE: `vbt=1 twt=1` after block comments are stripped (the file's own header quotes the attribute while explaining it). One `role="status" aria-live="polite"` region per control, present before anything has happened rather than mounted on success.

- [x] S7: **Dark mode through the existing semantic tokens, and no new colour.** The control uses
      the shared `Button` and the `text-muted-foreground` / `text-negative` tokens only — no hex,
      no `rgb(`, no raw Tailwind palette step.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && grep -hoE '#[0-9a-fA-F]{3,8}\b|rgba?\(|\b(text|bg|border)-(red|green|blue|amber|slate|zinc|gray|neutral|stone|orange|yellow|emerald|teal|cyan|sky|indigo|violet|purple|pink|rose)-[0-9]{2,3}\b' 'src/app/(app)/vbt/_components/scan-now.tsx' 'src/app/(app)/twt/_components/scan-now.tsx' | sort -u | wc -l | tr -d ' '
  EXPECT: /^0$/m
  EVIDENCE: `0`. No hex, no `rgb(`, no raw palette step in either component. Colour is `text-muted-foreground` and `text-negative`, plus the shared `Button`'s `outline` variant — all semantic tokens that already answer to the theme.

- [x] S8: **Nothing on either page can place an order, and the read-only census still passes.**
      This is non-negotiable #1 and law 2, asserted over both whole trees plus their read and
      write helpers.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run "src/app/(app)/vbt/__tests__/read-only.test.tsx" "src/app/(app)/twt/__tests__/read-only.test.tsx" 2>&1 | tail -8
  EXPECT: /Tests {2}\d+ passed/
  EVIDENCE: `Tests  27 passed (27)` — 12 in the vbt census, 15 in the twt one. Both now enumerate every export of every `use server` module and require exactly `["scanNow"]`; both pin the hub at one form and one submit button bound to an action rather than a URL; both assert the write helper's closed path union and single method; the twt census additionally asserts the **write path** can name neither `sleeve_capital` nor an execution switch, and its 405 row still finds no `route.ts` under the tree. Recorded as DECISIONS-VB VB14 and DECISIONS-TW TW11.

- [x] S9: **No internal identifier reaches the reader from the new control** — no route, no field
      name, no setting name, no source file, no host. The same scan
      `components/twt/__tests__/no-internals.test.tsx` runs over `/twt`, applied to every state
      this control can be in on both sleeves.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run "src/app/(app)/vbt/__tests__/scan-now.test.tsx" "src/app/(app)/twt/__tests__/scan-now.test.tsx" -t "internal" 2>&1 | tail -8
  EXPECT: /Tests {2}\d+ passed/
  EVIDENCE: `Tests  4 passed | 38 skipped (42)`. The seven BANNED patterns from `components/twt/__tests__/no-internals.test.tsx` — an API route, an API path, a host, a source file, a module path, a snake_case field, a SCREAMING_SNAKE setting — run over the rendered text in all five states (no run, QUEUED, RUNNING, DONE, FAILED-with-an-internal-error) and over all five outcome sentences. Nothing hits.

- [x] S10: **It polls while a run is on its way and stops when it lands.** `router.refresh()`
      every 5 s while the last run is queued or running; nothing at all on done or failed — the
      swing mechanism, for the swing reason: the rows come from a server component.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run "src/app/(app)/vbt/__tests__/scan-now.test.tsx" "src/app/(app)/twt/__tests__/scan-now.test.tsx" -t "polls" 2>&1 | tail -8
  EXPECT: /Tests {2}\d+ passed/
  EVIDENCE: `Tests  8 passed | 34 skipped (42)`. QUEUED: 1 refresh at 5 s, 3 by 15 s. RUNNING then re-rendered DONE: the count stops at 1 across a further 30 s. FAILED and "no run ever": zero refreshes in 30 s. Unmounted mid-run: zero — the interval is cleared, so a navigated-away page holds no timer.

- [x] S11: **The control is on both pages**, rendered through the page's own header, and the
      page tests say so over a mocked read.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run "src/app/(app)/vbt/__tests__/page.test.tsx" "src/app/(app)/twt/__tests__/page.test.tsx" 2>&1 | tail -8
  EXPECT: /Tests {2}\d+ passed/
  EVIDENCE: `Tests  30 passed (30)` — 19 vbt, 11 twt. Both pages render the control in the header; with a RUNNING run the status reads "Scanning now." and the button is disabled; `getAllByRole("button")` on each whole page returns exactly `["Scan now"]`; and each page still renders the sentence "nothing on this page can place an order". The twt page offers the button on a hub with nothing computed at all, which is the state that sleeve is genuinely in.

- [x] S12: **The whole web suite is green.** Not just the new files — the census tests in other
      trees read this one's source.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run 2>&1 | tail -6
  EXPECT: /Tests {2}\d+ passed \(\d+\)/
  EVIDENCE: `Test Files  169 passed (169)` / `Tests  3052 passed (3052)` in 31.72 s. The suite includes the censuses in other trees that read this one's source.

- [x] S13: **Types clean.** No `any`, no `// @ts-ignore` — house rule 3.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec tsc --noEmit 2>&1 | tail -5; echo "exit=$?"
  EXPECT: /^exit=0$/m
  EVIDENCE: `tsc_exit=0`, no output. No `any`, no `// @ts-ignore`, no `// @ts-expect-error` anywhere in the new files.

- [x] S14: **Lint clean — 0 errors.**
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec eslint . 2>&1 | tail -5; echo "exit=$?"
  EXPECT: /^exit=0$/m
  EVIDENCE: `eslint_exit=0`, `✖ 1 problem (0 errors, 1 warning)` — the single warning is the pre-existing `react-hooks/incompatible-library` on `components/data/data-table.tsx:349`, untouched by this leaf.

- [x] S15: **This leaf deployed nothing, wrote nothing to the box, funded no sleeve and flipped no
      flag.** `PLAN-SCAN-SYNC.md` hard rules 1, 2 and 4 plus the root safety rails. The check is
      over the leaf's own diff: no deploy script ran, and no changed file names a capital or an
      execution flag.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && P="decile-blueprint/apps/web/src/app/(app)/vbt decile-blueprint/apps/web/src/app/(app)/twt decile-blueprint/apps/web/src/lib/vbt decile-blueprint/apps/web/src/lib/twt"; printf 'flags=%s deploy=%s\n' "$( { git diff -- $(echo $P) ':(exclude)*__tests__*'; git ls-files -o --exclude-standard -- $(echo $P) ':(exclude)*__tests__*' | xargs cat; } | grep -cE 'sleeve_capital|BASKFY_TWT_EXECUTION_ENABLED|DRY_RUN|place_order|OrderGateway' )" "$(git status --porcelain -- tools/deploy | wc -l | tr -d ' ')"
  EXPECT: /^flags=0 deploy=0$/m
  EVIDENCE: `flags=0 deploy=0`. No file this leaf owns names a sleeve capital, an execution flag, `DRY_RUN`, `place_order` or the gateway (the census tests' own forbidden-word lists are excluded from the scan — they are the guard, not the thing guarded). Nothing under `tools/deploy/` changed; no deploy script was run. Other leaves' files show in `git status`, and none of them is this leaf's.

---

## Ledger

**15 / 15 complete. 0 incomplete, 0 abandoned.**

Test ids, as built: **`vbt-scan-now`** (the form) and **`vbt-scan-status`** (the live region) on
`/vbt`; **`twt-scan-now`** and **`twt-scan-status`** on `/twt`.

**Nothing was deployed.** The parent deploys once, at the end (`PLAN-SCAN-SYNC.md` hard rule 1).

**What this leaf needs from leaves 3 and 4, and what it does not.** It does not need their routes
to be green — the action is mocked in every test. When they land, the page reads the last run off
the day's payload: `last_scan` if it is inlined (the shape `/swing` serves), or `last_scan_id`
resolved through the contract's own `GET /{sleeve}/scan/{run_id}`. Either shape works without a
change here; neither is required for the button to be pressable.
