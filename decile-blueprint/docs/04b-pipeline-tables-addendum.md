# 04b — Addendum: pipeline tables and columns not in docs/04

`PROMPTS.md` Prompts 3 and 4 need three things `docs/04-data-model.md` does not define. All three
are additions to the data model, not changes to it.

---

## 1. `ingest_cursor`

`docs/09-data-pipeline.md` names it — "resume from `ingest_cursor`" — and its §Backfill requires a
`--resume` flag, but no DDL exists for it.

### Why

`docs/09` §Backfill: "~2,300 instruments × 15 years. Budget a weekend and a resumable cursor." A
backfill that loses its place on a kill is a backfill nobody can safely run.

### Granularity

One row per **(kind, instrument, chunk window)**, where the windows are the ≤2000-day slices
`docs/09` §"Kite specifics" requires ("chunk backfills into ≤ 2000-day slices per instrument"). A
cursor therefore never spans more than one upstream request, so an interrupted run resumes at a
chunk boundary rather than restarting an instrument's whole history.

### DDL

```sql
CREATE TABLE ingest_cursor (
  kind          text   NOT NULL,        -- 'bars'; open-ended for Prompt 15's backtest fan-out
  instrument_id bigint NOT NULL REFERENCES instrument(id),
  window_start  date   NOT NULL,
  window_end    date   NOT NULL,
  status        text   NOT NULL,        -- 'pending'|'running'|'done'|'failed'
  rows_written  bigint,
  attempts      bigint NOT NULL DEFAULT 0,
  last_error    jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (kind, instrument_id, window_start),
  CHECK (window_end >= window_start)
);
CREATE INDEX ON ingest_cursor (kind, status);
```

`attempts` and `last_error` live here rather than only in `pipeline_run_step` because a backfill is
not a pipeline run: it is a one-off that may span days and be resumed by a different process.

### The property that makes resume work

Each unit is claimed, executed and marked `done` **in its own transaction**. A process killed
mid-run leaves at most one unit `running`, everything before it `done`, everything after it
`pending`. Without per-unit commits, a kill would roll back the whole weekend's progress.

---

## 2. `UNIQUE (run_id, step)` on `pipeline_run_step`

`docs/04`'s DDL gives `pipeline_run_step` an `id` primary key and no uniqueness beyond it.

Prompt 3 deliverable 2 requires every task to be "an idempotent unit that **upserts** (never
blind-inserts) and writes a `pipeline_run_step` row". Without a unique key on `(run_id, step)`
there is nothing to upsert *on*: re-running a step appends a second, contradictory record, and
`/admin/pipeline` — which `docs/09` §Observability calls "the operator UI" — shows two answers to
the same question, with no way to tell which is current.

Migration `0003_unique_pipeline_run_step` adds it.


---

## 3. `index_member_daily.source`

`docs/04`'s DDL gives `index_member_daily` four columns: `index_id`, `date`, `instrument_id`,
`weight`.

### Why

`docs/09-data-pipeline.md` §Backfill requires the column **by name**:

> index membership (from NSE historical constituent files; where unavailable pre-2018, reconstruct
> from the earliest available file and **record the reconstruction in `index_member_daily.source`
> so backtests can exclude uncertain periods**)

### Values

| Value | Meaning |
|---|---|
| `nse_file` | Read from a published NSE constituent file for that exact date. Certain. |
| `derived` | Resolved by rule — `nifty-allcap` and `etf` (`docs/06` §"Step 2"). Certain, but not from a file. |
| `reconstructed` | Carried back from the earliest observed file, because NSE publishes none for the date. **Uncertain.** |

### DDL

```sql
ALTER TABLE index_member_daily
  ADD COLUMN source text NOT NULL DEFAULT 'nse_file',
  ADD CONSTRAINT ck_index_member_daily_index_member_source
    CHECK (source IN ('nse_file', 'reconstructed', 'derived'));
```

