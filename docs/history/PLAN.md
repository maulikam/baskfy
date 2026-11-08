# PLAN — Portfolio Redesign (PORTFOLIO_REDESIGN.md v1)

Decomposition of the approved spec. Written before any code, so what is *not* being built in a
given session is recorded rather than forgotten.

## The honest size of this

The spec is three phases (§10) touching schema, a nightly worker job, broker sync, the API, and a
whole new navigation section with two new pages and two new flows. That is not one sitting. The
failure mode this plan exists to prevent is a half-built Overview page over a ledger that does not
balance — which is exactly the order the spec forbids: *"Build the ledger first — the page is thin
once it exists."*

## Survey — what exists today (measured 26 Aug 2026, not assumed)

| Spec needs | State today |
|---|---|
| Allocation ledger | **Absent.** No module in `packages/core`. |
| Whole-holding, one capital portfolio (§4.2) | **Structurally violated.** `portfolio_holding` PK is `(portfolio_id, instrument_id, broker_account_id)` — the same holding in two portfolios is a legal row. |
| Portfolio kind (capital vs monitoring, §4.1) | **Absent.** No `kind` column. |
| Portfolio source badge (§3) | **Absent.** No `source` column. |
| Unallocated bucket + cash ledger (§4.4) | **Absent.** |
| EOD NAV series (§5.1) | **Absent.** |
| Reconciliation inbox (§4.3) | **Absent.** |
| Broker holdings sync | **Absent** in the worker. Blocked beyond Kite anyway — `NEEDS-MAULIK.md` §16, nine brokers with no credentials. |
| XIRR | **Exists and is tested** — `baskfy_core.curated_accounting.xirr`. Reuse, do not reimplement. |
| Nesting (`parent_id`) | Exists. §8 says drop the concept from the v1 UI. |
| §8 jargon in UI strings | Present: Box 14, Book 17, Sleeve 21, Divide 9, File under 2, Run by hand 1, Nest 1. "Spans brokers" and "Your rule" already 0. |

## The tree

```
1  Portfolio redesign
├─ 1.1  The spine (§10 Phase 1)
│  ├─ 1.1.1  Allocation-ledger domain, pure          ✅ done
│  ├─ 1.1.2  Schema: kind, source, one-capital-portfolio constraint  ✅ done
│  ├─ 1.1.3  Cash ledger + Unallocated bucket
│  ├─ 1.1.4  Nightly EOD NAV job + since-grouped marks
│  ├─ 1.1.5  Reconciliation inbox (service + API)
│  └─ 1.1.6  Broker holdings sync (Kite first)
├─ 1.2  The page (§10 Phase 2)
│  ├─ 1.2.1  Nav restructure: Portfolio → Overview | Portfolios | Holdings | Activity | Watchlist
│  ├─ 1.2.2  Overview: header, hero metrics, combined chart
│  ├─ 1.2.3  Portfolio table + inspector drawer
│  ├─ 1.2.4  Unallocated-first onboarding + grouping flow
│  ├─ 1.2.5  Needs-attention ribbon
│  ├─ 1.2.6  Detail page (§7)
│  └─ 1.2.7  Language renames (§8) + boilerplate removal
└─ 1.3  Depth (§10 Phase 3) — CAS import, true XIRR for holding groups, partial allocation,
        monitoring suggestions, intraday, contribution analysis
```

## Contract — shared surfaces, fixed before anything is built on them

Written here so 1.1.2 through 1.2.7 cannot each invent their own vocabulary.

- **Names come from §3 and nowhere else.** `PortfolioKind` ∈ {`CAPITAL`, `MONITORING`}.
  `PortfolioSource` ∈ {`SUBSCRIBED`, `MY_SCREEN`, `MY_STRATEGY`, `HOLDING_GROUP`}.
- **Money is `Decimal`, never `float`** (house rule 9). Quantities are `Decimal` too — corporate
  actions produce fractional entitlements before rounding.
- **The ledger is pure.** `packages/core` touches nothing (law 1): no database, no network, no
  clock. Prices, dates and holdings arrive as arguments.
- **`xirr` is `curated_accounting.xirr`.** One solver in the codebase.
- **A holding is identified by `(instrument_id, broker_account_id)`** — the physical position.
  That pair, not `instrument_id` alone, is what gets allocated. A stock at two brokers is two
  holdings that *display* aggregated (§6.7).
