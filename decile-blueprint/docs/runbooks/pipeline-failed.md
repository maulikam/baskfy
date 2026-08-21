# Runbook — the nightly pipeline failed

**Alerts:** `pipeline_failed`, `pipeline_abandoned`, `publish_late`, `queue_backlog`,
`api_error_rate_high`
**Raised by:** `decile.pipeline.nightly` (in-process, on the outcome), `decile.ops.reap_abandoned_runs`
(every 15 min), `decile.ops.check_publish_deadline` (20:15 IST), and the Prometheus rules in
`infra/prometheus/alerts.yml`
**Verified against:** NOT YET — written from `decile_worker.orchestrator` and
`decile_worker.ops`, not from a real failure. See `docs/runbooks/README.md`.

## The reassuring part, first

**A failed pipeline does not serve wrong data.** docs/03 makes step 9 a hard gate: `data_version`
is bumped only by step 10, and step 10 runs only if the gate passed. So a failed night leaves the
site serving the previous, consistent snapshot with a staleness banner
(`GET /meta/status` → `degraded: true`), which is docs/11 §Reliability's "graceful degradation"
behaving as designed.

That means: **you have until tomorrow evening, not until the market opens.** Do not rush a fix that
publishes something.

## 1. Find the failing step

`/admin/pipeline` → the top row → the step list. Every step carries `status`, `duration_ms`,
`rows_in`, `rows_out` and an `error` payload with the exception type, message and traceback
(`decile_worker.steps.record_step`).

Without a browser:

```bash
curl -s -H "Authorization: Bearer $STAFF_JWT" \
  https://<host>/api/v1/admin/pipeline/runs?limit=3 | jq '.data[0]'
```

## 2. Match the shape

| What you see | What it is | Go to |
|---|---|---|
| `fetch_daily_bars` failed, error mentions the token | Kite token expired — the common case | [kite-token-expired.md](kite-token-expired.md) |
| `data_quality_gate` failed, `error.failures[]` lists assertions | The gate did its job | [bad-data-published.md](bad-data-published.md) §"When the gate catches it" |
| A run stuck in `running`, **no step rows at all** | The worker was killed | §3 below |
| `refresh_index_membership` / `fetch_corporate_actions` failed with an HTTP error | NSE published late or changed a file | §4 below |
| Every step `succeeded` but nothing published | `publish` did not run, or the run is not the latest | §5 below |
| No run row for last night at all | Beat did not fire | §6 below |

## 3. A run stuck in `running` with no steps — the worker died

This is the `pipeline_abandoned` alert, and the state is diagnostic on its own.

`decile_worker.orchestrator` runs all ten steps in **one** transaction. When the process dies, that
transaction rolls back and every `pipeline_run_step` row goes with it. The `pipeline_run` row
survives because `decile_worker.ops.begin_run` committed it on a separate session *before* the
chain opened its transaction — that is the whole reason it exists (`docs/DECISIONS.md` §17.5).

So: **a run in `running` with zero steps means the worker was killed, and nothing it did was
kept.** No partial data landed. There is nothing to clean up.

`decile.ops.reap_abandoned_runs` marks it `failed` within 15 minutes of the 90-minute stale window
(`DECILE_PIPELINE_STALE_AFTER_MINUTES`). To do it now instead of waiting:

```bash
uv run celery -A decile_worker.celery_app:app call decile.ops.reap_abandoned_runs
```

Then find out **why** the worker died before re-running, or it will die again:

```bash
docker compose -f infra/docker/compose.prod.yml logs --tail 500 worker | grep -iE "oom|killed|memory"
dmesg -T | grep -i "out of memory"          # the usual answer on a 32 GB box during a backfill
docker inspect <worker-container> --format '{{.State.OOMKilled}} {{.State.ExitCode}}'
```

`compute_factors` is the memory-hungry step (Polars over the whole universe). If the box was
OOM-killed, running the same night again with the same memory limit produces the same result.

## 4. NSE published late, or changed a file

The NSE adapter's URL shapes and column names are **unverified against a live endpoint** — the
suite is network-blocked and they were written from the documented file layout (CLAUDE.md
§"Open items"). A parse error here is at least as likely to be our reader as their file.

```bash
uv run python -m decile_providers.cli doctor
```

