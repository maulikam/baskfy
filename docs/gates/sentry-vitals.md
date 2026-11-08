# Sentry / web-vitals — `Cannot read properties of undefined (reading 'startTime')`

**What Maulik reported (12 Sep 2026).** On the deployed app, in his browser console:

```
Uncaught TypeError: Cannot read properties of undefined (reading 'startTime')
    at et.reportAllChanges (<anonymous>:2:19429)
    ... at n.timeout, requestIdleCallback
```

**The brief's hypothesis, and why it was reasonable.** `reportAllChanges` is a web-vitals name, it
appears nowhere in `src/`, and `@sentry/nextjs@^10.70.0` is a dependency that bundles web-vitals
and uses `reportAllChanges` for browser performance instrumentation. So: Sentry's code, not ours.

**The finding: it is not Sentry, and it is not us.** Three independent lines of evidence, any one
of which is sufficient, and they do not share an assumption:

1. **There is no browser Sentry in this app at all.** No `instrumentation-client.*`, no
   `withSentryConfig`. `src/instrumentation.ts` is server-only by design and says so in its own
   docstring; `docs/DECISIONS.md` §17.12 records the trade (browser exceptions are not reported,
   in exchange for the docs/11 250 KB client budget). V1, V2.
2. **The shipped client bundle is empty of it.** A fresh `next build` produces 158 client chunks
   containing zero `@sentry`, zero `reportAllChanges`, zero web-vitals symbols — while the *server*
   output does contain `@sentry`, which is the control proving the grep and the build are real.
   V3, V4.
3. **`reportAllChanges` is not a callable anywhere in the dependency tree**, so it cannot produce
   the stack frame `et.reportAllChanges` even hypothetically. Across all 164 occurrences in
   `node_modules` — Sentry 10.70.0's bundled web-vitals and Next 15.5.23's compiled web-vitals
   alike — it is only ever an options **flag being read** (`opts.reportAllChanges`,
   `b.reportAllChanges`) or a boolean being **set** (`reportAllChanges: true`). Never a function,
   never a method. A stack frame names a function. V5.

**Where it does come from.** `<anonymous>:2:19429` is not a URL — every script this app loads is
served from `/_next/static/chunks/*.js` and appears in a stack under that name. `<anonymous>` /
`VM###` is what Chrome DevTools calls code evaluated from a *string*: `eval`, `new Function`, or a
browser extension injecting into the page's MAIN world. This app's production CSP is
`script-src 'self' 'nonce-…' 'strict-dynamic'` with **no `unsafe-eval`** (V6), so the page itself
cannot evaluate a string — an attempt would raise a CSP violation, not a `TypeError`. Extension
content scripts are exempt from page CSP. The behaviour matches a vitals-measuring browser
extension (Google's Web Vitals extension is the common one: it wraps web-vitals in its own class,
sets `reportAllChanges: true`, and schedules through `requestIdleCallback`), which is consistent
with every detail of the trace — but the load-bearing claim here is the negative one, which is
proven: it is not ours and not Sentry's.

**Harmful or cosmetic? Cosmetic, and narrower than "cosmetic" usually means.** The brief's worry
was that it would "bury real errors in the console and in Sentry's own issue stream". It cannot
touch the issue stream: this app reports **no** browser exceptions to Sentry — there is no client
SDK to report them (V1–V4). And it is not "on every page load" for users; it is in the console of
a browser running the extension that injects it. It is not in our bundle, so it is not in anyone
else's browser. There is no error budget being consumed, no user-visible failure, and nothing to
fix in this repo.

**So the fix is not a fix to the reported symptom — there is nothing of ours to change.**
Deliberately *not* done, and why:

| Option | Rejected because |
|---|---|
| Pin / upgrade `@sentry/nextjs` | Would change nothing. Sentry's browser code never reaches the browser; its bundled web-vitals has no such fault, and no version of it can emit that frame. |
| Disable the vitals integration | There is no integration to disable. `Sentry.init` runs on the server only, `tracesSampleRate: 0`, and browser integrations are never constructed. |
| Remove `@sentry/nextjs` | It is doing real work — server components, route handlers, server actions and middleware report through `register()` / `onRequestError`. |
| Guard / swallow the error | Forbidden by house rule 3, and it is not our error to catch. |

