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

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BLUE="$ROOT/decile-blueprint"
REGION="${AWS_REGION:-ap-south-1}"
HOSTNAME_PUBLIC="${BASKFY_PUBLIC_HOST:-staging.baskfy.com}"
TAG="$(cd "$ROOT" && git rev-parse --short HEAD)"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REG="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"

echo "── login to $REG"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REG"

echo "── build web ($TAG) for https://${HOSTNAME_PUBLIC}"
docker build --platform linux/arm64 -f "$BLUE/infra/docker/Dockerfile.web" \
  -t "$REG/baskfy-web:$TAG" -t "$REG/baskfy-web:latest" \
  --build-arg "NEXT_PUBLIC_SITE_URL=https://${HOSTNAME_PUBLIC}" \
  --build-arg "NEXT_PUBLIC_API_ORIGIN=https://${HOSTNAME_PUBLIC}" \
  --build-arg "NEXT_PUBLIC_API_URL=https://${HOSTNAME_PUBLIC}/api/v1" \
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
