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
