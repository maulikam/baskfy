#!/usr/bin/env bash
# G7 — all four reported defects are gone, checked over the public internet.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
B="${BASKFY_PUBLIC_URL:-https://staging.baskfy.com}"
# No `-u` any more: the gate went with M46.4, so there is nothing to authenticate to, and this
# script no longer needs to read the password file at all.
get()  { curl -s --max-time 40 "$@"; }
code() { curl -s -o /dev/null -w '%{http_code}' --max-time 40 "$@"; }

# WHAT THIS CAN AND CANNOT SEE
# ----------------------------
# curl has the gate password but no *application* session, so every authenticated route answers
# 307 to /login. That is correct behaviour and not a defect — the first version of this script
# read it as one. So the authenticated pages are checked for "not a server error", and the things
# that actually broke are checked at the layer that does not need a session.

# 1. /api/auth/session no longer 404s. This is the whole of defect 1.
[ "$(code "$B/api/auth/session")" = "200" ] || fail "defect 1: /api/auth/session still not 200"

# 2. The 503 that crashed /market/today is gone at the source: the API now has published data.
DASH="$(code "$B/api/v1/indices/dashboard")"
[ "$DASH" = "200" ] || fail "defect 2: /indices/dashboard answered $DASH — the page would degrade or crash"
get "$B/api/v1/indices/dashboard" | grep -q '"data_version"' \
  || fail "defect 2: the dashboard payload carries no data_version"

# ...and the page itself no longer 500s. 307 (to /login) or 200 are both fine; 5xx is not.
MARKET="$(code "$B/market/today")"
case "$MARKET" in 5*) fail "defect 2: /market/today answered $MARKET";; esac
grep -q "Application error" <<<"$(get "$B/market/today")" && fail "defect 2: crash screen served"

# 3. The stale banner is gone. Matched by its BODY, not by the phrase "December 2026 update":
#    that phrase also names a legitimate footer link to `/december-2026-update`, which is a
#    deliberate announcement-page pattern (docs/01 §1, Prompt 18 §2) and must stay. The first
#    version of this check matched the link and reported the banner as still served.
#
#    The banner itself lives in the authenticated layout, so it is the unit test
#    (`src/app/__tests__/announcement.test.ts`) that proves it is gone from the source; this
#    catches it only if it were ever rendered somewhere anonymous.
BANNER_BODY="split- and bonus-adjusted history"
grep -q "$BANNER_BODY" <<<"$(get "$B/")" && fail "defect 3: the stale banner body is still served"

#    And the announcement page it linked to must still resolve — removing the banner must not
#    have broken the pattern it pointed at.
ANN="$(code "$B/december-2026-update")"
case "$ANN" in 200|307) ;; *) fail "the announcement page answered $ANN";; esac

# 4. /home exists and is wired as an authenticated route (307 to login, not 404).
HOME="$(code "$B/home")"
case "$HOME" in 404|5*) fail "defect 4: /home answered $HOME — signing in would land nowhere";; esac

# And the site is still undiscoverable.
#
# This asserted a 401 until M74. `basic_auth` was removed on 27 Aug 2026 (M46.4) because Google's
# OAuth verification fetches the homepage and the legal pages anonymously and refused the app while
# they answered 401 — six of its eight complaints were that gate. The assertion was never updated,
# so it failed on a change Maulik made deliberately, and a check that cries wolf about an intended
# state is one people learn to skip.
#
# What replaced it is what the Caddyfile now calls load bearing: of the three original mechanisms
# the first is gone, and `X-Robots-Tag` plus `/robots.txt` ARE the defence. So they are what this
# tests. `noindex` governs indexing, not fetching — a 200 here is correct.
for path in "" "privacy" "terms"; do
  hdr="$(curl -sI --max-time 30 "$B/$path" | tr -d '\r' | grep -i '^x-robots-tag:' || true)"
  grep -qi "noindex" <<<"$hdr" || fail "no noindex on /$path — the site is discoverable"
done
grep -qE '^Disallow: /$' <<<"$(curl -s --max-time 30 "$B/robots.txt")" \
  || fail "robots.txt does not disallow everything"

# The gate must not come BACK without the legal drafts being cleared, either: a 401 on the pages
# Google fetches is what broke the OAuth app the first time.
[ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$B/")" = "401" ] \
  && fail "the gate is back — Google's OAuth verification will refuse the app again"

echo "LIVE OK — session 200, dashboard 200 with data, /market/today ${MARKET} (no crash), /home ${HOME}, no stale banner, noindex + robots.txt holding"
