# 09a — Screens UI implementation notes

Companion to `docs/08-ui-spec.md` §"Screen editor" / §"Columns editor" and `docs/01` §2, written
while building Prompt 9. Same role as `docs/06a`, `docs/07a` and `docs/08a`.

Implementation: `apps/web/src/app/(app)/screens/`, `apps/web/src/components/screens/`,
`apps/web/src/lib/screens/`, and — for the export — `services/api/src/decile_api/csv_export.py`.

---

## 1. The CSV export is the 93-column reference format, not the screen's columns

Prompt 7 deliverable 6 said the export carries "the screen's column set", and that is what it
shipped. Prompt 9 deliverable 6 says the opposite: "the 93 columns in the order listed in docs/13
§1, UTF-8 **with BOM**, only the `name` field quoted".

`docs/13` settles it, twice. §1 describes the reference product's own export as 93 columns, and §5
step 6 makes reproducing it an acceptance test: "Assert our CSV export reproduces this file's exact
column names, order, quoting and BOM." The reference product exports everything regardless of which
columns the user has chosen to *see*, and the committed fixture is the artefact both prompts have
to agree with. The export was rewritten accordingly.

Four details came from the committed file rather than from the prose, and two of them contradict
what one would assume:

* **Line endings are `\n`**, not `\r\n`, despite the file being Excel-facing. Prompt 7's writer used
  `\r\n`.
* **`name` is quoted on every row**, not "when it contains a comma". `csv.writer` cannot express
  that — `QUOTE_MINIMAL` leaves `CUPID LIMITED` bare and `QUOTE_ALL` quotes everything — so rows are
  assembled directly.
* The BOM is `EF BB BF`; the header row itself is unquoted.
* Numbers are written at their stored precision (`273.00`, `0.5793179400`, `38192`), never through
  `float` (CLAUDE.md house rule 8).

`docs/13` §1's *stated* column order also disagrees with the file — it describes three contiguous
groups of fourteen flags, and the file has 13 + 13 + 13 with the three `etf` columns appended.
`decile_core.reference_export.EXPORT_COLUMNS` already took the order from the file, and the export
follows it. `test_csv_export.py` diffs the header against the file and, on a seeded database,
compares **every value of all 271 rows**.

`open`, `high`, `low` and `volume_shares` come from `ohlcv_daily`, which a database seeded from the
export alone does not have. The join is a `LEFT JOIN` so those four render empty rather than
dropping every row, and the test asserts that they are empty rather than skipping them.

## 2. `build_screen_query` and `build_export_query` share one pipeline

The export and the JSON response must return **the same instruments in the same order** — an export
that disagrees with the table above it is worse than no export. `decile_core.screener` therefore
factors steps 1–6 of docs/06 into `_ranked_pipeline()`, and the two builders are two projections of
it. There is one filter chain, one ranking, one tie-break.

## 3. The Redis cache key had to grow the projection — a bug the browser suite found

`docs/06` §Caching specifies `screen:{sha256(definition_canonical_json)}:{as_of}:{data_version}`.
That is not sufficient, and the failure is not theoretical: it is what the columns editor does
every time.

The cached value is the **response body**, and the body carries `columns` and one member per column
(docs/07 §"Running a screen"). Two requests can share a definition and differ in columns — saving a
column layout changes `screen.columns` and nothing else, and `POST /screens/preview` takes columns
as a separate field. With the documented key, the second request is served the first one's columns:
the save succeeds, the API returns 200, and the table silently shows the old header row.

The end-to-end walk caught it. `cache_key` now hashes the canonical definition **and** the resolved
projection, separated by `\x1f` (a byte that cannot occur in either). The key's shape is unchanged,
and `screen_run.definition_hash` still uses `ScreenDefinition.definition_hash()` — that column
answers "what did the user run", not "what bytes did we send".

## 4. `CUSTOM_FILTER_OPERANDS` was reordered to match docs/01 §2.14

The registry built the volatility operands from `WINDOW_MONTHS`, which ascends (1m → 1y) because the
factor families are built short-to-long. docs/01 §2.14 prints them the other way: "Volatility
1y/9m/6m/3m/1m", and lists the volume averages descending too. The list is a dropdown's contents, so
its order is part of the specification. The registry now reverses that one group.

There is no `/meta/` endpoint publishing this list — docs/07 §Metadata enumerates five and this is
not one of them — so `apps/web/src/lib/screens/operands.ts` carries its own copy.
`packages/core/tests/test_operand_parity.py` reads that TypeScript file and asserts it equals the
registry, in the same way `test_screen_definition_parity.py` keeps the Zod mirror honest. Without
that link the UI could offer an operand the API answers 400 for, and nothing would notice until a
user tried it.

## 5. Signing in during the acceptance suite

`apps/web`'s browser suite runs against a production build (Prompt 8's Lighthouse and frame-timing
criteria are meaningless in dev mode), and `next start` sets `NODE_ENV=production` — which is
exactly the condition that disables the stubbed credential check of `docs/08a` §3. Without an
opt-in, the suite could not sign in at all.

