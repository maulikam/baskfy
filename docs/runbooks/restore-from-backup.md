# Runbook — restore from backup

**When:** data loss, a destructive migration, a corrupt raw price series, or the monthly drill.
**docs/11 §Reliability:** "Postgres: nightly `pg_dump` + continuous WAL archiving to R2;
**restore drill monthly** — an untested backup is not a backup."
**Verified against:** PARTIALLY. The dump → restore → assert sequence in §"The drill" has been run
end to end against a local TimescaleDB 2.17.2-pg16 container and passes. **The R2 half has never
run**: no backup has been uploaded to or read from a real bucket, and no WAL segment has ever been
archived. See §"What has never been tested".

## What exists

| Piece | File | State |
|---|---|---|
| Nightly logical dump | `infra/backup/pg_backup.sh` | works locally; R2 upload untested |
| Restore into a database | `infra/backup/restore.sh` | works locally |
| Integrity assertions | `decile_api.integrity` | works |
| Monthly drill | `.github/workflows/restore-drill.yml` | passes; takes its own backup, not a real one |
| WAL archiving | `infra/backup/wal-archive.conf` | **never run.** Configuration only |

## Restoring — the sequence

### 1. Get the dump

```bash
# From R2 (production).
aws --endpoint-url "$DECILE_S3_ENDPOINT_URL" s3 cp \
    "s3://$DECILE_BACKUP_S3_BUCKET/$(aws --endpoint-url "$DECILE_S3_ENDPOINT_URL" \
       s3 cp "s3://$DECILE_BACKUP_S3_BUCKET/pg/LATEST" -)" .backups/

# And its digest. Do not skip this.
aws --endpoint-url "$DECILE_S3_ENDPOINT_URL" s3 cp \
    "s3://$DECILE_BACKUP_S3_BUCKET/pg/<name>.dump.sha256" .backups/
```

`restore.sh` verifies the digest before it drops anything, and refuses to continue on a mismatch. A
truncated upload restores cleanly right up to the point where it does not, and finding that out
halfway through a production restore is the worst possible moment.

### 2. Restore into a *scratch* database first

Always. Even in an incident. A restore straight over production is an irreversible bet that the
backup is good, and you have no evidence for that yet.

```bash
infra/backup/restore.sh \
  --dump .backups/decile-20260821T140000Z.dump \
  --target postgresql://decile:decile@localhost:5432/decile_restore
```

The script refuses a target whose name does not contain `_restore`, `_drill`, `_test`, `_scratch`
or `staging` unless `--force` is given, because **it drops the target database**.

What it does, and why each step is there:

* terminates connections (`DROP DATABASE` fails while anything is attached, and in an incident the
  thing attached is usually the API you are restoring under);
* `CREATE EXTENSION timescaledb` **before** the restore — docs/02 locks the extension, and a dump
  restored without it turns `ohlcv_daily` back into a plain table, which works until the
  chunk-aware queries do not;
* `timescaledb_pre_restore()` / `timescaledb_post_restore()` around `pg_restore`, so Timescale's
  background workers do not fight the restore for chunks they think they own. `post_restore` runs
  **whether or not the restore succeeded** — a database left in pre-restore mode has its background
  workers off and nobody notices;
* `ANALYZE` at the end, so the first query after a restore does not pick a plan from guesses.

### 3. Assert it

```bash
uv run python -m decile_api.integrity \
  --database-url postgresql+asyncpg://decile:decile@localhost:5432/decile_restore
```

Fifteen assertions. `pg_restore` exiting 0 is not one of them, because a dump of an empty database
also restores cleanly. The ones that matter:

* **`alembic_revision`** — the schema is at a revision. A restore from before a migration is a
  valid backup and an invalid deployment; compare it against `alembic heads`.
* **`hypertables`** — all five are hypertables, not plain tables.
* **`tables_present`** — every table the models define. Read from `Base.metadata`, so it cannot
  fall behind the schema.
