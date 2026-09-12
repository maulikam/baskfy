#!/usr/bin/env bash
# Build the three images for the deployment host (arm64) and push them to ECR: web, python, and
# since SW13 the desk (`baskfy-desk`, built from the REPO ROOT — Dockerfile.desk copies both trees).
#
#     AWS_PROFILE=baskfy-poc bash tools/deploy/push-images.sh
#
# The web image is rebuilt rather than retagged, and that is not optional: every NEXT_PUBLIC_*
# value is inlined into the client bundle at BUILD time, so an image built for localhost serves a
# bundle that talks to localhost no matter what the compose file says. Retagging the local
# verification image would deploy a site whose browser-side calls all fail.
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REGION="${AWS_REGION:-ap-south-1}"
HOSTNAME_PUBLIC="${BASKFY_PUBLIC_HOST:-staging.baskfy.com}"
TAG="$(cd "$SOURCE_ROOT" && git rev-parse --short HEAD)"

# ------------------------------------------------------------------- build from a CLEAN worktree
# WHY THE IMAGE IS NOT BUILT FROM THE FILES IN FRONT OF YOU (11 Sep 2026)
#
# `TAG` comes from HEAD but `docker build` takes its context from the working tree, so an image
# tagged `abc1234` could contain anything — a half-finished edit, another agent's uncommitted
# regenerated client, a debug print. The tag then names a commit whose contents are not what is
# running, and every later question ("what is on the box?") has no answer.
#
# It nearly shipped that way on 11 Sep 2026: the tree carried a concurrent session's regenerated
# `openapi.json` and `schema.ts` that were in no commit at all.
#
# So: export HEAD to a temporary worktree and build from that. `git worktree add --detach` is
# cheap, and the checkout is by definition exactly the tag. `BUILD_FROM_WORKTREE=0` falls back to
# the old behaviour for someone deliberately testing an uncommitted change.
CLEAN_WORKTREE=""
cleanup_worktree() {
  [ -n "$CLEAN_WORKTREE" ] || return 0
  git -C "$SOURCE_ROOT" worktree remove --force "$CLEAN_WORKTREE" >/dev/null 2>&1 || true
}
trap cleanup_worktree EXIT

if [ "${BUILD_FROM_WORKTREE:-1}" = "1" ]; then
  DIRTY="$(cd "$SOURCE_ROOT" && git status --porcelain | grep -v '^??' || true)"
  [ -z "$DIRTY" ] || {
    printf '   note: the working tree has uncommitted changes; building from HEAD (%s) anyway.\n' "$TAG"
    printf '         they will NOT be in the image. BUILD_FROM_WORKTREE=0 to include them.\n'
  }
  CLEAN_WORKTREE="$(mktemp -d)/baskfy-$TAG"
  git -C "$SOURCE_ROOT" worktree add --detach --quiet "$CLEAN_WORKTREE" HEAD
  ROOT="$CLEAN_WORKTREE"
  echo "── building from a clean worktree of $TAG at $ROOT"
else
  ROOT="$SOURCE_ROOT"
  echo "!! BUILD_FROM_WORKTREE=0 — building from the working tree; $TAG will not describe the image"
fi
BLUE="$ROOT/decile-blueprint"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REG="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"

echo "── login to $REG"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REG"

echo "── build web ($TAG) for https://${HOSTNAME_PUBLIC}"
# NEXT_PUBLIC_DESK_URL was MISSING here until 12 Sep 2026, and the omission was silent in the
# worst way. `Dockerfile.web` declares `ARG NEXT_PUBLIC_DESK_URL=` with an EMPTY default, and
# `src/lib/site.ts` falls back to `https://desk.modelbasket.in` -- production. Its own comment
# says why that matters: "Staging must set this or links silently go to production." The app now
# refuses to build without it rather than shipping a staging page that links an operator at the
# live desk, which is how this surfaced: the build failed with
# `NEXT_PUBLIC_DESK_URL must be set in production`.
#
# THE COMMENT ABOVE USED TO SIT INSIDE THE `docker build` BELOW, AND THAT BROKE THE BUILD.
# `\`-newline is spliced away before tokenizing, so an indented `#` line in the middle of a
# continuation chain does not comment one argument out -- it ends the command at that point.
# `docker build` therefore ran with no build context and no `NEXT_PUBLIC_DESK_URL`, exiting 1
# under `set -e`, and the three lines after the comment block were parsed as commands of their
# own (`--build-arg: command not found`). The fix for a note about an argument is to put it
# above the command, never between its arguments.
docker build --platform linux/arm64 -f "$BLUE/infra/docker/Dockerfile.web" \
  -t "$REG/baskfy-web:$TAG" -t "$REG/baskfy-web:latest" \
  --build-arg "NEXT_PUBLIC_SITE_URL=https://${HOSTNAME_PUBLIC}" \
  --build-arg "NEXT_PUBLIC_API_ORIGIN=https://${HOSTNAME_PUBLIC}" \
  --build-arg "NEXT_PUBLIC_API_URL=https://${HOSTNAME_PUBLIC}/api/v1" \
  --build-arg "NEXT_PUBLIC_DESK_URL=https://desk.${HOSTNAME_PUBLIC}" \
  --build-arg "BASKFY_RELEASE=$TAG" \
  "$BLUE"

echo "── build python ($TAG)"
docker build --platform linux/arm64 -f "$BLUE/infra/docker/Dockerfile.python" \
  -t "$REG/baskfy-py:$TAG" -t "$REG/baskfy-py:latest" "$BLUE"

# The web and py repositories were created by hand (runbook §1); the desk's is created here the
# first time, idempotently, so a fresh SSO session needs nothing beyond this script. The box's
# pull policy names it in infra/terraform/compute.tf (SW13) — `tf.sh apply` once after this.
aws ecr describe-repositories --region "$REGION" --repository-names baskfy-desk >/dev/null 2>&1 \
  || aws ecr create-repository --region "$REGION" --repository-name baskfy-desk \
       --image-scanning-configuration scanOnPush=true >/dev/null

echo "── build desk ($TAG) — the weekly book and the swing book, DRY_RUN baked in"
docker build --platform linux/arm64 -f "$BLUE/infra/docker/Dockerfile.desk" \
  -t "$REG/baskfy-desk:$TAG" -t "$REG/baskfy-desk:latest" "$ROOT"

echo "── push"
# Retried. A layer push timed out once on a home connection ("net/http: timeout awaiting response
# headers"), `set -e` aborted the script, and the box then pulled a stale `latest` that still had
# the old hostname compiled into its client bundle — a failure that looks like a code bug and is a
# network one. Docker resumes from the layers already uploaded, so a retry is cheap.
push() {
  local ref="$1"
  for attempt in 1 2 3 4 5; do
    if docker push "$ref"; then return 0; fi
    echo "   push failed (attempt $attempt/5), retrying in $((attempt * 10))s: $ref" >&2
    sleep $((attempt * 10))
  done
  echo "push failed after 5 attempts: $ref" >&2
  return 1
}
push "$REG/baskfy-web:$TAG"
push "$REG/baskfy-web:latest"
push "$REG/baskfy-py:$TAG"
push "$REG/baskfy-py:latest"
push "$REG/baskfy-desk:$TAG"
push "$REG/baskfy-desk:latest"

echo
echo "PUSHED $TAG"
echo "  $REG/baskfy-web:$TAG"
echo "  $REG/baskfy-py:$TAG"
echo "  $REG/baskfy-desk:$TAG"
