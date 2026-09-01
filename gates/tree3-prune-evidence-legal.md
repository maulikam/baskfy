# Gates: Prune redirect stubs · evidence the two gate boxes · counsel package

Scope: three items from the UI audit of 25 Aug 2026. Filed under `gates/` and not `GATES.md`
because `GATES.md` holds another tree's plan (a `/home` dashboard). Run the checker with this
file named explicitly:

```
node ~/.claude/skills/unlazy/scripts/gate-check.mjs gates/tree3-prune-evidence-legal.md
```

## Measured before these gates were written — three things change the shape of the task

**1. It is 14 stubs, not 17.** `next.config.ts` redirects fire *before* the filesystem routes, so
a page at a redirected path is unreachable. Verified against the running server: `/screens`,
`/dashboard`, `/portfolios`, `/watchlist`, `/explore`, `/collections`, `/backtests`,
`/investments`, `/listings`, `/market-health` all return **308** and never reach their stub —
**including on `RSC: 1` + `Next-Router-Prefetch: 1`**, which is the request the App Router client
makes on a soft navigation. `next.config.ts` sets no `output:` mode, so `redirects()` always runs.
That empirically retires the "second line of defence for a client-side navigation" rationale in
`scripts/check-shadowed-routes.mjs`.

**But three of the seventeen are load-bearing and must NOT be deleted:** `/instruments`,
`/market` and `/me` have **no** `next.config` redirect. Their `page.tsx` `redirect()` call *is*
the mechanism — each 307s to login, then the page component sends the user on. Deleting them
returns 404 on three real entry points. So the ask "prune the 17" over-reaches by three; scoped
to 14, recorded here, per the autonomy charter's precedence (a criterion is a proxy for its Goal).

**2. The two "pending evidence" gate boxes are a formatting artifact, not missing work.** Both
files write `EVIDENCE:` with the text starting on the *next* line; `gate-check.mjs`'s
`ATTR_RE` captures only what follows the colon on the same line, so it reads as empty. The
evidence is present and substantial in both. This gate therefore **verifies the claims are true**
and then makes the ledger able to parse them — it does not manufacture evidence.

**3. Getting a lawyer is not something an agent can do.** See G9 — this is scoped to the work
that genuinely precedes it and is abandoned honestly for the part that cannot be done here.

Working root for every CHECK: `/Users/maulikdave/Documents/projects/baskfy`.
Web CHECKs run from `decile-blueprint/apps/web`.

---

## A — prune the dead stubs

- [x] G1: Exactly the 14 unreachable stubs are deleted, and `/instruments`, `/market` and `/me`
      are kept. Stated as a count, not "some".
  CHECK: bash tools/tree3b/stub-inventory.sh
  EXPECT: /INVENTORY dead=0 loadbearing=3/
  EVIDENCE: /me | INVENTORY dead=0 loadbearing=3
    collection/[slug], collections, dashboard, explore, investments, listings, market-health,
    portfolios, screens, screens/[id], screens/[id]/columns, watchlist. Kept: /instruments,
    /market, /me.
    The inventory is computed from next.config.ts and the filesystem, never from a typed list —
    and that mattered twice. A first version using `sed` mangled `:id` and misclassified 4
    dynamic routes; a second read `source:` from the whole config and swept in `headers()`'s
    catch-all `{ source: "/:path*" }` at line 124, marking all 17 dead — which would have
    deleted the three load-bearing files. The parse is now scoped to the redirects() block.

- [x] G2: Every deleted path still redirects to the same destination it did before, proven
      against the running server — a 404 or a changed destination is a regression.
  CHECK: bash tools/tree3b/redirect-parity.sh
  EXPECT: /PARITY OK/
  EVIDENCE: /me                      307 -> /login?next=%2Fme | PARITY OK — 18 path(s) redirect exactly as before the prune
    `tools/tree3b/redirects-expected.txt` was captured from the running server *before* any
    file was deleted, and covers dynamic paths too (`/screens/123/columns` -> `/build/123/columns`,
    `/investments/7/orders` -> `/me/investments/7/orders`). This is the only check that could
    catch a regression here: the deleted files were unreachable, so no existing test touched them.

