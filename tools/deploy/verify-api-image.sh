#!/usr/bin/env bash
# G2 — the API builds off the uv workspace and answers its health route.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

docker image inspect "$PY_IMAGE" >/dev/null 2>&1 || {
  echo "building $PY_IMAGE …"
  docker build --platform "$PLATFORM" -f "$BLUE/infra/docker/Dockerfile.python" -t "$PY_IMAGE" "$BLUE" \
    >/dev/null 2>&1 || fail "python image did not build"
}

NAME="baskfy-verify-api-$$"
docker run -d --name "$NAME" --platform "$PLATFORM" -p 39312:8000 \
  -e BASKFY_DATABASE_URL="postgresql+asyncpg://x:x@127.0.0.1:5432/x" \
  -e BASKFY_REDIS_URL="redis://127.0.0.1:6379/0" \
  "$PY_IMAGE" >/dev/null || fail "container did not start"
trap 'docker rm -f "$NAME" >/dev/null 2>&1' EXIT

for _ in $(seq 1 40); do
  curl -fsS -o /dev/null "http://localhost:39312/health" 2>/dev/null && break
  sleep 1
done

# `/health` sits at the app root, NOT under API_PREFIX, and deliberately does not touch Postgres —
# which is why this passes with a database URL that points nowhere. That is the documented
# behaviour and this asserts it: a health check that needed the database would take the box out
# of rotation during the exact blip it would have ridden out.
curl -fsS "http://localhost:39312/health" >/dev/null || fail "/health did not answer"
curl -fsS -o /dev/null "http://localhost:39312/api/v1/openapi.json" || fail "openapi.json missing — the versioned router did not mount"

TZ_IN="$(docker exec "$NAME" python -c 'import time; print(time.tzname[0])')"
[ "$TZ_IN" = "IST" ] || fail "container clock is $TZ_IN, not IST (docs/08 §1)"

echo "API IMAGE OK — /health answers without a database, openapi mounted, clock is IST"