**What was actually shipped, and why it is the right output.** The one genuine weakness the
investigation exposed is that "Sentry never enters the browser bundle" was a property held *only
by two files not existing*. Nothing asserted it. Adding `instrumentation-client.ts` is a three-line
change that no test, lint rule or type error would have objected to, and its cost would have
appeared as ~40 KB of SDK in every route's first load rather than as a failure — a decision
reversed by accident. House rule 2 asks tests to assert the spec; the spec was written in two
places with nothing holding it. `src/lib/__tests__/sentry-client-boundary.test.ts` now holds it,
and V7 proves that test can fail.

**Scope kept.** No dependency changed, no Sentry config changed, no `next.config.ts` change,
nothing deployed. Nothing touched under `src/app/(app)/twt/`, `src/lib/twt/`, `src/app/(app)/vbt/`
or `src/app/(app)/swing/`.

---

- [x] V1: No client-side Sentry entry point exists — neither of the two files that would pull the
      browser SDK into every page. This is the architecture `src/instrumentation.ts` documents and
      `docs/DECISIONS.md` §17.12 decided; it was previously unenforced.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && printf 'client_instr=%s withSentryConfig=%s\n' "$(find . -maxdepth 2 -name 'instrumentation-client.*' -not -path './node_modules/*' -not -path './.next*' | wc -l | tr -d ' ')" "$(grep -c withSentryConfig next.config.ts)"
  EXPECT: /^client_instr=0 withSentryConfig=0$/m
  EVIDENCE: client_instr=0 withSentryConfig=0. Next accepts `instrumentation-client` at the project root or in `src/`, with any of four extensions; the `find` covers all eight spellings. `withSentryConfig` appears nowhere in `next.config.ts` — the export is `withMdx(nextConfig)`.

- [x] V2: `@sentry/nextjs` is reachable from exactly one module, on the server, behind a dynamic
      import and a DSN guard — so no bundle that does not call it can contain it.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && printf 'importers=%s static=%s dynamic=%s dsn_guard=%s\n' "$(grep -rl '@sentry/' src --include='*.ts' --include='*.tsx' | grep -v __tests__ | tr '\n' ' ' | sed 's/ $//')" "$(grep -cE '^import .*@sentry/' src/instrumentation.ts)" "$(grep -cE 'await import\("@sentry/nextjs"\)' src/instrumentation.ts)" "$(grep -c 'if (!dsn) return;' src/instrumentation.ts)"
  EXPECT: /^importers=src\/instrumentation\.ts static=0 dynamic=2 dsn_guard=1$/m
  EVIDENCE: importers=src/instrumentation.ts static=0 dynamic=2 dsn_guard=1. One importer, zero top-level static imports (a static one would drag the Node SDK into the edge bundle), two `await import("@sentry/nextjs")` sites — `register()` and `onRequestError` — and `register()` returns before either if `SENTRY_DSN` is unset, which is why a laptop and the Playwright suite carry none of the machinery.

- [x] V3: The shipped **client** bundle of a fresh production build contains no Sentry and no
      web-vitals — with a positive control in the same line, so a silently-broken grep cannot pass
      this row.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && { [ -d .next-vitals/static ] || BASKFY_WEB_DIST_DIR=.next-vitals pnpm exec next build >/dev/null 2>&1; }; printf 'chunks=%s client_sentry=%s client_vitals=%s grep_works=%s\n' "$(find .next-vitals/static -name '*.js' | wc -l | tr -d ' ')" "$(grep -rl '@sentry' .next-vitals/static 2>/dev/null | wc -l | tr -d ' ')" "$(grep -rlE 'reportAllChanges|bindReporter|onLCP|onCLS' .next-vitals/static 2>/dev/null | wc -l | tr -d ' ')" "$(grep -rl 'useState' .next-vitals/static 2>/dev/null | wc -l | tr -d ' ')"
  EXPECT: /^chunks=[0-9]{2,} client_sentry=0 client_vitals=0 grep_works=[1-9][0-9]*$/m
  EVIDENCE: chunks=158 client_sentry=0 client_vitals=0 grep_works=71 (`next build` exit 0, 12 Sep 2026). 158 minified client chunks, not one containing `@sentry`, `reportAllChanges`, `bindReporter`, `onLCP` or `onCLS`; `useState` is found in 71 of them, which is the control showing the grep does match minified chunk content. The three stale build dirs already in the tree (`.next-build`, `.next-gate`, `.next-e2e`) give the same answer.

