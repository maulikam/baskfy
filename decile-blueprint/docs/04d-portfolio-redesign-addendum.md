# 04d — Addendum: the portfolio-redesign tables

`PORTFOLIO_REDESIGN.md` needs four tables and five columns that `docs/04-data-model.md` does not
define. All are additions to the data model, not changes to it. Migrations `0021_allocation_ledger`
and `0022_portfolio_redesign`.

The spec's §4.6 asks for three data layers kept **separate in the schema**, and that separation is
the organising idea here rather than a filing convenience:

1. **Broker ledger** — what a broker says is there. `broker_cash`, and `portfolio_holding`'s
   quantities.
2. **Market data** — what the exchange printed. Unchanged; `ohlcv_daily` already is this layer.
3. **Baskfy portfolio ledger** — the only layer this product authors: which holding belongs to
   which portfolio, which cash moved where, what each day was worth, and which questions are
   still open. `portfolio_cash_flow`, `portfolio_nav_daily`, `reconciliation_item`.

Collapsing them would make it impossible to say later which number came from whom, which is
exactly the argument a user will one day want settled.

---

## 1. `portfolio.kind` and `portfolio.source` (0021)

### Why

§4.1 defines two kinds of portfolio that differ **arithmetically**, not decoratively. A *capital
portfolio* sums into consolidated net worth and holds each of its holdings exclusively. A
*monitoring view* is an overlapping lens ("all defence stocks") and is excluded from every total.
§3 adds `source`, which decides the headline return metric (§5.2) and the badge on every surface.

### Values

`kind` ∈ {`CAPITAL`, `MONITORING`}.
`source` ∈ {`SUBSCRIBED`, `MY_SCREEN`, `MY_STRATEGY`, `HOLDING_GROUP`}.

Both are CHECK-constrained, and the values are generated from
`baskfy_core.allocation_ledger.PortfolioKind` / `.PortfolioSource` rather than typed twice, so the
database and the domain cannot drift into disagreeing about what a kind is.

## 2. Acceptance criterion 2, as a database fact (0021) — **REVERSED BY 0035**

> **Read this heading before the section.** Migration `0035` (10 Sep 2026) dropped the index this
> section is about. §11.2 was amended by Maulik — a holding *may* now be filed into several
> capital portfolios, a quantity at a time — and `PORTFOLIO_REDESIGN.md` §11.2a carries the new
> invariant and the reasoning. What follows is kept because the `(id, kind)` foreign-key
> machinery below is still live and still doing its job; only the partial unique index is gone.
>
> Why nothing replaced it in the database: the new rule is `sum(slices) <= held`, a fact about a
> *group* of rows. No CHECK can see it, and a trigger would re-aggregate the position on every
> write to the busiest table in the schema. Conservation comes from the write path instead —
> every path into `portfolio_holding` moves quantity rather than asserting a total.

§11.2, as it read until 10 Sep 2026: *a holding can never be in two capital portfolios.* Enforced
by Postgres, not by the application, because a rule the application owns survives exactly as long
as every writer remembers it — and a bulk broker sync is the writer most likely to forget.

That last clause turned out to be the prescient one, in the other direction: when the rule was
lifted, the bulk broker sync *was* the writer that would have got it wrong, by handing back shares
the user had filed elsewhere. See §11.2a's "double-count this nearly introduced".

The shape, since a unique index cannot read another table:

* `portfolio` carries `UNIQUE (id, kind)` — redundant against its primary key, and present only
  to be the target of the next line.
* `portfolio_holding.portfolio_kind` is a copy of the owning portfolio's kind, held honest by a
  composite foreign key `(portfolio_id, portfolio_kind) → portfolio (id, kind)` `ON UPDATE
  CASCADE`. The foreign key *is* the synchroniser; there is no second source of truth to maintain.
* A **partial unique index** `uq_portfolio_holding_one_capital_portfolio` on
  `(instrument_id, broker_account_id) WHERE portfolio_kind = 'CAPITAL'`.