- **Monitoring views never enter a total.** Any function returning a consolidated figure takes
  capital portfolios only, and the type system should make the mistake awkward.

## Status log

- **26 Aug 2026** — Survey done, plan written. Executing node **1.1.1** (pure allocation-ledger
  domain). Gates: `gates/portfolio-spine-1.1.1.md`.
  Everything else in the tree is **declared deferred, not forgotten** — the reasons are in this
  file's survey table, and node 1.1.6 is externally blocked on credentials.

- **26 Aug 2026 — node 1.1.1 DONE.** `packages/core/src/baskfy_core/allocation_ledger.py` plus
  `packages/core/tests/test_allocation_ledger.py`. Acceptance criteria **1, 2, 4, 5 and 6** are
  asserted by tests named after them. Criteria 3, 7 and 8 are Phase 2 (they are about what a
  screen shows) and remain open — see the gates file, which pins that split so this cannot be
  misread as "v1 acceptance done".

  Two defects were found and fixed on the re-read pass, both worth knowing about downstream:

  1. **Criterion 2 was only enforced on the totals path.** `portfolio_value` returned a number
     for an allocation set that `consolidated_value` refused, so the parts and the whole
     disagreed about whether the data was even legal. The duplicate check now lives in
     `_allocation_index`, which every function that needs to know where a holding sits must
     build — so the check is unavoidable rather than remembered.
  2. **The lookup was quadratic.** `allocation_of` scanned the allocation list per holding, so
     §6.5's table — a row per portfolio — was O(portfolios x holdings x allocations) *per page
     render*. Added `portfolio_values()`, which returns every row's value in one pass and
     validates once; that is the call the Overview table should use.

  **Next node, and the reason it is next:** 1.1.2, the schema. The domain now refuses a holding
  in two capital portfolios, but `portfolio_holding`'s primary key still *permits* the row —
  criterion 2 is enforced in Python and violable in SQL. Closing that needs `kind` and `source`
  columns and a uniqueness constraint on the allocation, and it touches live data, so it wants
  its own gates file and a backup step.

- **26 Aug 2026 — node 1.1.2 DONE.** Migration `0021_allocation_ledger`, plus `kind`, `source` and
  `portfolio_kind` on the models. **Criterion 2 is now a database fact**, not a Python promise:
  inserting the same `(instrument_id, broker_account_id)` into a second capital portfolio is
  refused by Postgres.

  The shape, and why not the obvious one: a unique index cannot read another table, so
  `portfolio` gained `UNIQUE (id, kind)`, `portfolio_holding` gained `portfolio_kind` with a
  composite FK `(portfolio_id, portfolio_kind) -> portfolio (id, kind)` `ON UPDATE CASCADE`, and
  a **partial unique index** covers only the CAPITAL rows. A trigger was rejected: it is skipped
  by `DISABLE TRIGGER` and some bulk-load paths, which are exactly the circumstances of the
  broker sync this constraint exists to survive. The cascade means flipping a portfolio's kind
  carries its holdings, and a flip that would collide **fails**.

  Three defects found and fixed while proving it, each by trying to break the thing rather than
  by reading it:

  1. **The migration died on pre-existing violations** with `UniqueViolationError: Key
     (instrument_id, broker_account_id)=(6, 6) is duplicated` — true, useless, and halfway
     through. It now runs a pre-flight query and refuses with the symbol, the broker account and
     both portfolio names. It deliberately does **not** pick a winner: which portfolio keeps a
     doubly-allocated holding decides whose return series it belongs to, which is §4.3's
     reconciliation question and only the owner can answer it.
  2. **Constraint names double-prefixed.** The naming convention prepends `ck_<table>_`, so
     passing `ck_portfolio_kind_known` produced `ck_portfolio_ck_portfolio_kind_known` and the
     ORM and database disagreed — the exact drift these constraints exist to prevent.
     `op.drop_constraint` applies the convention too, which is what made the first downgrade fail.
  3. **The check script was order-dependent.** `columns` passed alone and failed when run first,
     because a freshly migrated database has no `app_user`, so its "insert a bad kind" probe
     inserted zero rows and reported no violation — passing for the wrong reason.

  **Next node:** 1.1.3, the cash ledger and the Unallocated bucket (§4.4). The domain models
  Unallocated as the absence of an allocation, which is right for stocks; cash needs a real
  per-broker bucket and an internal-flow record, because §4.4 makes assigning cash to a portfolio
  the XIRR event.

