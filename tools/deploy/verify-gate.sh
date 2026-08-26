#!/usr/bin/env bash
# G5 — the site is reachable, and still says "do not index me" on every response.
#
# WHAT THIS USED TO ASSERT
# ------------------------
# That a stranger got a 401. The password was removed on 27 Aug 2026 (M46.4) because Google's
# OAuth verification fetches the homepage, the privacy policy and the terms anonymously and
# refused the app while each of them answered 401 — six of its eight complaints were that gate.
#
# WHAT IT ASSERTS NOW, AND WHY IT IS STILL WORTH RUNNING
# ------------------------------------------------------
# The Caddyfile's introduction lists three independent mechanisms keeping an unreviewed legal
# draft off the public internet. One is gone. The other two — `X-Robots-Tag: noindex` on every
# response, and a `/robots.txt` that disallows everything — were described as the belt for the
# braces, and are now the whole defence. They are one careless edit from disappearing, and nobody
# would notice: the site would look identical and simply start appearing in search results, which
# is the outcome the original decision existed to prevent.
#
# So this script is not vestigial. It changed from asserting "a stranger is refused" to asserting
# "a stranger is served, and told not to index it" — which is the property that now matters.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
require_stack
B="$(base_url)"

code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
hdr()  { curl -s -D- -o /dev/null "$@" | tr -d '\r'; }

# ---------------------------------------------------------------- reachable
# Google fetches each of these anonymously. Any one of them failing fails the app's verification.
for path in / /privacy-policy /terms-conditions /refund-policy /disclaimer; do
  c="$(code "$B$path")"
  [ "$c" = "200" ] || fail "$path answered $c, not 200 — Google's verification will refuse the app"
done

# No path may challenge for a password any more; a stray `basic_auth` would fail verification
# again and the symptom (an app Google keeps refusing) is a long way from the cause.
for path in / /home /me /privacy-policy; do
  hdr "$B$path" | grep -qi '^www-authenticate:' && fail "$path still challenges for a password"
done

# ---------------------------------------------------------------- not discoverable
# On every response, including an error — an error page is what a stranger meets on a bad URL,
# and it reaches the client through Caddy's `handle_errors` rather than the `header` directive.
for path in / /privacy-policy /home /robots.txt /this-page-does-not-exist; do
  hdr "$B$path" | grep -qi '^x-robots-tag: *noindex' || fail "$path is missing X-Robots-Tag: noindex"
done

# robots.txt is served by Caddy, not by the app: this file has to be true even when the app is
# down, and `apps/web` legitimately serves a permissive robots.txt for the day this goes public.
[ "$(code "$B/robots.txt")" = "200" ] || fail "robots.txt is not being served"
curl -fsS "$B/robots.txt" | grep -qx 'Disallow: /' || fail "robots.txt does not disallow everything"
[ "$(code "$B/healthz")" = "200" ]    || fail "healthz is not answering"

# ---------------------------------------------------------------- the homepage Google reads
# Two of its complaints were about content, not reachability, and both are only checkable here.
HOME="$(curl -fsS "$B/")"
grep -q '/privacy-policy' <<<"$HOME" || fail "the homepage does not link to the privacy policy"
grep -qi 'baskfy'         <<<"$HOME" || fail "the homepage does not name the app; Google will report a name mismatch"

echo "GATE OK — site reachable, no password challenge, noindex everywhere, robots.txt denying"