* **the five orphan checks** — joins the foreign keys do not enforce.
* **`data_version_unique`** — docs/06 §Caching keys every cached result on it. Two runs sharing one
  version means two different result sets under one cache key.

`SKIP` on `data_version_unique` and `published_runs_have_steps` is normal on a database that has
never published — it is not the same as `PASS`, which is why they are reported separately.

### 4. Promote it

Only after §3 passes.

```bash
# Rename, do not copy. A second full copy of an 8.5M-row database is an hour you do not have.
psql -d postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('decile','decile_restore') AND pid <> pg_backend_pid();"
psql -d postgres -c 'ALTER DATABASE decile RENAME TO decile_broken_20260821;'
psql -d postgres -c 'ALTER DATABASE decile_restore RENAME TO decile;'
```

Keep `decile_broken_*` until you know what happened. Then, before letting traffic in:

```bash
cd services/api && uv run alembic upgrade head    # the dump may predate a migration
uv run python -m decile_api.integrity             # against the promoted database
```

And purge the caches — the restored database almost certainly has a different `data_version` from
the one Redis and Next are holding results for:

```bash
docker exec decile-redis redis-cli --scan --pattern 'screen:*' | xargs -r docker exec -i decile-redis redis-cli del
curl -X POST https://<web-host>/api/revalidate -H "x-revalidate-secret: $REVALIDATE_SECRET"
```

## Point-in-time recovery

The dump gives you last night. WAL archiving gives you any moment since — which is what
[bad-data-published.md](bad-data-published.md)'s worst case needs: replay to the second *before*
the bad ingest rather than to whenever the dump happened.

**None of this has ever run.** `infra/backup/wal-archive.conf` is written from the PostgreSQL 16
documentation. Read it in full before relying on any of the following.

1. Restore the base backup as above, but do **not** promote and do **not** start normally.
2. In the target's `postgresql.conf`:
   ```
   restore_command = 'aws --endpoint-url "${R2_ENDPOINT}" s3 cp s3://${R2_BUCKET}/wal/%f %p'
   recovery_target_time = '2026-08-21 19:45:00+05:30'
   recovery_target_action = 'promote'
   ```
   The target time is **IST**, and it is the moment *before* the event you are undoing.
3. `touch $PGDATA/recovery.signal`, start PostgreSQL, and watch the log. It replays until the
   target and then promotes.
4. Assert with `decile_api.integrity`, then §4 above.

A logical `pg_dump` **cannot** be used as the base for point-in-time recovery — PITR needs a
physical base backup (`pg_basebackup`). **That is a gap: nothing in this repository takes one.**
Until it does, WAL archiving buys nothing and the recovery point objective is "last night's dump".

## The drill

docs/11: "restore drill monthly — an untested backup is not a backup."

`.github/workflows/restore-drill.yml` runs on the 1st of each month, on `workflow_dispatch`, and on
any pull request touching `infra/backup/**`. It migrates and seeds a source database, backs it up
with `pg_backup.sh`, restores it with `restore.sh`, and asserts with `decile_api.integrity` — and
it asserts the *source* first, as a control, so a bad seed cannot be reported as a bad restore.

**Read this carefully: the drill does not read a real backup.** CI has no R2 credentials. It
therefore proves the dump flags, the digest, the size floor, the drop guard, the Timescale
pre/post-restore dance and every assertion — and proves nothing about whether the object in the
bucket is readable, whether the credentials still work, or how long fifteen years of bars takes to
restore. `docs/DECISIONS.md` §17.13 records the decision.

**Do this manually, once, against production's real bucket, and write the result here.** Until
someone has, the monthly green tick is a statement about the scripts and not about the backups.

## What has never been tested

* An upload to, or a download from, a real R2 bucket.
* A single archived WAL segment.
* Any point-in-time recovery.
* A restore of a database with real volume. Every timing above is from a seeded database of a few
  hundred rows; docs/02 sizes the real one at ~8.5M bars.
* A restore onto a *different* PostgreSQL minor version, or a different TimescaleDB version. The
  drill pins both to the image the compose file uses.