---

# Full-redesign run — 26 Aug 2026

Instruction: *complete every phase; nothing blocked or remaining.* Orchestrated, with subagents.

## What "nothing blocked" can honestly mean

Two items in the spec are **externally gated** and no amount of engineering removes the gate:

* **Live broker credentials.** `NEEDS-MAULIK.md` §16: only `BASKFY_KITE_*` exists; nine brokers
  have none. The *code path* can be complete and proven against a fixture provider — the sync,
  the reconciliation it produces, the whole chain — and it is. What cannot happen here is a live
  fetch against an account nobody has logged into. So the rule for this run: **build and prove
  every path; never fake a credential, never claim a live run that did not happen.**
* **Intraday prices.** §5.1 makes EOD the v1 decision and intraday explicitly LATER, and there is
  no intraday feed wired. Phase 3's "intraday estimates" is therefore built as the *labelled
  estimate* the spec describes, fed by the same EOD series until a feed exists.

Everything else in Phases 1-3 is buildable and is planned below.

## Contracts — fixed before fan-out, because these leaves share surfaces

**Ownership, so two agents never edit one file.**

| Surface | Sole owner |
|---|---|
| Alembic migrations | Wave A only. Linear history: one migration (`0022`) carries the whole redesign schema. No other leaf writes a migration. |
| `packages/core/src/baskfy_core/allocation_ledger.py` | Already built (1.1.1). Leaves **import** it; none edits it. |
| `apps/web/src/lib/nav.ts`, route tables | Leaf D1 only. |
| Each new core module | Exactly one leaf, named in the tree below. |

**Vocabulary.** From `allocation_ledger`: `PortfolioKind`, `PortfolioSource`, `HoldingKey`,
`Holding`, `Allocation`, `ReturnFigure`, `MetricKind`. Nothing restates these as strings.

**Money and quantity are `Decimal`.** House rule 9. Quantities carry 4 dp
(`allocation_ledger.QUANTITY_PRECISION`), money 2 dp (`gst.money`).

**Purity.** New domain goes in `packages/core` and touches nothing (law 1). Anything with a
database, a network or a clock lives in `services/`.

**XIRR is `curated_accounting.xirr`.** One solver.

**Naming a constraint:** pass `<table>_<what>`; the convention prepends `ck_<table>_`. Passing a
name that already starts with `ck_` double-prefixes it (learned in 1.1.2).

## The tree, and who does what

```
1  Portfolio redesign
├─ A   schema (sole migration owner)                       migration 0022
├─ B   pure domain, parallel — disjoint files
│  ├─ B1  cash ledger + internal flows (§4.4)              cash_ledger.py
│  ├─ B2  NAV series, TWR, since-grouped, drawdown (§5)    portfolio_nav.py
│  ├─ B3  reconciliation inbox rules (§4.3)                reconciliation.py
│  ├─ B4  CAS import parser (§5.3)                         cas_import.py
│  └─ B5  overlap detection + grouping suggestions (§6.6)  grouping_suggestions.py
├─ C   services, parallel
│  ├─ C1  nightly EOD NAV job + since-grouped marks        worker
│  ├─ C2  broker holdings sync (fixture-proven)            providers + worker
│  └─ C3  API: overview, holdings, activity, reconcile     services/api
└─ D   web
   ├─ D1  nav restructure (sole owner of nav + routes)
   ├─ D2  Overview: hero, chart, table, drawer (§6)
   ├─ D3  Unallocated-first onboarding + grouping (§6.6-6.7)
   ├─ D4  detail page (§7)
   └─ D5  renames (§8) + boilerplate removal
```

## Status log (append only)

- **26 Aug 2026** — full-redesign run begins. 1.1.1 (ledger domain) and 1.1.2 (schema for kind /
  source / criterion 2) are already done and their gates are full. Wave A extends the schema for
  everything else; B–D follow.

