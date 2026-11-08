# Runbook — bad data was published (and how to roll `data_version` back)

**Alert:** `data_quality_gate_failed`, or — the case this runbook is really for — no alert at all,
because a human noticed the numbers were wrong.
**Verified against:** NOT YET — written from `decile_worker.tasks.publish`,
`decile_worker.tasks.quality` and `decile_api.screener`, not from a real incident. See
`docs/runbooks/README.md`.

## Two very different situations

### When the gate catches it — nothing was published

`data_quality_gate_failed` means docs/03's step 9 did its job: the run stopped, `data_version` was
**not** bumped, and the site is still serving the previous snapshot. **This is not a data incident.
It is a pipeline incident.** Read the failing assertions and go to
[pipeline-failed.md](pipeline-failed.md).

```bash
curl -s -H "Authorization: Bearer $STAFF_JWT" \
  https://<host>/api/v1/admin/pipeline/runs?limit=1 \
  | jq '.data[0].steps[] | select(.step=="data_quality_gate") | .detail.failures'
```

The eight assertions are docs/09 §"Data-quality gate", implemented in
`decile_worker.tasks.quality`. Two of them are structural rather than empirical: **assertions 3 and
7 are constraint checks** — the primary key and NOT NULL already forbid what they assert, and they
exist to catch a migration that relaxes either. **Assertion 6 always reports SKIPPED**: it needs
free-float weights and the index divisor to reconstruct a NIFTY 50 level, and NSE publishes neither
in the files docs/09 lists. A SKIPPED assertion does not block publish (`GateReport.passed` only
looks at `FAILED`).

### When it was published — the rest of this document

The gate has eight assertions and the space of ways data can be wrong is larger than eight. If
`data_version` moved over bad data, three things are now true:

1. Every analytics endpoint is serving it.
2. Every cached screen result is keyed on it (docs/06 §Caching), so the wrong answers are *warm*.
3. Instrument pages are cached by Next's data cache under the `factsheet` tag.

## 1. Decide whether to roll back at all

Answer this before touching anything, because rolling back is visible to users and rolling back
twice is worse than rolling back once.

* **One instrument is wrong** (a corporate action mis-applied, one bad print): do **not** roll
  back. Reprocess that instrument (§4). Nobody else's numbers are affected.
* **One factor family is wrong across the universe**: roll back, fix, re-run.
* **The wrong trade date was computed**, or membership is wrong: roll back.
* **It is cosmetic** — a label, a rounding at the display layer: fix forward. A `data_version`
  rollback re-runs every cached screen for every user to fix a label.

## 2. Roll `data_version` back

There is no button for this and there deliberately is not. `data_version` is the gate's output
(docs/03 step 10), and a UI that moves it is a UI that can move it forward.

The rollback is: **clear the bad run's `data_version`, then purge the cache.** Because
`decile_api.screener.current_data_version` is `max(data_version)` over published runs, clearing the
newest one makes the previous one current again — no counter to reset, no separate table to keep in
step. That property is why `publish` was written this way.

```sql
-- 1. Look at what you are about to change. ALWAYS run this first.
SELECT id, trade_date, status, data_version, finished_at
FROM pipeline_run
WHERE data_version IS NOT NULL
ORDER BY data_version DESC
LIMIT 5;

-- 2. Clear the bad one. Note the version number down first — you will want it for the incident log.
--    `status` becomes 'failed' so /meta/status reports degraded:true and the site shows the banner,
--    which is docs/11 §Reliability's graceful degradation. Leaving it 'succeeded' would roll the
--    data back while telling every client the data is fresh.
UPDATE pipeline_run
SET data_version = NULL, status = 'failed'
WHERE id = <THE BAD RUN ID>;
```