Migration `0004_index_member_source`.

### Why the distinction is load-bearing

A reconstructed 2013 NIFTY 500 is *today's* index projected backwards — survivorship bias in its
purest form. A backtest that cannot tell it from observed membership will report a number it has
not earned. Two rules enforce this:

- reconstruction only ever reads rows already marked `nse_file`, and only from a **later** date,
  so a reconstruction is never built on another reconstruction;
- an existing `nse_file` row is never downgraded by a later backfill pass.

`decile_worker.tasks.membership.membership_sources()` is the query Prompt 15 uses to exclude
uncertain periods.

---

## `basket_snapshot` — the nightly chain's answer, stored (M30)

Not in `docs/04`, which predates the merge and describes a screener with no basket to show.

`/baskets` computed its answer per request: every bar the scanned symbols have ever had, loaded
into Polars, scored, and turned into a plan. That cost about a second while `ohlcv_daily` held two
years of history. After the deep backfill took it to nine, the page took **67 seconds**.

None of that is per-request work — the inputs change once a night — so pipeline step 11,
`refresh_basket`, computes it after `publish` and stores it here.

| column | why |
|---|---|
| `as_of` | the trade date the basket is for |
| `screen_run_id` | docs/06's cache key: definition + as-of + data version |
| `data_version` | the version `publish` bumped, which the basket is identified by |
| `payload` | the endpoint's response body, **stored whole** |
| `computed_ms` | how long it took, so a regression is visible in the table rather than only in somebody's patience |
| `computed_at` | when |

**`payload` is JSONB and deliberately not shredded into columns.** It is a record of what the
strategy wanted on a date — nothing queries across it, and the page reads exactly one row.
Shredding it would mean a migration every time the basket page gains a field, which is the
opposite of what a snapshot table is for.

**Unique on `(as_of, data_version)`**, so re-running a night replaces its own row rather than
piling up (house rule 7). A second publish on the same date writes a second row and the page takes
the newer one.

**It is a cache, and the code treats it as one.** `refresh_basket` records its own failure and
returns rather than raising, so a basket that cannot be built never holds back a `data_version`
that is otherwise good; and `GET /baskets` falls back to computing live when no snapshot exists,
because a page that 404s on a cold cache would be worse than a slow page.

---

## `portfolio_sleeve` — a portfolio run as several screens (M34)

Not in `docs/04`. The Rebalance Tracker there answers *"which symbols changed"* and knows nothing
about money (`docs/01` §8); a portfolio run as several screens needs the other question answered —
**how much goes where**.

A sleeve is one slice with its own capital and its own source.

| column | why |
|---|---|
| `portfolio_id` | owner; `ON DELETE CASCADE` |
| `name` | unique within the portfolio |
| `kind` | `screen` or `manual` |
| `screen_id` | the source, NULL for a manual sleeve |
| `capital` | `numeric(18,2)` — money is never float (house rule 9) |
| `sort_order` | the order the owner arranged them in |

**Three check constraints carry the rules structurally** rather than by convention: `kind` is one
of two values, `capital` is non-negative, and a screen sleeve must name a screen while a manual one
must not. That last pairing is what keeps *"which screen is this sleeve from"* answerable without a
join that returns NULL.

**Deleting a screen sets `screen_id` to NULL rather than cascading.** Capital allocated against a
screen that no longer exists should become visibly unsourced and need attention, not silently
vanish along with the number the owner chose.

The allocation itself is **not stored**. It is derived on read from the sleeves, the screens'
current output and — only if asked — the desk's current policy tier, because a sleeve is a standing
instruction and a stored allocation would answer last week's version of it.


---

## The portfolio graph — nesting, broker attribution, basket sleeves (0019, tree 5)

