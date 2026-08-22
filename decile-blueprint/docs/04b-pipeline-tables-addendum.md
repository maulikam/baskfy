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

