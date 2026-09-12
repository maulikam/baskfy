#!/usr/bin/env bash
# One command to put HEAD on the box.
#
#     bash tools/deploy/ship.sh
#
# It builds and pushes the three images, ships compose, migrates, restarts every service, and
# then VERIFIES — because a deploy that does not check itself is a deploy you find out about on
# Monday. Every step is idempotent; re-running after a failure is safe.
#
# WHAT IT WILL NOT DO, and none of these is an oversight:
#   * It never sets `tw_config.sleeve_capital_inr` or `BASKFY_TWT_EXECUTION_ENABLED`.
#     `docs/twt/02` §3: "never … in any circumstance." Those are yours.
#   * It never places an order. Nothing it runs can reach `OrderGateway.place`.
#   * It does not reconcile holdings or file anything into a portfolio. That is
#     `ops/bonds-portfolio/file-the-bond.py`, run deliberately and separately, because it moves
#     real positions.
#   * `deploy-swing.sh` refuses during market hours and inside the 18:40-21:15 nightly window.
#     Those guards are the point; do not add a flag to skip them.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
export AWS_PROFILE="${AWS_PROFILE:-baskfy-poc}"
TAG="$(git rev-parse --short HEAD)"

step() { printf '\n\033[1m── %s\033[0m\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

step "0. who am I, and is the session alive"
# The single most common reason this fails. The token dies daily and the refresh needs a browser,
# so no script can clear it for you -- but it can tell you in one line instead of failing deep
# inside a docker build twenty minutes later.
aws sts get-caller-identity --query Account --output text >/dev/null 2>&1 || die \
  "AWS session is dead. Run:  aws sso login --sso-session baskfy    then re-run this script."
echo "   account $(aws sts get-caller-identity --query Account --output text)"

step "1. what is about to ship"
echo "   tag $TAG  ($(git log -1 --format=%s | cut -c1-72))"
DIRTY="$(git status --porcelain | grep -v '^??' || true)"
if [ -n "$DIRTY" ]; then
  # Not fatal: push-images.sh builds from a clean worktree of HEAD by design, so uncommitted work
  # is excluded rather than smuggled in. Say so, because "I fixed it and deployed" with the fix
  # still uncommitted is a genuinely confusing half hour.
  echo "   note: uncommitted changes exist and will NOT be in the image (built from HEAD):"
  echo "$DIRTY" | sed 's/^/     /' | head -10
fi

step "2. build and push the three images"
bash tools/deploy/push-images.sh

step "3. ship compose, migrate, restart"
bash tools/deploy/deploy-swing.sh

step "4. verify"
bash tools/deploy/verify-swing.sh || die "verify-swing failed -- the box is up but not right."
OUT="$(bash tools/deploy/verify-pc-deploy.sh "$TAG")"
echo "   $OUT"
case "$OUT" in
  *"pins=3"*"running=10"*"twt_execution_true=0"*) ;;
  *) die "post-deploy check failed: $OUT" ;;
esac

printf '\n\033[32m✓ DEPLOYED %s\033[0m — all three images pinned, ten services up, no execution flag moved.\n' "$TAG"
echo "  rollback: the tag lines in /opt/baskfy/.env.staging.compose back to the previous sha, then 'up -d'."