```bash
# 3. Purge the screen cache. Entries under the rolled-back version can never be served again
#    (the version is in the key), but the *previous* version's entries were purged when the bad
#    run published, so the cache is now cold for the version you just rolled back to.
uv run celery -A decile_worker.celery_app:app call decile.compute.warm_screen_cache

# 4. Purge the web app's data cache. `data_version` is a cache tag, not a static route
#    (docs/11a §6): everything under (app) renders dynamically because the shell reads cookies,
#    and the tagged fetch is what revalidateTag invalidates.
curl -X POST https://<web-host>/api/revalidate -H "x-revalidate-secret: $REVALIDATE_SECRET"
#    It takes no body and purges one tag, `factsheet`. See apps/web/src/app/api/revalidate/route.ts.
#    NOTE: nothing calls this automatically yet — the nightly publish step does not make the
#    request (CLAUDE.md §"Open items"), so ISR otherwise refreshes on its one-hour timer.
#    During a rollback, one hour of stale factsheets is the difference this curl makes.
```

```bash
# 5. Confirm.
curl -s https://<host>/api/v1/meta/status | jq
#   data_version  -> the previous number
#   as_of         -> the previous trading day
#   degraded      -> true, and the banner is showing. Correct: the last run failed.
```

## 3. If the bad rows themselves must go

Rolling `data_version` back stops the rows being *served*. It does not delete them. Usually that is
enough — the next good run overwrites them, because every step upserts on `(instrument_id, date)`.

Delete them only when the bad rows would poison a *later* computation: a wrong `close` feeds every
window that includes that date, so a corrupt adjusted price is not fixed by tomorrow's run.

```sql
-- Scope it to one date. There is no undo.
BEGIN;
SELECT count(*) FROM factor_daily WHERE date = '2026-08-18';   -- look first
DELETE FROM factor_daily WHERE date = '2026-08-18';
DELETE FROM market_health_daily WHERE date = '2026-08-18';
COMMIT;
```

For a corrupt *price* series, do not delete — reprocess (§4). `close_raw` is the exchange print and
is not derived from anything; `close` is rebuilt from it and the corporate-action history, so
`reprocess_instrument` restores it exactly.

If the raw prints themselves are wrong, that is data loss and belongs in
[restore-from-backup.md](restore-from-backup.md) — specifically the point-in-time restore, which is
what lets you replay to the moment before the bad ingest.

## 4. Reprocess one instrument

The narrow fix, and the one that is right most of the time.

`/admin` → *Reprocess instrument* → symbol. Or:

```bash
curl -X POST -H "Authorization: Bearer $STAFF_JWT" \
  https://<host>/api/v1/admin/instruments/CUPID/reprocess
```

That enqueues `decile.compute.reprocess_instrument`, which rebuilds `close/open/high/low/volume`
from `close_raw` and the corporate-action history (docs/09 §"Adjustment algorithm"). It is
idempotent: a healthy history is rewritten to identical values, which is what makes it safe to run
when you are only *fairly* sure it is the problem.

**Rights issues are not adjusted at all.** TERP needs the subscription price and NSE's free-text
purpose usually omits it, so `decile_core.adjustments` returns `INSUFFICIENT_DATA` and leaves the
series alone rather than guessing. If the instrument had a rights issue, reprocessing will not fix
it and the step payload will say so under `unquantified`.

Factors are **not** recomputed by this. After reprocessing, re-run the nights whose factor windows
include the corrected bars (§"Re-running a night" in [pipeline-failed.md](pipeline-failed.md)) —
which, for a 12-month window, is up to 247 trading days.

## 5. Afterwards

* **Add the assertion.** The gate missed this. docs/09 lists eight and
  `decile_worker.tasks.quality` is where a ninth goes. An incident that does not become a test is
  an incident that recurs — CLAUDE.md house rule 2: the test asserts the spec, so write down what
  *should* be true, not what went wrong.
* **Record the version numbers.** Which `data_version` was bad, which one you rolled back to, and
  when. `admin_action` records the re-runs and the reprocesses but not the SQL you ran by hand.
* **Check the backtests.** A backtest that ran against the bad version stored its results
  (`backtest.metrics`, `equity_curve`, the R2 artefacts) and does **not** recompute. Nothing
  invalidates them. There is no mechanism to; deleting the affected rows is the only option, and
  it is a decision, not a procedure.
