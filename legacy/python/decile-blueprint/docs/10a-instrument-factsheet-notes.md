# 10a — Instrument factsheet implementation notes

Companion to `docs/01-product-teardown.md` §5, `docs/05-factor-formulas.md` §16 and
`docs/07-api-spec.md` §Instruments, written while building Prompt 10. Same role as `docs/06a`,
`docs/07a`, `docs/08a` and `docs/09a`: what the documents did not settle, and what was decided.

Implementation: `services/api/src/decile_api/instruments.py` and `routers/instruments.py`,
`packages/core/src/decile_core/pros_cons.py`, `apps/web/src/components/instrument/`,
`apps/web/src/lib/instrument/`, `apps/web/src/app/(app)/instruments/[symbol]/`.

---

## 1. Eight PROS fire for CUPID; the reference product showed six

`docs/01` §5 block 3 records the reference product rendering six lines — the four moving-average
lines, "within 25% of all time high", and "the beta is less than 1.25". `docs/05` §16 tables
**eight** rules and says "Ship **at least**" them, adding positive-days and median-turnover.

Every one of the eight is satisfied by the committed export row for CUPID, so our page renders
eight where the reference showed six. Both tests assert the same thing: the six observed lines are
the **first six**, in `docs/05` §16's order, word for word
(`services/api/tests/test_api_instruments.py::TestProsAndCons`,
`apps/web/src/components/instrument/__tests__/factsheet.test.tsx`). The two extra lines are a
superset the specification explicitly invites, not a deviation.

We cannot tell from `docs/01` whether the reference product lacks those two rules or whether its
snapshot simply failed them. Either way §16 is the instruction we follow.

## 2. A rule with a NULL input is neither a PRO nor a CON

