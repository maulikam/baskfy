#!/usr/bin/env bash
# Leave AWS: take the box's whole state home, verified, before anything is destroyed.
#
#     bash tools/deploy/exit-aws.sh export     # on the box: stop, dump, pack, upload to S3
#     bash tools/deploy/exit-aws.sh fetch      # here: pull the bundle to the T7, verify sha256
#
# Maulik, 29 Oct 2026: "I dont want to have aws anymore, and want to run everything locally, and
# save money." His answers the same day: the local stack is READ-ONLY (every execution and
# auto-execute flag false — Kite accepts API orders only from the registered static IP, which is
# the box's EIP, docs/07 §4a), it runs on demand rather than always on, it is reached over
# Tailscale, and AWS is torn down whole once the local copy is verified.
#
# `export` STOPS EVERY APP SERVICE ON THE BOX and leaves them stopped: the dump must be the last
# write, or rows land after it and are lost with the box. So it refuses inside the session and the
# nightly window, exactly like deploy-swing.sh. Postgres and Redis keep running for the dump.
# Nothing here deletes anything; teardown is `tf.sh destroy`, separately, after `fetch` verifies.
set -euo pipefail
export AWS_PAGER=""
export AWS_PROFILE="${AWS_PROFILE:-baskfy-poc}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STAMP="${BASKFY_EXIT_STAMP:-$(TZ=Asia/Kolkata date +%Y%m%d)}"
PREFIX="s3://baskfy-archive/exit-$STAMP"
DEST="${BASKFY_EXIT_DIR:-/Volumes/T7-Work/baskfy-aws-exit-$STAMP}/bundle"

case "${1:-}" in
export)
  read -r -d '' REMOTE <<EOF || true
set -euo pipefail
NOW=\$(TZ=Asia/Kolkata date +%H%M); DOW=\$(TZ=Asia/Kolkata date +%u)
if [ "\$DOW" -le 5 ] && { { [ "\$NOW" -ge 0830 ] && [ "\$NOW" -le 1600 ]; } || { [ "\$NOW" -ge 1840 ] && [ "\$NOW" -le 2120 ]; }; }; then
  echo "REFUSED: \$NOW IST is inside the session or the nightly window"; exit 1
fi
cd /opt/baskfy
C="docker compose -p baskfy-staging -f compose.prod.yml --env-file .env.staging.compose"
\$C stop caddy web api worker ingest-worker beat desk desk-daily swing-monitor twt-auto session-supervisor options-monitor fno-monitor
\$C ps --format '{{.Service}} {{.State}}'
X=/opt/baskfy/exit-$STAMP; rm -rf \$X; mkdir -p \$X
PG="docker exec -i baskfy-staging-postgres-1"
\$PG psql -U baskfy -d baskfy -Atc "select extversion from pg_extension where extname='timescaledb'" > \$X/timescale.version
\$PG psql -U baskfy -d baskfy -Atc "select current_setting('server_version')" > \$X/pg.version
\$PG psql -U baskfy -d baskfy -At > \$X/rowcounts.tsv <<'SQL'
select format('select %L, count(*) from %I.%I;', n.nspname||'.'||c.relname, n.nspname, c.relname)
from pg_class c join pg_namespace n on n.oid=c.relnamespace
where c.relkind in ('r','p') and n.nspname not in ('pg_catalog','information_schema','pg_toast')
  and n.nspname not like '\_timescaledb%' and n.nspname <> 'timescaledb_information'
  and not exists (select 1 from pg_inherits i where i.inhrelid=c.oid)
order by 1 \gexec
SQL
sed -i 's/|/\t/' \$X/rowcounts.tsv
\$PG pg_dump -U baskfy -d baskfy -Fc -Z 6 > \$X/baskfy.dump
# A to Z (Maulik, 29 Oct 2026: "I need all the data which are on aws here as well"): every volume
# but pgdata (whose consistent copy is the dump above), all of /opt/baskfy, every container's log.
for v in \$(docker volume ls -q | grep -v pgdata); do tar -C /var/lib/docker/volumes/\$v/_data -czf \$X/vol-\$v.tgz .; done
tar -C /opt/baskfy --exclude=./exit-$STAMP -czf \$X/config.tgz .
mkdir -p \$X/logs; for c in \$(docker ps -aq); do n=\$(docker inspect -f '{{.Name}}' \$c | tr -d /); docker logs --timestamps \$c > \$X/logs/\$n.log 2>&1; done
tar -C \$X -czf \$X/logs.tgz logs && rm -rf \$X/logs
docker ps -a --format '{{.Names}} {{.Image}} {{.Status}}' > \$X/containers.txt
chmod 600 \$X/config.tgz
(cd \$X && sha256sum * > SHA256SUMS)
aws s3 cp --recursive --only-show-errors \$X $PREFIX/
echo "tables \$(wc -l < \$X/rowcounts.tsv)  rows \$(awk -F'\t' '{s+=\$2} END {print s}' \$X/rowcounts.tsv)"
ls -la \$X
echo EXPORT OK
EOF
  BOX_TIMEOUT_SECONDS=3600 bash "$ROOT/tools/deploy/box.sh" "$REMOTE"
  ;;
fetch)
  mkdir -p "$DEST"
  aws s3 sync --only-show-errors "$PREFIX" "$DEST"
  chmod 600 "$DEST/config.tgz"
  (cd "$DEST" && shasum -a 256 -c SHA256SUMS)
  echo "FETCH OK -> $DEST"
  ;;
*) echo "usage: $0 export|fetch" >&2; exit 64;;
esac