- [x] V4: The same build's **server** output does contain `@sentry` — the control that proves the
      zeros in V3 are a boundary being respected, not Sentry having disappeared from the install.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && { [ -d .next-vitals/server ] || BASKFY_WEB_DIST_DIR=.next-vitals pnpm exec next build >/dev/null 2>&1; }; printf 'server_sentry=%s installed=%s\n' "$(grep -rl '@sentry' .next-vitals/server 2>/dev/null | wc -l | tr -d ' ')" "$(node -p "require('./node_modules/@sentry/nextjs/package.json').version")"
  EXPECT: /^server_sentry=[1-9][0-9]* installed=10\.70\.0$/m
  EVIDENCE: server_sentry=3 installed=10.70.0. `@sentry` appears only under `.next-vitals/server/**` and the mirrored `.next-vitals/standalone/**/server/**` — never under `static/`. The dependency is installed and in use; it is simply on the other side of the boundary.

- [x] V5: `reportAllChanges` is never a callable in any installed package, so the reported frame
      `at et.reportAllChanges` cannot originate from Sentry 10.70.0 or Next 15.5.23. This is the
      row that refutes the hypothesis rather than merely failing to confirm it.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && printf 'callable=%s occurrences=%s\n' "$(grep -rhoE 'reportAllChanges[[:space:]]*\(|reportAllChanges[[:space:]]*[:=][[:space:]]*(function|\(|[A-Za-z_$]+[[:space:]]*=>)' node_modules apps/web/node_modules 2>/dev/null | wc -l | tr -d ' ')" "$(grep -rho 'reportAllChanges' node_modules apps/web/node_modules 2>/dev/null | wc -l | tr -d ' ')"
  EXPECT: /^callable=0 occurrences=[1-9][0-9]*$/m
  EVIDENCE: callable=0 occurrences=164. Every one of the 164 is a flag read or a boolean set — `bindReporter(onReport, metric, LCPThresholds, opts.reportAllChanges)` in `@sentry/browser-utils@10.70.0`'s web-vitals, `b.reportAllChanges` in `next/dist/compiled/web-vitals`, and `{ reportAllChanges: true }` at Sentry's call sites. Zero function declarations, zero method shorthands, zero arrow assignments. Chrome names a stack frame after a *function*; there is no function of that name to name.

- [x] V6: The production CSP forbids evaluating a string as JavaScript, so an executing
      `<anonymous>` / VM script is by construction not this page's own code.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec vitest run src/lib/__tests__/csp.test.ts -t "allows eval only in development" 2>&1 | grep -E "Tests +[0-9]+ (passed|failed)"
  EXPECT: /Tests +1 passed/
  EVIDENCE: The pre-existing `csp.test.ts` asserts `contentSecurityPolicy(nonce, false)` and `staticContentSecurityPolicy(false)` both exclude `'unsafe-eval'`; only `isDev` adds it (`src/middleware.ts:156,172`). So in production `eval`/`new Function` are refused by the browser, and a refusal surfaces as a CSP violation, not as `TypeError: Cannot read properties of undefined`. Code that *ran* from `<anonymous>` therefore entered the page outside the CSP — which is what an extension's MAIN-world injection does and what page code cannot do.

- [x] V7: The new boundary test genuinely fails when the invariant is broken. Asserted by mutation,
      not by assertion — a green test that cannot go red would be worse than no test, and this repo
      has already paid once for a test pinned to the wrong thing (root CLAUDE.md, 9 Sep 2026).
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && F=instrumentation-client.ts; trap 'rm -f "$F"' EXIT INT TERM; echo 'export const x = 1;' > "$F"; M=$(pnpm exec vitest run src/lib/__tests__/sentry-client-boundary.test.ts 2>&1 | grep -oE 'Tests +[0-9]+ failed' || echo 'none'); rm -f "$F"; C=$(pnpm exec vitest run src/lib/__tests__/sentry-client-boundary.test.ts 2>&1 | grep -oE 'Tests +[0-9]+ passed'); printf 'mutated=[%s] clean=[%s] leftover=%s\n' "$M" "$C" "$(find . -maxdepth 1 -name 'instrumentation-client.*' | wc -l | tr -d ' ')"
  EXPECT: /^mutated=\[Tests +1 failed\] clean=\[Tests +12 passed\] leftover=0$/m
  EVIDENCE: mutated=[Tests  1 failed] clean=[Tests  12 passed] leftover=0. Five mutations were run by hand and all five were caught: creating `instrumentation-client.ts` (1 failed), appending `withSentryConfig` to `next.config.ts` (1), adding a second `@sentry` importer (1), adding a `"use client"` module importing `@sentry` (2 — the importer row and the client-component row), and converting `instrumentation.ts`'s dynamic import to a static one (1). The tree was restored after each; `git status` on `src/instrumentation.ts` and `next.config.ts` is clean.