- [x] G3: The three kept stubs still work, including the RSC/prefetch path.
  CHECK: bash tools/tree3b/loadbearing.sh
  EXPECT: /LOADBEARING OK/
  EVIDENCE: /me            307 | LOADBEARING OK — 3 kept stubs resolve on both request paths
    `RSC: 1` + `Next-Router-Prefetch: 1` both give 307 to `/login?next=<original path>` — the
    path being preserved is the proof the route resolved rather than 404ing.
    **A second class of keep-me file was confirmed untouched**: `/basket/[slug]/constituents`
    is an SC5 stub holding the route open until SC3 publishes immutable constituent versions.
    It renders real content at a non-redirected path, so it was never a prune candidate, and
    `check-shadowed-routes.mjs` ignores it. The script now says so in as many words —
    "unreachable" is not "unfinished", and this check must never be read as licence to delete a
    thin-but-reachable page.

- [x] G4: `check-shadowed-routes.mjs` is updated so the invariant it enforces matches what is
      now true — a redirected path holding *any* page file is dead code, and the script's own
      comment must stop asserting a "second line of defence" that the measurement disproves.
      It must still fail loudly if someone puts a real page back at a redirected path.
  CHECK: cd decile-blueprint/apps/web && node scripts/check-shadowed-routes.mjs
  EXPECT: /ok —/
  EVIDENCE: ok — 17 redirected source(s), no page file shadowed by any of them
    Rewritten, and the rewrite is what found the real bug. The old script did
    `if (source.includes(":path*")) return null` — it skipped wildcard sources entirely, so the
    three REAL pages under `/investments/:path*` were invisible to it. It now walks the tree
    beneath a wildcard source. Both regression shapes were proven to fail it, by reintroducing
    them: a stub at `/screens` -> exit 1; a real page at `/investments/[id]` -> exit 1
    (`/investments/:path* -> src/app/(app)/investments/[id]/page.tsx`), then exit 0 once removed.
    The rule changed from "nothing but a redirect at a redirected path" to "no page file at all",
    and the docstring's "second line of defence" claim is replaced with the measurement that
    disproves it.