Not in `docs/04`, which describes a screener with one flat list of portfolios and no broker in
sight. Migration `0019_portfolio_graph` (on `0018_trading_path_tenancy`) makes four changes that
are one shape: a portfolio can sit inside another, a holding knows which account it is held at, a
sleeve can be sourced from a curated basket, and an investment can be filed under a portfolio.
Decisions: `docs/DECISIONS-MERGE.md` §PM1–PM5.

### 1. `portfolio` becomes a forest

| column | why |
|---|---|
| `parent_id` | nullable self-reference, `ON DELETE SET NULL`. NULL is a root |
| `broker_account_id` | nullable FK to `broker_account`, `ON DELETE SET NULL` |

```sql
ALTER TABLE portfolio ADD COLUMN parent_id         bigint NULL REFERENCES portfolio(id)      ON DELETE SET NULL;
ALTER TABLE portfolio ADD COLUMN broker_account_id bigint NULL REFERENCES broker_account(id) ON DELETE SET NULL;
ALTER TABLE portfolio ADD CONSTRAINT portfolio_parent_not_self
  CHECK (parent_id IS NULL OR parent_id <> id);
CREATE INDEX ix_portfolio_parent_id         ON portfolio (parent_id);
CREATE INDEX ix_portfolio_broker_account_id ON portfolio (broker_account_id);
```

**`broker_account_id IS NULL` is a fact, not a gap.** NOT NULL means "everything here is
attributable to this one account"; NULL means "this is a roll-up node spanning brokers". The
per-broker roll-up keeps *spans brokers* and *unattributed money* as different things, with the
wire invariant `total = sum(by_broker) + unattributed`.

**What the database owns and what it does not.** Self-parenting is the one violation a single row
can introduce, so `portfolio_parent_not_self` owns it. Cycles and the depth cap are multi-row
facts a CHECK cannot express; they live in `baskfy_core.portfolio_graph` (`MAX_DEPTH = 6`,
`ROOT_DEPTH = 1` — exactly six is legal, seven is refused), which is pure and therefore testable
without a database. `ON DELETE SET NULL` on `parent_id` promotes children to roots: deleting an
organisational label must not cascade away the holdings beneath it, the same reasoning 0013
records for `portfolio_sleeve`.

### 2. `portfolio_holding` — a holding is identified by *where* it is held

The primary key was `(portfolio_id, instrument_id)`. That key asserts a portfolio holds a name in
exactly one place, which is false the moment the same instrument is held at two brokers.

```sql
ALTER TABLE portfolio_holding ADD COLUMN broker_account_id bigint;   -- then backfilled, then:
ALTER TABLE portfolio_holding ALTER COLUMN broker_account_id SET NOT NULL;
ALTER TABLE portfolio_holding DROP CONSTRAINT pk_portfolio_holding;
ALTER TABLE portfolio_holding ADD  CONSTRAINT pk_portfolio_holding
  PRIMARY KEY (portfolio_id, instrument_id, broker_account_id);
ALTER TABLE portfolio_holding ADD  CONSTRAINT fk_portfolio_holding_broker_account_id_broker_account
  FOREIGN KEY (broker_account_id) REFERENCES broker_account(id);   -- no ON DELETE, deliberately
CREATE INDEX ix_portfolio_holding_broker_account_id ON portfolio_holding (broker_account_id);
```

**NOT NULL forces a rule for legacy rows**, and the rule is the one 0018 already established for
`cb_investment`. Resolution order, most specific first: the portfolio's own `broker_account_id`,
else the owner's default broker account, else any account the owner has, else a default account
created for them — exactly what `baskfy_api.broker_accounts.ensure_default_broker_account` does at
runtime. A `broker_account` row is a name for "this user's book at this broker"; whether the
broker is actually *connected* is answered by `broker_account.kite_user_id` and the connection
state, never by the row's existence, so attributing legacy holdings to it claims nothing new.
The `SET NOT NULL` has no rescue clause: if a row survived the backfill unattributed, the
assumption behind the migration is wrong and it must fail there, loudly.

**The trigger keeps the rule live.**

