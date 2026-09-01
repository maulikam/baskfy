#!/usr/bin/env bash
# G3 — worker and beat start from the same image and load the real schedule.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
docker image inspect "$PY_IMAGE" >/dev/null 2>&1 || fail "run verify-api-image.sh first (builds $PY_IMAGE)"

SCHED="$(docker run --rm --platform "$PLATFORM" "$PY_IMAGE" python -c '
from baskfy_worker.celery_app import app
print(len(app.conf.beat_schedule or {}))
' 2>/dev/null)" || fail "celery app did not import"
[ "${SCHED:-0}" -gt 0 ] || fail "beat schedule is empty — the IST chain would never fire"

# Beat's shelve file goes to a volume because the working directory is root-owned; without it Beat
# crash-looped on EACCES, and a Beat that cannot persist re-fires the whole nightly chain on every
# restart. Assert the directory exists and is writable by the runtime user.
docker run --rm --platform "$PLATFORM" "$PY_IMAGE" \
  sh -c 'test -w /var/lib/baskfy' || fail "/var/lib/baskfy is not writable by the runtime user"

# The queue split from docs/08 §5, as the compose file actually declares it.
grep -q 'compute,backtest,default' "$COMPOSE_FILE" || fail "worker queue list missing from compose"
grep -q -- '- ingest' "$COMPOSE_FILE" || fail "ingest-worker queue missing from compose"

if compose ps --format '{{.Service}} {{.Status}}' 2>/dev/null | grep -qE '^(worker|beat|ingest-worker) Up'; then
  RESTARTS="$(docker inspect --format '{{.RestartCount}}' baskfy-staging-beat-1 2>/dev/null || echo 0)"
  [ "${RESTARTS:-0}" -lt 3 ] || fail "beat has restarted ${RESTARTS} times — it is crash-looping"
fi

echo "WORKER IMAGE OK — ${SCHED} scheduled jobs, schedule dir writable, queues split"