`docs/05` §16 says "CONS are the negations". Read literally, an instrument with four months of
history — no `beta_12m`, no `pos_days_12m`, no `ma_200` — would collect a full set of CONS for
facts nobody knows. That is the same mistake `docs/06` §"Step 4" forbids for filters ("NULLs never
satisfy a predicate"), pointed at the reader instead of the query.

So `evaluate()` skips a rule whose inputs are NULL, `undecided()` returns those keys, and the page
says how many rules it could not decide. The list is shorter and it is honest.

## 3. The two Wasserstein distances are recomputed, not stored

`docs/05` §15: "Expose the two distances in the API so the label is explainable rather than magic."
`docs/04`'s DDL has a `factor_daily.regime` column and no columns for the distances. Rather than
add two columns Prompt 4 did not specify, `build_factsheet` recomputes them from the same
`ohlcv_daily` history the nightly job classifies from, via `decile_core.regime.classify_series`.

Consequence worth knowing: the stored label wins when it is populated, and the recomputed label
stands in when it is not. On a database seeded only from the reference export — which has no
`regime` column — the label on the page is the recomputed one. Both come from the same function,
so they agree; they are not independently derived numbers that could drift.

## 4. "Current universe" for the percentile bars means the narrowest one

`docs/08` §"Instrument factsheet" asks for "that value's percentile within the current universe"
and never defines "current". A stock is in several at once — CUPID is in NIFTY TOTAL MARKET and
NIFTY MICROCAP 250 on the export's date.

`primary_universe()` picks the **narrowest** index the instrument belongs to on the as-of date,
measured by membership count that day rather than by a hard-coded ranking (index sizes move; a
list here would go stale silently). `etf` and `nifty-fno` are skipped — they are classifications,
not size bands. The chosen universe travels in the payload as `percentile_universe` and is named
on the page, so the reader is never left to guess what a bar is relative to.

A NULL value gets no percentile. An unknown number is not "the worst"; the bar is simply not drawn.

## 5. Medians come from whichever table holds that metric's history

`docs/01` §5 block 4: "The medians are the stock's own historical medians." Closing price has years
of history in `ohlcv_daily`; the four factor metrics have theirs in `factor_daily`. Each median is
taken over the instrument's own rows in the table that holds it, with `percentile_cont(0.5)`.

Each card also carries `observations` — the count the median was actually taken over — and the
card's tooltip states it. A median of one row and a median of three years look identical otherwise,
and on a database with a single published factor day most of them are the former.

## 6. `/listings` is Prompt 11, so the sitemap does not list instruments yet

`docs/07` §Instruments has six entries; five are implemented here and `/listings` is not — it
belongs with the listings register (Prompt 11). That is also why `apps/web/src/app/sitemap.ts`
still lists only the root: enumerating every instrument URL needs an endpoint that enumerates
instruments, and `/instruments?search=` requires a search term. The pages are crawlable meanwhile —
`robots.ts` allows `/instruments`, and every results table links to them.

## 7. Responses are serialised with `canonical_json`, not by Pydantic

The factsheet endpoints return a pre-encoded body, the same way `/screens/{id}/run` does. Pydantic's
JSON mode renders `Decimal` through `float`, which turns a stored `13.00` into `13.0` — the
disagreement CLAUDE.md house rule 8 exists to prevent. `decile_core.screener.canonical_json` emits
the stored digits verbatim.

The response models are still declared, still validated, and still in `openapi.json`; only the
encoder changed. `CellValue` is `Decimal | date | int | str | None` rather than Pydantic's
`JsonValue` for the same reason — `JsonValue` has no `Decimal` member — and admits a string and a
date because `docs/01` §5 block 2 puts Series and Listed On in the same list as P/E and Beta.

## 8. `away_high` reconciles with `docs/05` §10 against each source's own inputs

`docs/05` §10 verifies CUPID at **−4.83%**, from a close of 284.56 and a 1-year high of 299.00.
The committed export is a different snapshot of the same instrument: close **284.03**, same high,
so `284.03 / 299.00 − 1 = −5.01%`, which is what the export stores and what we serve.

The test asserts both — `docs/05`'s arithmetic from `docs/05`'s numbers, and the served value from
the export's numbers. Asserting only one would check a snapshot rather than a formula.

## 9. `factor_daily.pe` — was an em dash everywhere, now a number (Tree 3)

`docs/01` §5 puts P/E in block 2 and a "Price to Earnings" metric card in block 4, so both are
rendered. Both used to show `—` for every instrument, because nothing had ever written a row to
`fundamental_daily`.

The cause turned out not to be a missing source. T9.1 had written the parser and the join against
NSE's `/api/quote-equity`, and NSE had **retired that route** — it answers 403 from the Akamai
edge, which reads like a bot block and is really a removed endpoint. The quote page calls
`GetQuoteApi` now. With the provider pointed at the live route and the date's fundamentals filled,
the factsheet serves real values: `GET /api/v1/instruments/BHARTIARTL` returns
`marketcap_cr = 1207038` and `pe = 32.942` for `as_of = 2026-08-18`.

Three things about it are still worth knowing:

* **An em dash on a single name is now meaningful.** NSE publishes no P/E for a company without
  earnings, and BZ-series names generally have none. The card is not removed — a missing ratio is
  a data gap, not a specification change — but it no longer means "we never fetched anything".
* **`pb` and `div_yield` will not follow.** The current payload does not carry them at all. Their
  columns stay because the schema documents them, not because anything fills them.
* **The stored P/E is re-priced onto the as-of date**, not the ratio NSE quoted on the day of the
  fetch — the quote carries no history, and storing it verbatim into a past date would be
  look-ahead (house rule 5, `docs/05` §14).

## 10. Two accessibility fixes the factsheet forced on the shell

The instrument page is the first one long enough to scroll, which surfaced two latent issues in
`AppShell`:

* `<main>` is the scroll container and carried `tabIndex={-1}`. axe's `scrollable-region-focusable`
  (WCAG 2.1.1) requires a scrollable region to be reachable by **Tab**, not merely focusable
  programmatically. It is now `tabIndex={0}` and shows the global `:focus-visible` ring.
  `jsx-a11y/no-noninteractive-tabindex` disagrees with axe here; the eslint config names the
  exception rather than suppressing it at the call site.
* The announcement banner's action read "Read more", which Lighthouse's `link-text` audit counts as
  non-descriptive. It cost 8 points of SEO on this route and now reads "Read the December 2026
  update". Score after the change: **100**.

The `<Disclaimer/>` is the shell's, not the factsheet's. Rendering a second copy of the same
regulatory sentence on one screen reads as a bug; CLAUDE.md house rule 9 is satisfied by the frame,
which no page can ship without.

## 11. ISR is invalidated by a tag, not only by a timer

`docs/08` §Routes marks this route ISR and Prompt 10 asks for it to be "revalidated on
`data_version`". Every factsheet request carries the `factsheet` cache tag and a one-hour
`revalidate` backstop; `POST /api/revalidate`, guarded by a constant-time comparison against
`REVALIDATE_SECRET`, calls `revalidateTag("factsheet")`.

**Nothing calls it yet.** Wiring the nightly publish step (`docs/09` §Schedule step 10) to make
that request is not in Prompt 10's deliverables and has not been done; until it is, the pages
refresh on the timer. Without `REVALIDATE_SECRET` set the route answers 503 rather than defaulting
to open.

> **Correction, from Prompt 11.** What the tag revalidates is Next's *data cache*, not a statically
> rendered route. Every page under `(app)` renders dynamically, because the shell's layout calls
> `auth()` and `cookies()`. The API response is still cached and still invalidated by the tag; the
> HTML is re-rendered per request. `docs/11a` §6.

## 12. `/rank-history` reads the audit trail, it does not recompute

`docs/07`: "this stock's rank over time in a screen". The series is read from `screen_run.results`,
which `docs/04` defines as the record of what a screen returned on each date. It is deliberately
not recomputed: the point of a rank history is what the screen said at the time, and re-running
today's engine over past data answers a different question.

So the series is exactly as long as the screen's run history — one point for a screen run once. The
response says so rather than padding it.