- **26 Aug 2026, wave A done + waves B/C/D fanned out.** Migration `0022_portfolio_redesign`
  carries §4.6's three layers — `broker_cash`, `portfolio_cash_flow`, `portfolio_nav_daily`,
  `reconciliation_item` — plus `portfolio.started_on` / `benchmark_index_id` and
  `portfolio_holding.first_bought_on` / `history_source` for §5.3. Up/down/up verified. ORM
  models added to `models/accounts.py`.

  One integration defect caught by the harness rather than by review: `MONEY` in the migration
  was `Numeric(18,2)` while `models/base.MONEY` is `Numeric(20,2)` — the ORM and the database
  would have disagreed about a money column's precision, silent until a value needed the 19th
  digit. Aligned to 20,2. A second: `started_on NOT NULL` broke the 1.1.2 schema-check seed,
  which is exactly the kind of self-inflicted break an acceptance harness exists to find.

  Ten leaves running as subagents, each with a gates file the parent re-runs independently:
  B1 cash ledger · B2 NAV/TWR · B3 reconciliation · B4 CAS import · B5 grouping suggestions ·
  C1 nightly NAV job · C2 holdings sync · C3 API · D1 nav · D5 renames.
  Still to launch, after D1 lands the routes they build into: D2 Overview, D3 onboarding,
  D4 detail page.

- **26 Aug 2026, first leaves land.** B1 (cash ledger, 75 tests) and B5 (grouping suggestions,
  74 tests) verified **independently by the parent** re-running their gates — 3/3 and 4/4. That
  independent re-run is the point of orchestrated mode and it earned its keep immediately: B1
  reported a failure in *my* wave-A work rather than in its own.

  **`test_schema_matches_docs.py` was failing** because 0022's four tables were undocumented.
  The rule that test enforces is good — "a table nobody wrote down is a table nobody maintains",
  and a new table must be added to its list *and* to a docs addendum. Followed properly:
  `docs/04d-portfolio-redesign-addendum.md` now documents all four tables and the five new
  columns with their DDL and their reasoning, and the list was extended rather than the test
  silenced. 170 passing.

- **26 Aug 2026, wave B complete bar B2.** B3 (reconciliation, 63 tests) and B4 (CAS import,
  52 tests) verified independently — 5/5 and 4/4. **Whole core suite: 2,022 passed, 2 skipped,
  zero failures**, up from 1,695 before this run. The four new domain modules coexist.

  B3 reported `test_schema_matches_docs` still failing; it was reading a state fixed mid-flight
  by the 04d addendum. Confirmed directly: 170 passing. Worth recording because "an agent said it
  was broken" is not the same as "it is broken", and the parent's job is to check rather than
  relay.

  Three judgement calls from the leaves that a reviewer should see rather than discover:
  * **A frozen holding still receives its corporate action** (B3). A split is an exchange fact,
    not an attribution; postponing it leaves the position at a steadily *more* wrong quantity
    while the question waits.
  * **Cost basis is preserved to the paisa, not to full Decimal precision** (B3). A 1:7 ratio
    leaves a residue in the 28th significant digit however the arithmetic is arranged; refusing
    those would give the module an opinion about which corporate actions may happen.
  * **CAS format detection raises on a tie** (B4). NSDL's eCAS summarises CDSL holdings, so the
    word "CDSL" is not evidence of format; the two layouts order columns differently, so a wrong
    guess parses cleanly into wrong numbers.

