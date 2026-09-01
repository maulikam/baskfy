#!/usr/bin/env bash
# G1 — the web app builds standalone and actually serves the site.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

docker image inspect "$WEB_IMAGE" >/dev/null 2>&1 || {
  echo "building $WEB_IMAGE …"
  docker build --platform "$PLATFORM" -f "$BLUE/infra/docker/Dockerfile.web" -t "$WEB_IMAGE" "$BLUE" \
    >/dev/null 2>&1 || fail "web image did not build"
}

# Run it alone on a spare port. No API, no database: this gate is about the image, and the
# marketing pages render without either.
NAME="baskfy-verify-web-$$"
docker run -d --name "$NAME" --platform "$PLATFORM" -p 39311:3000 "$WEB_IMAGE" >/dev/null \
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