The raw file is archived before it is parsed (docs/09; `decile_providers.archive`), so the actual
bytes NSE served are on disk or in R2 under the fetch's key. Read them before changing a parser.

docs/03 times the chain for "NSE EOD files settle ~18:00–19:00 IST" and Beat fires at 18:45. A
late publication is a wait, not a fix: re-run at 20:00 (§7) and it will usually just work.

## 5. Every step succeeded but nothing published

Check `publish`'s step payload. It records `previous_data_version`, `data_version`,
`cache_keys_purged` and `cache_warmed`. Two things it tolerates rather than fails on:

* `cache_purge_failed` — Redis was unreachable. **This matters.** The version moved but the cache
  was not purged, so `screen:*` keys from the previous version are still there. They are keyed
  *including* `data_version` (docs/06 §Caching), so they cannot be served under the new one — they
  are dead weight, not wrong answers. Purge them when Redis is back:
  ```bash
  uv run celery -A decile_worker.celery_app:app call decile.compute.warm_screen_cache
  ```
  (the warm task rebuilds the top screens; the stale keys expire on their TTL).
* `cache_warm_failures` — one saved screen definition will not run. Cosmetic: the first user to
  run it pays a cold query.

## 6. No run row at all — Beat did not fire

```bash
docker compose -f infra/docker/compose.prod.yml ps beat
uv run celery -A decile_worker.celery_app:app inspect scheduled
```

Beat runs in `Asia/Kolkata` with `enable_utc=False` (`decile_worker.celery_app`), because every
time in docs/09's schedule is an IST wall-clock time tied to the NSE session. If the *container's*
clock or timezone is wrong, Beat fires at the wrong hour and everything looks fine in the logs.

Also check the calendar: `run_nightly_pipeline` returns `ABORTED` on a non-trading day and writes
no steps. That is correct behaviour, and on an NSE holiday it is the expected outcome.

## 7. Re-running a night

**From `/admin/pipeline`:** find the date, press **Re-run**. It publishes
`decile.pipeline.nightly` for that trade date and writes an `admin_action` row naming you.

**From a shell:**

```bash
uv run celery -A decile_worker.celery_app:app call decile.pipeline.nightly --args='["2026-08-18"]'
# or, in-process, without a broker or a worker:
make pipeline DATE=2026-08-18
```

**Re-running is safe.** docs/02 rule 3: every step upserts on `(instrument_id, date)`, so a night
that half-succeeded is completed rather than duplicated. `pipeline_run_step` is upserted on
`(run_id, step)`, so the step history is replaced rather than doubled.

**Re-running is not a fix.** If you have not found the cause, the second run fails the same way and
you have burned twenty minutes. The exception to that is §4: a late NSE file genuinely does fix
itself.

## Making yourself staff

`/admin/*` needs `app_user.is_staff`. Nothing sets it — no form, no seed, deliberately. The first
one is set by hand:

```bash
docker exec -it decile-postgres psql -U decile -d decile \
  -c "UPDATE app_user SET is_staff = true WHERE email = 'you@example.com';"
```

A non-staff caller gets **404**, not 403 (`docs/DECISIONS.md` §17.3), so "the admin page 404s" is
what a missing bit looks like. Do this before an incident.

## `queue_backlog` and `api_error_rate_high`

Neither is a pipeline failure; both route here because there is nowhere better yet.

**`queue_backlog`** names the queue. docs/02 gives each workload its own so one cannot starve
another, and which one is backed up tells you what to do: `ingest` is a rate-limited or down
provider (§4); `backtest` is demand, and docs/03 §"Scaling plan" step 4 — a dedicated worker pool —
is the documented answer; `default` means the orchestrator itself is stuck behind something.

```bash
uv run celery -A decile_worker.celery_app:app inspect active
docker exec decile-redis redis-cli llen ingest
```

**`api_error_rate_high`** is >1% 5xx over five minutes. Start at Grafana's *Decile — API and cache*
dashboard, "Request rate by route" and "Latency p95 by route": a single route at 100% error is a
bug in that handler; every route at once is the database, Redis, or the connection pool
(`DECILE_DB_POOL_TIMEOUT_SECONDS` exhaustion shows up as 500s, not as slowness — see
`decile_api.db`).
