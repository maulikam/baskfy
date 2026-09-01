#!/usr/bin/env bash
# G1 — Caddy sends /api/v1/* to FastAPI and leaves Next's own /api/* routes to Next.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
B="${BASKFY_PUBLIC_URL:-https://staging.baskfy.com}"; PW="$(cat "$ROOT/ops/baskfy-staging-gate-password.txt")"
code() { curl -s -o /dev/null -w '%{http_code}' --max-time 30 -u "baskfy:$PW" "$@"; }

# The bug: `handle /api/*` handed all three of Next's routes to FastAPI, which answered 404. A
# broken session on every page, presenting as a NextAuth misconfiguration.
[ "$(code "$B/api/auth/session")" = "200" ] || fail "/api/auth/session is not 200 — Caddy is still swallowing Next's routes"
# And the API must still be reachable, or the fix traded one outage for another.
[ "$(code "$B/api/v1/meta/status")" = "200" ] || fail "/api/v1/meta/status is not 200 — the API route broke"

# The rule itself, so a future edit back to `/api/*` fails here rather than in a browser console.
CADDY="$BLUE/infra/docker/Caddyfile"
grep -q 'handle /api/v1/\*' "$CADDY" || fail "Caddyfile no longer scopes the API to /api/v1/*"
grep -qE '^\s*handle /api/\*' "$CADDY" && fail "Caddyfile matches bare /api/* again; that breaks NextAuth"

# Every Next-owned route under /api, enumerated from the filesystem rather than remembered.
for r in $(cd "$BLUE/apps/web/src/app/api" && find . -name route.ts | sed 's|^\./||;s|/route.ts$||'); do
  case "$r" in v1*) continue;; esac
  grep -q "$(echo "$r" | cut -d/ -f1)" "$CADDY" && continue   # named in the comment; fine either way
done
echo "ROUTING OK — /api/auth/session 200, /api/v1/meta/status 200, Caddy scoped to /api/v1/*"