Monitoring rows are absent from the index, which is §4.1 exactly: lenses overlap freely and never
enter a total. Flipping a portfolio `CAPITAL → MONITORING` drops its holdings out of the index via
the cascade; flipping back when that would collide **fails the flip**, which is the correct answer.

**A trigger was rejected.** Verified rather than assumed: bulk `COPY ... FROM STDIN` and
`SET session_replication_role = replica` — which disables triggers *and* foreign-key checks — are
both refused by the index. A trigger would have been bypassed by both.

## 3. `broker_cash` (0022) — layer 1

The Unallocated cash bucket of §4.4, one row per broker account.

Derived from `portfolio_cash_flow` and materialised anyway: §6.2 shows cash in the hero row and
§6.6 shows it in the Unallocated section, and summing a lifetime of flows to render one number on
every page load is the cheap version. `as_of` records which day the balance is true for, because
§6.1 shows the holdings-sync timestamp separately from the price timestamp and the two must never
be confused.

```sql
CREATE TABLE broker_cash (
  broker_account_id bigint PRIMARY KEY REFERENCES broker_account(id) ON DELETE CASCADE,
  balance           numeric(20,2) NOT NULL DEFAULT 0,
  as_of             date          NOT NULL
);
```

## 4. `portfolio_cash_flow` (0022) — layer 3

Every movement of money, and the **only** input to per-portfolio XIRR.

### Why a ledger of flows and not a balance column

§4.4 draws a line that a single balance erases. Money arriving from outside lands in Unallocated
and is not a portfolio event. Money *assigned* to a portfolio is an internal inflow and **is** the
XIRR cash-flow event. Buying a stock inside a portfolio is a cash→stock transfer and is **not** an
XIRR event at all. Only a record of flows can tell those three apart afterwards, and §4.4 makes
per-portfolio XIRR computable from nothing else — which is what keeps it "mathematically pure".

### Values and invariants

`kind` ∈ {`EXTERNAL_DEPOSIT`, `EXTERNAL_WITHDRAWAL`, `ASSIGN`, `RELEASE`, `BUY`, `SELL`,
`DIVIDEND`}. A CHECK constraint enforces that an external flow has **no** `portfolio_id` and an
internal one **must** name one: the boundary §4.4 draws is data, not convention.

Amounts are positive magnitudes and the kind carries the direction. A signed amount would give two
sources of truth for one direction, and the constraint polices the kind but could not police the
sign.

```sql
CREATE TABLE portfolio_cash_flow (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  broker_account_id bigint NOT NULL REFERENCES broker_account(id) ON DELETE CASCADE,
  portfolio_id      bigint REFERENCES portfolio(id) ON DELETE CASCADE,  -- NULL iff external
  kind              varchar NOT NULL,
  amount            numeric(20,2) NOT NULL,
  occurred_on       date NOT NULL,
  instrument_id     bigint REFERENCES instrument(id),   -- BUY/SELL/DIVIDEND name the stock
  quantity          numeric(20,4),
  note              varchar,
  created_at        timestamptz NOT NULL DEFAULT now()
);
```

## 5. `portfolio_nav_daily` (0022) — layer 3

The official end-of-day value. §5.1: *"v1 is end-of-day only, presented honestly, like a fund
NAV."*

### Why stored rather than computed on read

§5.1 makes this series the source for the combined chart, per-day P&L, contribution and drawdown.
Recomputing a year of it per page load would be slow, and worse would silently **rewrite history**
whenever an allocation changed. "Valued at close of {date}" is a claim about that day, and a claim
needs a record.

`portfolio_id IS NULL` is the consolidated series, deliberately the same row shape so §6.3's
combined chart and a single portfolio's chart read one table rather than two that can disagree.

`net_flow` is the day's net internal flow, and it is what makes a time-weighted return computable
from this series alone: TWR has to divide the day at each flow and cannot if the flow is lost.

