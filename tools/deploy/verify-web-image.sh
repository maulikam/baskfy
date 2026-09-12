#!/usr/bin/env bash
# G1 — the web app builds standalone and actually serves the site.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

docker image inspect "$WEB_IMAGE" >/dev/null 2>&1 || {
  echo "building $WEB_IMAGE …"
  # `NEXT_PUBLIC_DESK_URL` has to be passed even though this gate never clicks a desk link.
  # `Dockerfile.web` builds with `NODE_ENV=production` and declares the arg with an EMPTY
  # default; `src/lib/site.ts` throws `NEXT_PUBLIC_DESK_URL must be set in production` rather
  # than falling back to the live desk, so `next build` failed here for every caller that did
  # not pass it. The other three NEXT_PUBLIC_* values keep the Dockerfile's staging defaults --
  # this gate asserts the image renders, not what it points at. The value below is deliberately
  # an unroutable placeholder: an image built by a verification gate must not carry a URL that
  # could send an operator anywhere real.
  #
  # The build log is kept and shown on failure. It was `>/dev/null 2>&1`, so the one line that
  # named the missing variable was discarded and the gate said only "web image did not build".
  LOG="$(mktemp)"
  docker build --platform "$PLATFORM" -f "$BLUE/infra/docker/Dockerfile.web" -t "$WEB_IMAGE" \
    --build-arg "NEXT_PUBLIC_DESK_URL=http://desk.invalid" \
    "$BLUE" >"$LOG" 2>&1 || {
      tail -25 "$LOG" | sed 's/^/    /' >&2
      fail "web image did not build (full log: $LOG)"
    }
  rm -f "$LOG"
}

# Run it alone on a spare port. No API, no database: this gate is about the image, and the
# marketing pages render without either.
NAME="baskfy-verify-web-$$"
# `REVALIDATE_SECRET` is a RUNTIME variable, unlike the NEXT_PUBLIC_* set baked in above, and the
# container is unusable without it: since `2e4437e` `middleware.ts` calls
# `assertRevalidateSecretConfigured()`, which throws in production on an empty value, and
# middleware runs on every request. The container started, logged `Ready in 408ms`, and answered
# 500 to `/` — so this gate reported "landing page did not respond" for a fully working image.
# The value is a throwaway: nothing here posts to `/api/revalidate`, it only has to be non-empty.
docker run -d --name "$NAME" --platform "$PLATFORM" -p 39311:3000 \
  -e REVALIDATE_SECRET="verify-web-image-throwaway" \
  "$WEB_IMAGE" >/dev/null \
  || fail "container did not start"
trap 'docker rm -f "$NAME" >/dev/null 2>&1' EXIT

for _ in $(seq 1 40); do
  curl -fsS -o /dev/null "http://localhost:39311/" 2>/dev/null && break
  sleep 1
done

HTML="$(curl -fsS "http://localhost:39311/" 2>/dev/null)" || fail "landing page did not respond"
grep -q "<title>" <<<"$HTML" || fail "no <title> — the app did not render"

# The classic standalone failure is a container that boots and serves unstyled HTML, because
# `.next/static` and `public/` are documented exclusions from the standalone output. A 200 on `/`
# does not catch it; a stylesheet that actually loads does.
CSS="$(grep -oE '/_next/static/css/[^"]+\.css' <<<"$HTML" | head -1)"
[ -n "$CSS" ] || fail "no stylesheet referenced — .next/static was not copied into the image"
curl -fsS -o /dev/null "http://localhost:39311${CSS}" || fail "stylesheet 404s — .next/static missing at runtime"

# The work from earlier today, proving the image carries current source and not a stale layer.
grep -q "From intent to result" <<<"$HTML" || fail "landing section missing from the built page"

SIZE="$(docker image inspect "$WEB_IMAGE" --format '{{.Size}}')"
echo "WEB IMAGE OK — renders, stylesheet loads, $(( SIZE / 1024 / 1024 )) MB"