- **26 Aug 2026, waves C and D land.** Verified independently by the parent: C1 nav job (20),
  C2 holdings sync (291), C3 API (42), D1 nav (4 gates), D2 Overview (52), D3 onboarding (33),
  D5 renames. **Thirteen leaves ALL MET.**

  **Three regressions my own migrations caused, each found by a leaf and fixed at the root:**

  1. **`portfolio_nav_daily.portfolio_id` was unwritable.** 0022 put it in the primary key, and
     Postgres makes every PK column NOT NULL — so the consolidated row, the one criterion 1 is
     computed from, could not be inserted. I had documented it as nullable twice and the
     database silently disagreed. C1 wrote `0023` (UNIQUE ... NULLS NOT DISTINCT), correctly
     judging its leaf's Goal outranked "do not write a migration". I aligned the ORM, the
     schema-docs contract and the 04d addendum.
  2. **Every `portfolio_holding` writer broke.** 0021 added `portfolio_kind` NOT NULL with no way
     for existing writers to populate it — 45 API tests failing. `0024` adds a BEFORE INSERT
     trigger that fills it from the owning portfolio. This is *not* a reversal of 0021's
     "no trigger" argument: that rejected a trigger for **enforcing** the rule (bypassable by
     `session_replication_role = replica` and bulk loads, both verified). This one **defaults** a
     value; a bypass still meets NOT NULL and the composite FK, so it fails loudly rather than
     writing a wrong row. 0019 set the precedent on the same table for `broker_account_id`.
  3. **Every `portfolio` writer broke** on `kind`/`source`/`started_on`. Two production writers
     fixed explicitly; the rest were test fixtures. Fixed the fixtures rather than adding server
     defaults — §4.1 makes the kind an arithmetic decision, and a fixture that omits it is
     describing a portfolio that cannot exist.

  Also fixed: `test_migrations` asserted a primary key 0023 deliberately removed. Rather than
  skip the table and lose the check, it now asserts the equivalent unique index — a stronger
  test than before.

  **Two real gaps D3 refused to paper over, now assigned to leaf C4:** nothing serves
  `grouping_suggestions` (§6.6's activation path was inert), and `POST /portfolios` is the old
  tree API that cannot express `kind`, `source` or a benchmark.

- **26 Aug 2026 — RUN CLOSED. Root 13/13, all 16 leaf gates ALL MET.**
  Python 2,415 passed / 2 skipped (1,695 at the start). Web 1,841 passed. Zero failures.
  Eight acceptance criteria (§11) all proven end to end, not merely unit-tested.

  Twelve subagents; every leaf's gates re-run **by the parent** rather than accepted on report.
  That caught: a leaf reporting a failure already fixed, two of my own gate patterns matching no
  test, and an `acceptance.sh` path bug that double-prefixed `decile-blueprint/` and so selected
  nothing — the harness correctly called that "a criterion nobody wrote", and it was right to.

  **Not done, and stated rather than implied:** no live broker fetch (credentials are hands-only,
  `NEEDS-MAULIK.md` §16); no intraday (§5.1 defers it and no feed exists); the onboarding confirm
  button is unwired now that `POST /portfolio` exists. Two partials show an em dash with a reason
  rather than a fabricated number: target weight/drift, and unallocated stock value pending a
  layer-1 broker-holdings table.

- **26 Aug 2026 — leaf D6: the confirm button is wired.** The last loose end from the run report.
  `POST /portfolio` existed; nothing called it. Now `src/app/actions/portfolio.ts` does, as a
  server action, and the holdings page passes it in. 6/6 gates, 25 new tests.

  Two translations were needed and neither was the identity, which is why this was more than
  passing a function reference:

  * **start -> source.** Five tiles collapse to four sources: "From broker holdings" and "Empty"
    are both `HOLDING_GROUP`, because §3's source describes where a portfolio's *rule* comes from
    and neither of those has one. Written as a total `Record`, not a `switch` with a default — a
    default would silently absorb a sixth start into the value that decides §5.2's metric.
  * **benchmark name -> index id.** The flow offers "Nifty 500"; the column is a foreign key. The
    ids are database-assigned, so they are resolved from `GET /meta/universes` rather than typed
    into the web app, where a hard-coded id stays correct only until somebody reseeds.

  The pure half was split into `draft-mapping.ts` after the first test run failed: the mapping
  test was dragging in `next-auth` through `create.ts` and could not load at all. A translation
  that needs a session to test is testing the wrong thing.

  A refusal now carries the **server's own sentence**. Criterion 2's conflict answers "HDFC Bank
  is already in Long term", naming both facts the user needs; replacing that with "Something went
  wrong" would strip exactly the information that makes it fixable. The flow stays on the review
  step so a forty-holding selection is not lost, and the button disables mid-flight — a double
  submit would ask the API to allocate the same holdings twice and earn a conflict the user did
  not cause.

  Web suite 1,869 passing (from 1,841). eslint back to the pre-existing 22 after I removed six
  `async` test handlers that had nothing to await.