`pending_reconciliation` is §4.3's freeze made visible. A day whose value could not be trusted
says so; omitting the row instead would leave a gap that a chart draws as zero.

```sql
CREATE TABLE portfolio_nav_daily (
  portfolio_id           bigint REFERENCES portfolio(id) ON DELETE CASCADE,  -- NULL = consolidated
  user_id                bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  date                   date NOT NULL,
  market_value           numeric(20,2) NOT NULL,
  cash                   numeric(20,2) NOT NULL DEFAULT 0,
  net_flow               numeric(20,2) NOT NULL DEFAULT 0,
  pending_reconciliation boolean NOT NULL DEFAULT false
);
CREATE UNIQUE INDEX uq_portfolio_nav_daily_identity
  ON portfolio_nav_daily (user_id, date, portfolio_id) NULLS NOT DISTINCT;
```

**Why a unique index and not a primary key (0023).** 0022 declared this table with
`PRIMARY KEY (user_id, date, portfolio_id)` and documented `portfolio_id` as nullable in two
places. Both cannot be true: Postgres makes every primary-key column `NOT NULL`, so the
consolidated row — the one criterion 1 is computed from — was **unwritable**, and nothing noticed
until the nightly job tried to insert it. `UNIQUE ... NULLS NOT DISTINCT` (PostgreSQL 15+; this
deployment is 16.6) expresses the intended uniqueness exactly, keeps working as an `ON CONFLICT`
arbiter for the job's upsert, and lets NULL mean what the table says it means. The ORM keeps a
mapper-level key because SQLAlchemy requires one; it is not a database constraint.

## 6. `reconciliation_item` (0022) — layer 3

A question the sync could not answer on its own. §4.3: *"We detected a sell of 100 HDFC Bank —
which portfolio?"*

Its existence is what stops a guess. While an item is `OPEN`, the affected holding's contribution
to performance is **frozen** rather than attributed to whichever portfolio looked likely — §4.3 is
explicit that we never silently corrupt a portfolio's return series.

A `RESOLVED` row must name `resolved_portfolio_id`, enforced by a CHECK. Without it "resolved"
could mean "somebody clicked something", and a return series would move on a decision nobody
recorded. `suggested_portfolio_id` is the pre-selection the UI offers: a suggestion, never an
attribution.

```sql
CREATE TABLE reconciliation_item (
  id                     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id                bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  instrument_id          bigint NOT NULL REFERENCES instrument(id),
  broker_account_id      bigint NOT NULL REFERENCES broker_account(id) ON DELETE CASCADE,
  quantity               numeric(20,4) NOT NULL,
  reason                 varchar NOT NULL,   -- UNALLOCATED_HOLDING | QUANTITY_MISMATCH | UNKNOWN_INFLOW
  state                  varchar NOT NULL DEFAULT 'OPEN',  -- OPEN | RESOLVED | DISMISSED
  suggested_portfolio_id bigint REFERENCES portfolio(id) ON DELETE SET NULL,
  resolved_portfolio_id  bigint REFERENCES portfolio(id) ON DELETE SET NULL,
  detected_on            date NOT NULL,
  resolved_at            timestamptz
);
```

## 7. Columns for §5.2 and §5.3

* `portfolio.started_on` — every metric in §5.2 is measured from a start date, and a metric with
  an implicit start date is a number nobody can check. Backfilled from `created_at`.
* `portfolio.benchmark_index_id` — §6.3 overlays a benchmark (default Nifty 500) and §6.5 lets a
  portfolio override it.
* `portfolio_holding.first_bought_on` and `.history_source` (`NONE` | `CAS` | `BROKER` | `MANUAL`)
  — §5.3's switch. Before a CAS import a holding group shows "since grouped" only; after it, true
  XIRR and since-purchase P&L unlock. The column records *which* answer we are entitled to give.