- [x] G5: The deletion is provably safe against a rebuild, not just against a warm dev server:
      `tsc --noEmit` and the shadow check pass, every file this tree owns is eslint-clean, and
      the repo-wide eslint count is **no worse than the baseline**.
      *Criterion scoped:* "lint is clean" cannot hold — `pnpm run lint` fails on 22 eslint errors
      in components this tree never opened. Measured both ways by stashing the work: **22 errors
      + 1 warning before, 22 + 1 after**. Fixing 22 unrelated errors is not this ask; reporting
      them is. (Charter precedence: a criterion is a proxy for its Goal.)
  CHECK: bash tools/tree3b/lint-delta.sh
  EXPECT: /LINT DELTA OK/
  EVIDENCE: repo-wide eslint errors: 21 (baseline before this tree: 22, all pre-existing) | LINT DELTA OK — introduced 0 new errors
    passes; every file this tree owns is eslint-clean; repo-wide **21 errors against a 22
    baseline** — one fewer, not one more. Baseline measured by `git stash`-ing this tree's work
    and re-running, so it is a measurement rather than an assumption.
    Getting tsc clean required clearing three stale gitignored build artifacts
    (`.next-gate` 20:21, `.next-e2e` 21:14, `.next`) whose generated `types/validator.ts` still
    imported the deleted pages. `tsconfig.json` includes all four `.next*/types/**` globs, so a
    stale artifact fails the typecheck with errors that look like source errors and are not.
    **Independently confirmed by a production build** — `BASKFY_WEB_DIST_DIR=.next-build
    next build`, exit 0, "Compiled successfully" — because a warm dev server proves nothing
    about a rebuild.
    *The env var is not optional and I learned that the hard way:* the first run was a bare
    `next build`, which writes into `.next` — **the directory the running dev server owns**. That
    left `.next` a mixed tree (production `BUILD_ID`, `export-marker.json` and hashed
    `main-app-<hash>.js` beside dev's unhashed `webpack.js`), so `/login` served 200 while
    `main-app.js` and `polyfills.js` 404'd as `text/plain`, React never hydrated, and every
    control on the page looked disabled. `next.config.ts:65` reads `BASKFY_WEB_DIST_DIR`, and
    `tsconfig.json` already lists `.next-build`, `.next-e2e`, `.next-gate` and `.next-tree4` —
    the convention existed and the bare command ignored it. Re-run correctly, the build passes
    and the dev server keeps serving. Its route table
    lists none of the 14 pruned paths, lists `/instruments`, `/market` and `/me` at 174 B each
    (what a redirect-only page compiles to), and still lists `/portfolios/[id]/{brokers,
    rebalance,sleeves}` — correctly, since only the exact `/portfolios` is redirected, not
    `/portfolios/:path*`.

- [x] G6: The page count is stated exactly, before and after.
  EVIDENCE: **85 -> 68 page files** (`find src/app -name page.tsx | wc -l`), which lands exactly
    on the "~68 real pages" the audit estimated. 17 files removed, not 14: the 14 dead stubs plus
    **3 real pages** at `/investments/[id]`, `/investments/[id]/orders` and
    `/investments/[id]/customize`, shadowed by `/investments/:path*` and therefore unreachable —
    verified live (`/investments/7/orders` -> 308 -> `/me/investments/7/orders`).
    Deleting them loses nothing: diffed against their live `/me/investments` counterparts, the
    deleted copies were strictly **older** — missing the `DriftRepair` and `SipForm` imports and
    still carrying pre-polish jargon ("desk plan" where the live copy says "order plan").
    This also corrects the audit's claim of "two live copies of the investments tree": there was
    one live copy and three files nobody could reach.

## B — the two gate boxes

- [x] G7: Both claims are **verified true in the current source** before either box is ticked:
      the duplicate `default-src` is gone from the CSP builder, and the second defect the sweep
      named is fixed. Verified by running the tests those gates cite.
  CHECK: bash tools/tree3b/verify-sweep-claims.sh
  EXPECT: /SWEEP CLAIMS VERIFIED/
  EVIDENCE: 1 passed, 3506 deselected in 0.90s | SWEEP CLAIMS VERIFIED — both defects are fixed and the tests that prove it pass today
    taken on trust:
    (a) `src/lib/__tests__/csp.test.ts` exists, **10 passed**; `middleware.ts` emits exactly
        **1** real `default-src` directive (line 119). Lines 27 and 167-169 also contain the
        string, in a docstring and in the comment explaining why it is not repeated — counting
        raw matches would have "proved" 4 and proved nothing, so the check counts quoted
        directive lines.
    (b) `dict.fromkeys` present in `backtests.py` (2 uses);
        `test_assumptions_state_each_note_once` **1 passed**.

- [x] G8: Both gate files parse as met — `EVIDENCE:` carries a summary on its own line, with the
      detail kept beneath it. Neither file loses a word of the original evidence.
  CHECK: node ~/.claude/skills/unlazy/scripts/gate-check.mjs --status gates/tree3-ui-fix.md gates/archive-tree3-ui-issues.md 2>&1 | tail -3
  EXPECT: /ALL MET/
  EVIDENCE: gates/archive-tree3-ui-issues.md: 13 gates | ALL MET (27 met)
    The fix was one line per file: a summary on the `EVIDENCE:` line itself, because
    `gate-check.mjs`'s `ATTR_RE` reads only what follows the colon on the same line and both
    files started their evidence on the next line. **Not one word of the original evidence was
    removed** — it sits below the summary exactly as written. The claims were verified first
    (G7); ticking a box because it merely *looked* documented is the failure this discipline
    exists to prevent.

## C — the legal drafts

- [x] G9: The part an agent can actually do is done: a single counsel-ready brief that names
      every document, what it claims, the specific questions counsel must answer, and the
      jurisdiction — so the review can start the moment a lawyer is engaged. Plus proof the
      drafts cannot be mistaken for reviewed text in the product today.
  CHECK: bash tools/tree3b/legal-readiness.sh
  EXPECT: /LEGAL PACKAGE READY/
  EVIDENCE: /refund-policy      [GRIEVANCE OFFICER EMAIL] [GRIEVANCE OFFICER NAME] | LEGAL PACKAGE READY — brief written, guard passing, blocker filed
    be forwarded to a lawyer as-is: the four documents and their sizes, the eight questions from
    `DRAFT-NOTICE.md`, the C1-C3 regulatory questions from `NEEDS-MAULIK.md` §13, D7/D10, a
    checklist of the seven business facts, and what the code already enforces so counsel knows
    what is *not* at risk. They previously lived in three files.
    Guard re-run: `legal-drafts.test.ts` **22 passed**; every `.mdx` carries the in-repo DRAFT
    marker and none of it reaches the rendered page.
    **The finding that makes this urgent, which the audit had not caught:** all four pages return
    **200 to the public** and render literal `[SUPPLIER LEGAL NAME]`, `[SUPPLIER GSTIN]`,
    `[GRIEVANCE OFFICER NAME]` and more. Not a rendering bug — the values are unknown to the repo.

- [x] G10: The blocker is filed where Maulik will see it, stated as blocking, and not softened.
  EVIDENCE: `NEEDS-MAULIK.md` §19, plus a row in the summary table at the top of the file. It
    leads with what a visitor sees today, carries the per-page placeholder table, lists the seven
    facts as an unchecked checklist, and points at `docs/COUNSEL-BRIEF.md`. It ends by saying
    plainly that **an agent cannot engage a lawyer** and that nothing filed here substitutes for
    that step — the ask is not softened into something that sounds done.

ABANDON: G9(partial) engaging a lawyer is outside anything an agent can do. Everything that precedes it — the sendable brief, the guard proving the drafts cannot pass as reviewed, the filed blocker and the fact checklist — is delivered; the engagement itself is Maulik's and is stated as such.

## D — honest record

- [x] G11: Nothing outside these three items changed; the desk tree and `frozen/` are untouched.
  CHECK: test "$(git status --short kite-momentum-rebalancer/ frozen/ | wc -l | tr -d ' ')" = 0 && echo "DESK AND FROZEN UNTOUCHED"
  EXPECT: /DESK AND FROZEN UNTOUCHED/
  EVIDENCE: DESK AND FROZEN UNTOUCHED
    this tree touched is under `decile-blueprint/apps/web`, `tools/tree3b/`, `docs/` and
    `NEEDS-MAULIK.md`.

- [x] G12: Every number in the report is re-measured at report time. The ledger is pasted with
      its N-of-N count.
  EVIDENCE: Re-measured immediately before reporting, not recalled:
      page files                68  (was 85)
      files deleted             17  (14 dead stubs + 3 shadowed real pages)
      stub inventory            dead=0 loadbearing=3
      redirect parity           18 paths, all unchanged
      eslint                    21 errors vs a 22 baseline — 0 introduced
      tsc --noEmit              clean
      shadowed-routes check     17 sources, 0 shadowed page files
      the two gate files        ALL MET (27 met), previously 1 unmet in each
      counsel brief             1,192 words
      desk tree / frozen        0 files touched
    One number in the original ask was wrong and is corrected rather than repeated: it is not
    "17 stubs" — it is 14 stubs plus 3 real pages, and 3 *other* stubs must never be deleted.

<!--
A checked box with EVIDENCE still "pending" counts as UNMET.
If a gate becomes impossible: add `ABANDON: G<n> <reason>` and say so in the report.
-->
