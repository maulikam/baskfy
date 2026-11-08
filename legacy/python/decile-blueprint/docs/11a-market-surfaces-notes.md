# 11a — Dashboard, Market Health and Listings implementation notes

Companion to `docs/01-product-teardown.md` §6 and §7, `docs/08-ui-spec.md` §Dashboard and
§"Market Health", and `docs/07-api-spec.md` §"Market data surfaces", written while building
Prompt 11. Same role as `docs/06a`, `docs/07a`, `docs/08a`, `docs/09a` and `docs/10a`.

Implementation: `services/api/src/decile_api/market_data.py` and `routers/market_data.py`,
`packages/core/src/decile_core/breadth.py`, `apps/web/src/app/(app)/{dashboard,market-health,listings}/`,
`apps/web/src/components/market/`, `apps/web/src/lib/market/`.

---

## 1. The fixture carries 117 indices, not docs/01 §7's ~145

docs/01 §7 says "~145 index rows" and names exactly two of them — `Nifty50 PR 1x Inverse` and
`India VIX`. `decile_worker.tasks.snapshots` already settled the production question: index rows
are registered from *what NSE publishes*, because "hardcoding a list of 145 index names would be
inventing data — and it would go stale the first time NSE adds an index".

The fixture is the part that had to change, because a dashboard with 14 rows does not exercise a
dashboard. `decile_providers.fixture_builder` now emits the 14 selectable universes plus **103
real NSE index names** across the broad-market, sectoral, thematic, strategy, derived and
fixed-income families — 117 in total, each with 30 trading days of seeded levels.

It stops at 117 deliberately. The remaining ~28 are names we would be guessing at, and a fixture
padded with invented index names is worse than a shorter one: it would teach every reader of the
dashboard a set of indices that does not exist. The count is asserted, so a fixture regression is
a failing test rather than a quiet shrink.

**The consequence for Prompt 11's second acceptance criterion is stated in the report**: the
behaviour — every row rendered, virtualised, no jank, sorting with zero requests — is verified at
117 rows rather than 145. The neighbouring `table-performance.spec.ts` holds the same component to
the same frame budget at 4,000 rows, which is the stronger statement about scale.

The values against those names are synthetic; `tests/fixtures/providers/PROVENANCE.md` says so,
and the name list has not been checked against a live NSE index file — the same standing caveat
CLAUDE.md carries for every NSE file shape.

## 2. There is no sector grouping, because there is no sector

docs/08 §Dashboard asks for "search box, **sector grouping**, and a sparkline per index". Search,
the card/table toggle, the sort and the sparkline are all delivered. Grouping is not, and it is
not an oversight: **`docs/04` has no sector column** — not on `index_def`, not on `instrument`,
not anywhere. NSE's index names encode a sector for some of them (`NIFTY BANK`, `NIFTY IT`) and
not for others (`NIFTY100 QUALITY 30`, `INDIA VIX`), and deriving a taxonomy by pattern-matching
names would be inventing a classification and then presenting it as the exchange's.

What that needs is either a sector column populated from a source `docs/09` does not currently
list, or an explicit family taxonomy in `docs/04`. Either is a data-model decision, not a UI one.

## 3. "Data available from" is read, never written as a constant