```sql
CREATE TRIGGER portfolio_holding_attribute_broker_account
BEFORE INSERT ON portfolio_holding FOR EACH ROW
EXECUTE FUNCTION portfolio_holding_attribute_broker_account();
```

The function applies the same resolution order and fires **only when the column arrives NULL**, so
a caller that names an account is never second-guessed and no holding can be written unattributed
by any writer, present or future. It exists because the live writer
(`baskfy_api.portfolios.replace_holdings`) omitted the column and would otherwise have started
failing on a NOT NULL violation the moment 0019 landed. That writer now names the column itself;
the trigger stays as the floor, not the mechanism. `SECURITY INVOKER` with a pinned `search_path`,
so it cannot be redirected by a caller's schema settings.

**No `ON DELETE` on the foreign key, and that is the design.** `SET NULL` is impossible inside a
primary key, and `CASCADE` would delete positions in order to tidy up a login. Postgres refuses
the delete instead.

**The downgrade merges; it does not delete.** Rows written after the upgrade may hold one
instrument at several brokers and the old key cannot express that, so before the column goes:
quantities are summed, `avg_price` is re-derived as the quantity-weighted mean (`ROUND(…, 4)`,
because `avg_price` is `numeric(18,4)` and rounding happens at write time — house rule 8),
`added_on` is the earliest, and only then are the folded-in rows removed. A row that predates the
upgrade is alone in its group and returns byte-identical. Verified on real data:
`10 @ 100.0000` (2026-07-01) + `30 @ 200.0000` (2026-08-01) → `40.0000 / 175.0000 / 2026-07-01`.
What is lost going down is exactly the attribution the old schema had no column for.

`packages/core/tests/test_schema_matches_docs.py` binds this document to the ORM: it asserts the
primary key is `(portfolio_id, instrument_id, broker_account_id)` and fails if the two drift apart.

### 3. `portfolio_sleeve` gains a third kind

The table is described above (M34). 0019 adds `basket_id` and admits `kind = 'basket'`:

```sql
ALTER TABLE portfolio_sleeve ADD COLUMN basket_id bigint NULL REFERENCES cb_basket(id);  -- no ON DELETE
ALTER TABLE portfolio_sleeve DROP CONSTRAINT ck_portfolio_sleeve_portfolio_sleeve_kind;
ALTER TABLE portfolio_sleeve ADD  CONSTRAINT ck_portfolio_sleeve_portfolio_sleeve_kind
  CHECK (kind IN ('screen', 'manual', 'basket'));
ALTER TABLE portfolio_sleeve ADD  CONSTRAINT ck_portfolio_sleeve_portfolio_sleeve_source
  CHECK ((kind = 'screen' AND screen_id IS NOT NULL AND basket_id IS NULL)
      OR (kind = 'manual' AND screen_id IS NULL     AND basket_id IS NULL)
      OR (kind = 'basket' AND basket_id IS NOT NULL AND screen_id IS NULL));
CREATE INDEX ix_portfolio_sleeve_basket_id ON portfolio_sleeve (basket_id);
```

The three-way pairing is what keeps *"where does this sleeve's list come from"* answerable without
a join that returns NULL — the same property the two-way version had.

**`basket_id` has no `ON DELETE` while `screen_id` has `SET NULL`, on purpose.** `SET NULL` would
contradict the pairing constraint: blanking the id while `kind` stays `'basket'` fails the CHECK,
so the delete errors anyway, with a message about a check constraint rather than about the
reference. Omitting the action makes Postgres refuse the delete outright — same outcome, honest
error.

**Going down, a basket sleeve becomes a `manual` sleeve.** Its capital stays, visibly unsourced —
the outcome 0013 chose for a sleeve whose screen went away. Deleting the row would destroy an
allocation somebody chose.

### 4. `cb_investment.portfolio_id`