- [x] V8: `tsc --noEmit` is clean; the new file draws not one ESLint error *or warning*; and no
      ESLint error anywhere in the app sits outside the three trees being edited concurrently.
      ⚠️ The bare `pnpm exec eslint .` count is **3 errors** as this is written, and all three are
      in another agent's file — so the row attributes them instead of asserting a total that is not
      mine to hold.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && pnpm exec tsc --noEmit && J=$(pnpm exec eslint . -f json 2>/dev/null); printf 'tsc=ok mine_eslint=%s foreign_ground_error_files=%s mine=%s\n' "$(printf '%s' "$J" | python3 -c "import json,sys; d=json.load(sys.stdin); print(sum(f['errorCount']+f['warningCount'] for f in d if 'sentry-client-boundary' in f['filePath']))")" "$(printf '%s' "$J" | python3 -c "import json,sys,re; d=json.load(sys.stdin); print(len([f for f in d if f['errorCount']>0 and not re.search(r'/(twt|vbt|swing)/', f['filePath'])]))")" "$(pnpm exec vitest run src/lib/__tests__/sentry-client-boundary.test.ts 2>&1 | grep -oE 'Tests +[0-9]+ passed')"
  EXPECT: /^tsc=ok mine_eslint=0 foreign_ground_error_files=0 mine=Tests +12 passed$/m
  EVIDENCE: tsc=ok mine_eslint=0 foreign_ground_error_files=0 mine=Tests  12 passed. TypeScript is clean across the app. The 3 ESLint errors are all in `src/lib/swing/__tests__/read-contract.test.ts` — `@typescript-eslint/require-await` at 26:3 and two `no-useless-assignment` at 77:9 and 111:9 — a file created by the swing agent during this session. Zero error files outside `twt/`/`vbt/`/`swing/`. The one remaining warning, `react-hooks/incompatible-library` at `data-table.tsx:349` (TanStack Table's `useReactTable`), is pre-existing and was in the baseline. My baseline, taken before any of this, was `eslint .` → 0 errors, 1 warning.

- [x] V9: No test failure anywhere in the app is attributable to this work. ⚠️ **The full suite is
      NOT green right now, and that is stated rather than hidden:** three other agents were editing
      `twt/`, `vbt/` and `swing/` throughout this session, and the suite's failures move between
      runs as they commit. So this row asserts the two things that are actually mine to assert —
      everything outside their live ground passes, and the new file is never among the failures.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web && O=$(pnpm exec vitest run --exclude 'src/app/\(app\)/{twt,vbt,swing}/**' --exclude 'src/lib/{twt,swing}/**' --exclude 'src/lib/__tests__/no-any.test.ts' --exclude '**/node_modules/**' 2>&1); F=$(pnpm exec vitest run 2>&1 | grep -oE '^ ❯ [^ ]+\.test\.tsx?' | sed 's/^ ❯ //' | sort -u); printf 'clean_ground=%s failed=%s mine_ran=%s mine_failing=%s\n' "$(printf '%s\n' "$O" | grep -oE 'Test Files +[0-9]+ passed' | tr -s ' ' '_')" "$(printf '%s\n' "$O" | grep -cE 'Tests +[0-9]+ failed')" "$(printf '%s\n' "$O" | grep -c 'sentry-client-boundary')" "$(printf '%s\n' "$F" | grep -c 'sentry-client-boundary')"
  EXPECT: /^clean_ground=Test_Files_[0-9]+_passed failed=0 mine_ran=1 mine_failing=0$/m
  EVIDENCE: clean_ground=Test_Files_154_passed failed=0 mine_ran=1 mine_failing=0 — 154 files / 2835 tests pass with the three agents' live trees excluded, the new file among them. The whole-suite failures observed while writing this (varying run to run, 2 to 7 files) were all theirs: `src/app/(app)/{twt,vbt,swing}/__tests__/page.test.tsx` (their in-flight "Scan now" work), `src/lib/swing/__tests__/read-contract.test.ts`, and `src/lib/__tests__/no-any.test.ts` — the last one *caused by* that same new swing file, whose prose comment "wraps its reads in the same `readOrNull`: any non-OK response" trips the `:\s*any\b`-style scan. The baseline taken before any of this work began was 169 files / 3053 tests, all passing.