docs/01 §6 shows "Data available from 1st Nov 2024" — the reference product's own floor, which
docs/01 §10 lists as one of its gaps and ours to fix ("Store point-in-time index membership +
factors from day 1 of backfill").

So the note is `min(market_health_daily.date)` for the selected universe. On the seeded dataset it
reads 18 Aug 2026, because that is genuinely the only date breadth exists for. Hard-coding
1 Nov 2024 would have made the page state something false from the first day the backfill ran
further back, and false in the opposite direction today.

## 4. The history overlay is the selected universe's own index level

docs/08 §"Market Health" asks for "a history chart of each breadth series with **the Nifty**
overlaid". Taken literally that is NIFTY 50 in every chart, including the one for NIFTY MICROCAP
250 — where the large-cap index is the wrong denominator and the comparison would be actively
misleading.

Every universe in the selector is itself an index with an `index_snapshot_daily` row, so the
overlay is that universe's own level, and the chart legend names it. Same reasoning as `docs/10a`
§4's choice of percentile universe: the comparison set is stated rather than assumed.

Two y-scales, because breadth is bounded at 0–100 and an index level is not. One axis would flatten
the breadth line onto the baseline.

## 5. `/listings` carries no `as_of` or `data_version`

docs/07 §Conventions: "Every **analytics** response includes `as_of` and `data_version`". The
listings register is not an analytics response — it is a list of what is listed, and there is no
trading day on which its rows would have been different values. It carries §Conventions' other
shape instead, `{ data, next_cursor }`.

The three market-data responses do carry both.

`?search=` is an addition. docs/07 gives `/listings` `from`, `to`, `series` and `cursor`; Prompt 11
deliverable 4 asks for "a series filter **and search**", so search is there and matches symbol or
name. Changing either filter clears the cursor — a keyset cursor is a position in one ordered
result set, and carrying it into a differently-filtered set would resume from a row no longer in
it.

## 6. "Revalidate on `data_version`" happens in the data cache, not the route cache

docs/08 §Routes marks `/dashboard` "RSC, revalidate on `data_version`" and `/instruments/[symbol]`
"ISR". Both render **dynamically**, and will keep doing so: every route under `(app)` sits below a
layout that calls `auth()` and `cookies()`, which opts the whole segment out of static rendering.

What is cached is the *fetch*. Every server-side read tags itself `factsheet` with a one-hour
`revalidate`, so the API response is served from Next's Data Cache and `revalidateTag("factsheet")`
— from `POST /api/revalidate` — invalidates every market surface and every factsheet at once. The
page still re-renders per request; it does not re-query the API.

`docs/10a` §11 described the mechanism correctly but implied the *route* was statically cached. It
is not, and the reason is the authenticated shell rather than anything about these pages. Nothing
calls `/api/revalidate` yet — wiring the nightly publish step to it is still open.

## 7. The breadth arithmetic moved into `decile_core`

`decile_core.breadth.breadth_query` builds the statement; `decile_worker.tasks.market_health`
executes and stores it, and `decile_api.seed` executes it to populate a development database. The
same split as `decile_core.screener` versus `decile_api.screener`, and here it is load-bearing:
Prompt 11's first acceptance criterion checks that arithmetic against a hand computation, and a
seed carrying a second copy would have made the check meaningless.

`decile_worker.tasks.market_health.market_health_history` was deleted rather than left beside
`decile_api.market_data.market_health_history`. Two readers of one table, one of which nothing
called, is a drift waiting to happen; the API's is the one with the overlay join.

## 8. Two properties of the seeded dataset worth knowing before reading the gauges

* **`1Y Return > 0%` is 100% for every universe.** `docs/13`'s export is the *output of a momentum
  screen*, so every row in it has a positive one-year return by construction. On real market data
  this gauge would not be 100%, and a test asserts the 100% so nobody reads it as a bug.
* **Breadth exists for exactly one date.** All four metrics read factor-engine columns, and
  `factor_daily` is seeded from that same single-date export. So the history charts render "only 1
  day of history so far — a line needs two" rather than drawing a line through one point. Populating
  a range needs the pipeline run over a real backfill, which is `make backfill` plus `make pipeline`,
  not a seeding change.

## 9. The sitemap now enumerates instruments

`docs/10a` §6 recorded that it could not: enumerating instrument URLs needs an endpoint that
enumerates instruments, and `/instruments?search=` requires a search term. `/listings` is that
endpoint, so `apps/web/src/app/sitemap.ts` walks it and emits one URL per symbol, plus the three
new routes. It is bounded at 60 pages of 100 and degrades to the static routes if the API is
unreachable — a short sitemap costs discovery, a failed build costs the deploy.

## 10. The dashboard is sorted server-side once and re-sorted client-side thereafter

docs/01 §7's "Sorted by % change descending" is the API's `ORDER BY`, so the first paint is already
right. Every subsequent sort, the search and the view toggle are in-memory over the array the
server sent — Prompt 11's second acceptance criterion, asserted by counting network requests during
a sort and finding zero.

That is also why the 30-day sparkline travels *inside* the dashboard payload rather than from a
per-index endpoint: 117 sparklines is 117 requests otherwise, and the whole series is one number
per index per day.

NULLs sort last in both directions. "No P/E" is not "the lowest P/E", and an index with no
fundamentals should not lead the ascending sort.