```sql
ALTER TABLE cb_investment ADD COLUMN portfolio_id bigint NULL REFERENCES portfolio(id) ON DELETE SET NULL;
CREATE INDEX ix_cb_investment_portfolio_id ON cb_investment (portfolio_id);
```

**Nullable permanently, by design.** An investment may sit outside any portfolio, and every row
predating 0019 does. Filing is bookkeeping — it records where a person considers the money to live
and never places, modifies or implies an order. `ON DELETE SET NULL`: deleting a portfolio unfiles
the investment; it never deletes the money or its history.

### What is deliberately *not* stored

`held_units` per sleeve. `baskfy_core.portfolio_units` computes it, but `portfolio_holding` is
keyed by portfolio (and now broker account) while a sleeve is not a portfolio, and when two
sleeves target the same name there is no honest rule for splitting the held quantity between them.
Sleeve allocation therefore reports **target** units only. Attributing holdings to sleeves is a
real design that needs a real answer to "what happens when a sleeve is deleted" — see
`DECISIONS-MERGE.md` §PM11 before adding a column for it.

## `cb_manager` — migration 0020, and `cb_manager_revenue_share`

Before 0020 a manager was a seed row. `curated_seed` wrote exactly two — the engine and the
operator — and no column joined a manager to an account, so there was no way for a person to
become one. 0020 adds the three things that were missing.

**A person.** `user_id` is a nullable FK to `app_user`, unique where present (partial index
`uq_cb_manager_user_id`). Nullable because the two seed managers are not people: the engine is a
pipeline and the operator row predates accounts. At most one manager identity per account.

**A lifecycle.** `state` is constrained to `DRAFT`, `SUBMITTED`, `APPROVED`, `REJECTED`,
`SUSPENDED`. The legal transitions live in `baskfy_core.manager_onboarding`, not in the database
and not in the router, so one function answers for the API and its tests alike. The column
defaults to `DRAFT`; the migration backfills the two pre-existing rows to `APPROVED` because they
were publishing before a lifecycle existed, and `curated_seed` now seeds them `APPROVED` too — a
rebuilt database would otherwise come up with both seed managers unable to list their own
baskets.

**A registration that can be checked.** `sebi_reg_type` (constrained), `sebi_reg_valid_from`,
`sebi_reg_valid_to` and `sebi_reg_verified_at` join the pre-existing `sebi_reg_no`. Two check
constraints make the shape structural: `cb_manager_sebi_reg_pairing` (a type of `NONE` carries no
number, and a number requires a type) and `cb_manager_sebi_reg_window` (an end is not before a
start). Format checking is `baskfy_core.sebi_registration`, and it is syntactic only —
`sebi_reg_verified_at` is NULL until a human compares the number against the SEBI register, and
neither field is a statement by Baskfy that the holder may manage anyone's money. That is D3, and
D3 is ⚠ UNREVIEWED.

### `cb_manager_revenue_share`

| column | type | notes |
|---|---|---|
| `id` | bigserial | PK |
| `manager_id` | bigint | FK → `cb_manager.id`, `ON DELETE CASCADE` |
| `rate_bps` | integer | NOT NULL, **no default** — see below. `BETWEEN 0 AND 10000` |
| `effective_from` | date | NOT NULL |
| `effective_to` | date | NULL means still in force; must be `> effective_from` |
| `note` | text | which agreement, signed when. Displayed, never parsed |
| `created_at` | timestamptz | |

**There is deliberately no default rate.** D7 (pricing amounts) is human-track, and a default is
how a guess survives review — it never appears in a diff again. A row cannot exist without
somebody stating a number.

**Rates are superseded, not edited.** A renegotiated share is a new row with a new
`effective_from`; the old row gets an `effective_to`. An UPDATE would rewrite what a manager was
owed last quarter, which is a number somebody may already have been paid on.

The surface reading this table (`GET /managers/me/revenue-share`) is dark: it 404s while
`BASKFY_FEE_COLLECTION_ENABLED` is false, following `routers/track_b.py`. The flag is false.