`DECILE_ALLOW_STUB_AUTH=1` was that opt-in, set only in `playwright.config.ts`.

> **Superseded by Prompt 12.** There is no stub any more: `/auth/*` is built, the suite registers
> and signs in for real, and both the variable and `stub-endpoints.ts` are deleted. What the suite
> needs instead is the seeded account's password (`decile_api.seed.E2E_PASSWORD`) and an Argon2id
> cost turned down for speed — both in `playwright.config.ts`. `docs/12a` §5 and §12.

The suite also needs a seeded database and a running API, so `playwright.config.ts` starts both:
`alembic upgrade head && decile_api.seed e2e && uvicorn`, against a database of its own
(`decile_e2e`) so a `pytest` run and a `playwright` run cannot pull the ground out from under each
other. `decile_api.seed e2e` is a new command; it adds the published `pipeline_run` the fixture
lacks and one subscribed account, because the export is entitlement-gated.

**A configuration bug surfaced here too:** the API's `cors_origins` defaults to
`http://localhost:3000`, and the browser calls the API directly (docs/03 §"Request path"), so every
preflight from any other origin fails — with a `400 invalid-screen-definition`, which is a
confusing thing to be told. Nothing is wrong with the default; it just has to be set, and the
config now does.

## 6. URL state is a sparse diff, and the back button pushes

docs/08: "Entire form state is mirrored into the URL via `nuqs` → shareable, back-button-correct."

A full `ScreenDefinition` is about forty fields and seven hundred characters of JSON. Putting all of
it in the query string makes every URL unreadable and most of it noise, because a typical screen
changes four things. `encodeState` writes only what differs **from the screen's saved definition**,
and `decodeState` layers it back on — so a link to an unmodified screen has no parameter at all, and
one to a modified screen carries exactly the modifications. Round-tripping is exact by construction.

Nested groups diff field by field (`{"positive_days":{"m6":55}}`, not the whole five-window object);
arrays are compared whole, because a positional diff of an ordered list is a bug generator and both
arrays here are short.

`history: "push"` is what makes the back button undo a filter change rather than leave the editor.
`throttleMs` is set to the same 400 ms the preview debounces by, so typing does not produce one
history entry per keystroke and the URL and the results settle together. The cost is that a reload
inside that 400 ms window loses the very last change; the browser suite waits for the write, and a
user would have to reload within four-tenths of a second of their last keystroke to notice.

## 7. The results page reads its screen from the query cache, not the server prop

The editor is server-rendered with the screen, and that value is the *initial* one only. Next's
client Router Cache serves the payload it already rendered when you navigate back from the columns
editor, so an editor that read `screen.columns` from its prop showed the old column set after a
successful save — and `router.refresh()` does not reliably fix that from the page you are leaving.

`useScreen(publicId, initial)` seeds a TanStack Query entry from the server render and makes no
request; `useSaveScreen` writes the saved screen straight into it. The editor reads the live copy.

## 8. Reordering columns is keyboard-first

docs/08 §"Columns editor" asks for "drag-to-reorder". HTML5 drag-and-drop is mouse-only — there is
no keyboard event that starts a drag — so a picker built on it alone is unusable for anyone who does
not use a pointer, against docs/08 §"Accessibility & quality bar". Each chosen column therefore has
explicit Move-up / Move-down buttons, with the pointer drag layered on top.

The picker offers **36** columns, not the 34 docs/01 §4 is headed with: that section enumerates
thirty-six, the same arithmetic slip as the 62/64 factor count, and the registry implements every
named key.

`/meta/columns` carries no family, and grouping the picker by the *ranking* families would put a
third of it under "Other" — thirteen of the thirty-six are not ranking factors at all. The groups in
`src/lib/screens/column-families.ts` follow docs/01 §4's own enumeration order instead.

## 9. `ignore_top_beta.count` still has no control

docs/01 §2.10 gives each risk switch "a count/percentile input". `docs/06a` §5 records why the count
cannot be honoured: docs/06 settles the semantics as a boolean flag precomputed per universe at
`TOP_RISK_FLAG_PERCENTILE`, and a per-request count cannot be served by testing a bit. The form
therefore renders the switch and no count field — offering an input that changes nothing would be
worse than omitting it. The switch's hint says what the cut actually is.

## 10. What Prompt 9 did not build

`/screens/new` appears in docs/08 §Routes; the "New screen" button creates a screen through
`POST /screens` and navigates to its id instead, so there is no unsaved-screen state to reconcile.

The peek drawer shows what the row already carries rather than fetching the factsheet — the point of
a peek is that it is instant, and `/instruments/{symbol}` is Prompt 10's. Its "open the full
factsheet" link is a plain anchor for the same reason: `typedRoutes` would reject a `<Link>` to a
route that does not exist yet, correctly.
