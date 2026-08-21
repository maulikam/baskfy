#!/usr/bin/env bash
# Nightly logical backup — PROMPTS.md Prompt 17 deliverable 6, docs/11 §Reliability:
#
#   "Postgres: nightly `pg_dump` + continuous WAL archiving to R2; **restore drill monthly** —
#    an untested backup is not a backup."
#
# This script is the `pg_dump` half. WAL archiving is a PostgreSQL configuration, not a script;
# `infra/backup/wal-archive.conf` holds it and `docs/runbooks/restore-from-backup.md` explains how
# the two combine into a point-in-time restore.
#
# Design notes worth reading before changing anything:
#
#   --format=custom, not plain SQL. A custom-format dump is compressed, and `pg_restore` can
#   restore one table out of it — which is exactly what `docs/runbooks/bad-data-published.md` needs
#   when one night's factor rows are wrong and the rest of the database is fine.
#
#   Two artefacts, not one: the dump and a `.sha256` beside it. A silently truncated upload is the
#   classic way a backup turns out not to be one, and the restore drill verifies the digest before
#   it restores.
#
#   The dump is written to disk first and uploaded second. Streaming straight into `aws s3 cp -`
#   would be one fewer step and would also mean a failed dump uploads a partial object under the
#   name of a good one.
#
#   Nothing here decrypts or reads application data. It runs as a database role that can read
#   everything, which is why the object store credentials it uses should be write-only
#   (R2 tokens support this) — a compromised backup host should not be able to read last month's.
#
# Usage:
#   infra/backup/pg_backup.sh                       # uses the environment below
#   BASKFY_BACKUP_DIR=/tmp/x infra/backup/pg_backup.sh --no-upload
#
# Environment:
#   BASKFY_BACKUP_DATABASE_URL  libpq URL to dump. Falls back to BASKFY_DATABASE_URL with the
#                               SQLAlchemy `+asyncpg` driver marker stripped.
#   BASKFY_BACKUP_DIR           where dumps are written (default ./.backups)
#   BASKFY_BACKUP_S3_BUCKET     R2/S3 bucket. Empty means local-only, which is what CI uses.
#   BASKFY_BACKUP_S3_PREFIX     key prefix (default "pg")
#   BASKFY_S3_ENDPOINT_URL      R2 endpoint. Empty means AWS S3.
#   BASKFY_BACKUP_RETENTION_DAYS  local dumps older than this are deleted (default 7)

set -euo pipefail

UPLOAD=1
[[ "${1:-}" == "--no-upload" ]] && UPLOAD=0

BACKUP_DIR="${BASKFY_BACKUP_DIR:-.backups}"
S3_BUCKET="${BASKFY_BACKUP_S3_BUCKET:-}"
S3_PREFIX="${BASKFY_BACKUP_S3_PREFIX:-pg}"
RETENTION_DAYS="${BASKFY_BACKUP_RETENTION_DAYS:-7}"

# SQLAlchemy URLs carry a driver marker (`postgresql+asyncpg://`) that libpq does not understand.
# One `sed` rather than a second environment variable nobody remembers to set.
DB_URL="${BASKFY_BACKUP_DATABASE_URL:-${BASKFY_DATABASE_URL:-}}"
DB_URL="${DB_URL/postgresql+asyncpg:/postgresql:}"
DB_URL="${DB_URL/postgresql+psycopg:/postgresql:}"
if [[ -z "${DB_URL}" ]]; then
  echo "pg_backup: set BASKFY_BACKUP_DATABASE_URL or BASKFY_DATABASE_URL" >&2
  exit 2
fi

# UTC, and sortable. A backup named in local time is a backup that is out of order twice a year in
# any jurisdiction that observes DST — India does not, but the box it runs on may not be in India.
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "${BACKUP_DIR}"
DUMP="${BACKUP_DIR}/baskfy-${STAMP}.dump"

echo "pg_backup: dumping to ${DUMP}"
# --no-owner / --no-privileges: a restore into a scratch database (the drill) has different roles,
# and a dump that insists on the production role names cannot be restored anywhere else — which
# would make the drill test something other than the real backup.
pg_dump \
  --dbname="${DB_URL}" \
  --format=custom \
  --compress=6 \
  --no-owner \
  --no-privileges \
  --file="${DUMP}"

# Digest beside the dump. Linux has sha256sum; macOS has shasum. Both are checked because this
# script runs on a Hetzner box in production and on a laptop when someone is testing it.
if command -v sha256sum >/dev/null 2>&1; then
  (cd "${BACKUP_DIR}" && sha256sum "$(basename "${DUMP}")" > "$(basename "${DUMP}").sha256")
else
  (cd "${BACKUP_DIR}" && shasum -a 256 "$(basename "${DUMP}")" > "$(basename "${DUMP}").sha256")
fi

SIZE="$(wc -c < "${DUMP}" | tr -d ' ')"
echo "pg_backup: wrote ${SIZE} bytes"
# A dump smaller than this is not a database. `pg_dump` exits 0 on an empty database, so the exit
# code alone does not tell you the backup is worth keeping.
if [[ "${SIZE}" -lt 4096 ]]; then
  echo "pg_backup: dump is implausibly small (${SIZE} bytes); refusing to upload" >&2
  exit 1
fi

if [[ "${UPLOAD}" -eq 1 && -n "${S3_BUCKET}" ]]; then
  ENDPOINT_ARG=()
  [[ -n "${BASKFY_S3_ENDPOINT_URL:-}" ]] && ENDPOINT_ARG=(--endpoint-url "${BASKFY_S3_ENDPOINT_URL}")
  KEY="${S3_PREFIX}/baskfy-${STAMP}.dump"
  echo "pg_backup: uploading to s3://${S3_BUCKET}/${KEY}"
  aws "${ENDPOINT_ARG[@]}" s3 cp "${DUMP}" "s3://${S3_BUCKET}/${KEY}"
  aws "${ENDPOINT_ARG[@]}" s3 cp "${DUMP}.sha256" "s3://${S3_BUCKET}/${KEY}.sha256"
  # The pointer the restore drill reads, so "the latest backup" is a single GET rather than a
  # LIST-and-sort that a lexicographically odd key could get wrong.
  echo "${KEY}" > "${BACKUP_DIR}/LATEST"
  aws "${ENDPOINT_ARG[@]}" s3 cp "${BACKUP_DIR}/LATEST" "s3://${S3_BUCKET}/${S3_PREFIX}/LATEST"
else
  echo "${DUMP}" > "${BACKUP_DIR}/LATEST"
  echo "pg_backup: no bucket configured; kept locally only"
fi

# Local pruning only. Object-store retention is a lifecycle rule on the bucket, because a script
# that deletes remote backups is a script that can delete every remote backup.
find "${BACKUP_DIR}" -name 'baskfy-*.dump*' -mtime "+${RETENTION_DAYS}" -delete 2>/dev/null || true

echo "pg_backup: done"
