#!/usr/bin/env bash
# G4 — the whole Phase A stack runs and the site is reachable end to end through Caddy.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
require_stack
B="$(base_url)"; PW="$(gate_password)"

for svc in caddy web api worker ingest-worker beat postgres redis; do
  compose ps --format '{{.Service}} {{.Status}}' | grep -qE "^${svc} Up" \
    || fail "service '${svc}' is not up"
done

curl -fsS -o /dev/null -u "baskfy:${PW}" "$B/" || fail "landing page unreachable through Caddy"
curl -fsS -o /dev/null -u "baskfy:${PW}" "$B/api/v1/meta/universes" || fail "API unreachable through Caddy"

# The whole point of the split horizon. If server rendering were still going out to the public
# host it would be challenged by Caddy's own password and every data page would render its error
# state — which is exactly what happened before `serverApiOrigin()` existed.
LEAK="$(docker logs --since 10m baskfy-staging-web-1 2>&1 | grep -c 'ENOTFOUND\|fetch failed' || true)"
[ "${LEAK:-0}" -eq 0 ] || fail "${LEAK} server-side fetch failures — SSR is not reaching the API internally"

VER="$(compose exec -T postgres psql -U baskfy -d baskfy -tAc 'select version_num from alembic_version' 2>/dev/null)"
[ -n "$VER" ] || fail "no alembic_version row — migrations have not been applied"

echo "STACK OK — 8 services up, migrations at ${VER}, no SSR leak to the public host"
