#!/usr/bin/env bash
# Restore a `pg_backup.sh` dump into a database — PROMPTS.md Prompt 17 deliverables 5 and 6.
#
# Used two ways, and they must be the same script or the drill proves nothing:
#
#   1. `docs/runbooks/restore-from-backup.md`, by a human, against a real incident.
#   2. `.github/workflows/restore-drill.yml`, monthly, against a scratch database.
#
# docs/11 §Reliability: "restore drill monthly — an untested backup is not a backup."
#
# THIS SCRIPT DROPS AND RECREATES THE TARGET DATABASE. It refuses to run against a target whose
# name does not look like a scratch or staging database unless --force is given, because the one
# irreversible mistake available here is restoring last week over production.
#
# Usage:
#   infra/backup/restore.sh --dump .backups/baskfy-20260821T140000Z.dump \
#                           --target postgresql://baskfy:baskfy@localhost:5433/baskfy_restore
#   infra/backup/restore.sh --dump ... --target ... --force     # allows a non-scratch name
#
# Environment:
#   BASKFY_ADMIN_DATABASE_URL   a URL on the same server pointing at `postgres`, used to issue the
#                               DROP/CREATE. Derived from --target if unset.

set -euo pipefail

DUMP=""
TARGET=""
FORCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dump) DUMP="$2"; shift 2 ;;
    --target) TARGET="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    *) echo "restore: unknown argument $1" >&2; exit 2 ;;
  esac
done

[[ -n "${DUMP}" && -n "${TARGET}" ]] || { echo "restore: --dump and --target are required" >&2; exit 2; }
[[ -f "${DUMP}" ]] || { echo "restore: ${DUMP} does not exist" >&2; exit 2; }

TARGET="${TARGET/postgresql+asyncpg:/postgresql:}"
DBNAME="$(basename "${TARGET%%\?*}")"

# The guard. `_restore`, `_drill`, `_test`, `_scratch` and `staging` are the names the drill and
# the runbook use; anything else needs --force and a moment's thought.
if [[ "${FORCE}" -eq 0 && ! "${DBNAME}" =~ (_restore|_drill|_test|_scratch|staging) ]]; then
  cat >&2 <<MSG
restore: refusing to restore into '${DBNAME}'.

  This script DROPS the target database. The name does not contain _restore, _drill, _test,
  _scratch or staging, so it may be production.

  If you mean it — and docs/runbooks/restore-from-backup.md says when you would — pass --force.
MSG
  exit 3
fi

# Verify the digest first, if one is beside the dump. A truncated upload restores cleanly right up
# to the point where it does not, and finding that out halfway through a production restore is the
# worst possible moment.
if [[ -f "${DUMP}.sha256" ]]; then
  echo "restore: verifying ${DUMP}.sha256"
  DIR="$(dirname "${DUMP}")"
  BASE="$(basename "${DUMP}")"
  # Short flags on purpose: GNU coreutils, BusyBox and Perl's shasum all accept `-c` and `-s`,
  # and BusyBox — which is what an alpine-based image ships — accepts neither long form.
  if command -v sha256sum >/dev/null 2>&1; then
    (cd "${DIR}" && sha256sum -c -s "${BASE}.sha256")
  else
    (cd "${DIR}" && shasum -a 256 -c -s "${BASE}.sha256")
  fi
  echo "restore: digest ok"
else
  echo "restore: WARNING — no .sha256 beside the dump; integrity unverified" >&2
fi

ADMIN_URL="${BASKFY_ADMIN_DATABASE_URL:-${TARGET%/*}/postgres}"

echo "restore: recreating ${DBNAME}"
# Terminate first: `DROP DATABASE` fails while anything is connected, and in an incident the thing
# still connected is usually the API you are about to restore under.
psql --dbname="${ADMIN_URL}" -v ON_ERROR_STOP=1 -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${DBNAME}' AND pid <> pg_backend_pid();" \
  >/dev/null
psql --dbname="${ADMIN_URL}" -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS \"${DBNAME}\";"
psql --dbname="${ADMIN_URL}" -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"${DBNAME}\";"

# docs/02 locks "PostgreSQL 16 + TimescaleDB". The extension must exist before the dump's
# hypertable metadata is restored, and `timescaledb_pre_restore()` is what stops the background
# workers from fighting the restore for the chunks they think they own.
psql --dbname="${TARGET}" -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"
psql --dbname="${TARGET}" -v ON_ERROR_STOP=1 -c "SELECT timescaledb_pre_restore();" >/dev/null

echo "restore: restoring ${DUMP}"
# --no-owner: the dump was taken with --no-owner, and the scratch database has different roles.
# --single-transaction is deliberately NOT used: a Timescale restore issues statements that cannot
# all share one transaction, and a half-restored scratch database is a diagnosable state, whereas
# a rollback tells you only that something failed.
#
# `pg_restore` exits non-zero on *warnings* too (a missing role, a comment on an object it did not
# create). The exit code is captured and re-checked after `timescaledb_post_restore()`, which must
# run whether or not the restore was clean — leaving a database in pre-restore mode is worse than
# a failed restore, because its background workers stay off and nobody notices.
set +e
pg_restore --dbname="${TARGET}" --no-owner --no-privileges --exit-on-error "${DUMP}"
RESTORE_STATUS=$?
set -e

psql --dbname="${TARGET}" -v ON_ERROR_STOP=1 -c "SELECT timescaledb_post_restore();" >/dev/null

if [[ "${RESTORE_STATUS}" -ne 0 ]]; then
  echo "restore: pg_restore exited ${RESTORE_STATUS}" >&2
  exit "${RESTORE_STATUS}"
fi

# ANALYZE, not VACUUM FULL. A freshly restored database has no statistics, so the first query
# against it picks a plan from guesses — which would make the integrity assertions that follow
# look slow for a reason that has nothing to do with the backup.
psql --dbname="${TARGET}" -v ON_ERROR_STOP=1 -c "ANALYZE;" >/dev/null

echo "restore: done. Now run the integrity assertions:"
echo "  BASKFY_DATABASE_URL='${TARGET/postgresql:/postgresql+asyncpg:}' uv run python -m baskfy_api.integrity"
