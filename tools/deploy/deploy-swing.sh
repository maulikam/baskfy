#!/usr/bin/env bash
# SW13 — deploy the merged desk (weekly book + swing book) beside api/web/worker/beat on the
# Phase A box, per DECISIONS-SW MD20. Run from the laptop after `aws sso login`, after
# `push-images.sh` has pushed this commit's three images and `tf.sh apply` has added the
# `desk.` A record + the desk ECR pull grant:
#
#     AWS_PROFILE=baskfy-poc bash tools/deploy/deploy-swing.sh
#
# What it does, in order, every step idempotent:
#   1. ships compose.prod.yml + Caddyfile to the box through the private archive bucket
#      (the only file transport there is: no SSH, and SSM parameters land in CloudTrail);
#   2. on the box: backs up the old copies, pins the three image tags to this commit in
#      .env.staging.compose, generates BASKFY_DESK_PASSWORD there if absent (never sent), validates;
#   3. compose pull; run --rm migrate (waits, exit 0 or the deploy stops);
#   4. run --rm seed reference; seed swing --capital ₹25,00,000 --risk 0.5 (MD1/MD2, audited);
#   5. up -d api worker ingest-worker beat desk swing-monitor web caddy (caddy recreated, since a
#      bind-mounted Caddyfile is pinned by inode); prints `docker compose ps`.
# Flags: DRY_RUN=true and every BASKFY_SWING_* stay false — they are compose defaults, and this
# script writes no override. Rollback: the three image lines back to the previous tag + `up -d`.
# Override the sleeve with SWING_CAPITAL / SWING_RISK; the tag with TAG=<sha>.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BLUE="$ROOT/decile-blueprint"
HERE="$ROOT/tools/deploy"
REGION="${AWS_REGION:-ap-south-1}"
PROFILE="${AWS_PROFILE:-baskfy-poc}"
TAG="${TAG:-$(cd "$ROOT" && git rev-parse --short HEAD)}"
SWING_CAPITAL="${SWING_CAPITAL:-2500000}"
SWING_RISK="${SWING_RISK:-0.5}"
export AWS_PROFILE="$PROFILE"

say() { printf '\n── %s\n' "$*"; }
box() { bash "$HERE/box.sh" "$@"; }

say "preflight"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)" \
  || { echo "no AWS session — aws sso login --profile $PROFILE" >&2; exit 1; }
REG="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
for repo in baskfy-web baskfy-py baskfy-desk; do
  aws ecr describe-images --region "$REGION" --repository-name "$repo" --image-ids "imageTag=$TAG" \
    >/dev/null 2>&1 || { echo "$repo:$TAG is not in ECR — run tools/deploy/push-images.sh first" >&2; exit 1; }
done
BUCKET="${BASKFY_ARCHIVE_BUCKET:-$(cd "$BLUE/infra/terraform" && \
  docker run --rm -v "$PWD:/w" -w /w -v "$HOME/.aws:/root/.aws:ro" -e AWS_PROFILE="$PROFILE" \
  hashicorp/terraform:1.9 output -raw archive_bucket 2>/dev/null)}"
[ -n "$BUCKET" ] || { echo "no archive bucket; set BASKFY_ARCHIVE_BUCKET" >&2; exit 1; }
echo "   images $REG/{web,py,desk}:$TAG   bucket s3://$BUCKET"

say "1. ship compose.prod.yml + Caddyfile ($TAG)"
aws s3 cp --region "$REGION" --only-show-errors "$BLUE/infra/docker/compose.prod.yml" "s3://$BUCKET/deploy/$TAG/compose.prod.yml"
aws s3 cp --region "$REGION" --only-show-errors "$BLUE/infra/docker/Caddyfile"         "s3://$BUCKET/deploy/$TAG/Caddyfile"

# Every box step is `cd /opt/baskfy` + the compose invocation NEEDS-MAULIK §30 uses.
C='cd /opt/baskfy && docker compose --env-file .env.staging.compose -f compose.prod.yml'
STAMP="$(date +%Y%m%dT%H%M%S)"

say "2. on the box: files, image tags, desk password, validate"
box \
  "set -e; cd /opt/baskfy; for f in compose.prod.yml Caddyfile; do [ -f \$f ] && cp -p \$f \$f.bak-sw13-$STAMP; aws s3 cp --region $REGION --only-show-errors s3://$BUCKET/deploy/$TAG/\$f \$f; done; chown ec2-user:ec2-user compose.prod.yml Caddyfile; ls -l compose.prod.yml Caddyfile" \
  "set -e; cd /opt/baskfy; F=.env.staging.compose; for kv in BASKFY_WEB_IMAGE=$REG/baskfy-web:$TAG BASKFY_PY_IMAGE=$REG/baskfy-py:$TAG BASKFY_DESK_IMAGE=$REG/baskfy-desk:$TAG; do k=\${kv%%=*}; grep -q \"^\$k=\" \$F && sed -i \"s#^\$k=.*#\$kv#\" \$F || echo \"\$kv\" >> \$F; done; grep -E '^BASKFY_(WEB|PY|DESK)_IMAGE=' \$F" \
  "set -e; cd /opt/baskfy; F=.env.staging.compose; if ! grep -q '^BASKFY_DESK_PASSWORD=' \$F; then printf 'BASKFY_DESK_PASSWORD=%s\n' \"\$(python3 -c 'import secrets; print(secrets.token_hex(16))')\" >> \$F; echo 'BASKFY_DESK_PASSWORD generated on the box (hex, 32 chars; read it in an SSM shell, never through box.sh)'; else echo 'BASKFY_DESK_PASSWORD present'; fi; chmod 0600 \$F" \
  "$C config -q && echo 'compose config: ok'"

# SSM runs the lines as one script WITHOUT set -e, so every step that must gate the next one
# exits the script itself on failure (box.sh then reports Failed and `set -e` here stops).
step() { echo "$1 >/tmp/sw13.log 2>&1 || { tail -30 /tmp/sw13.log; echo \"FAILED: $2\"; exit 1; }; tail -${3:-5} /tmp/sw13.log"; }

say "3. pull + migrate"
box "$(step "$C pull -q web api desk" pull 3); echo pulled" \
    "$(step "$C run --rm migrate" "alembic upgrade head" 5)" \
    "$C exec -T postgres psql -U baskfy -d baskfy -tAc 'select version_num from alembic_version'"

say "4. seed reference; seed swing (sleeve ₹$SWING_CAPITAL, risk $SWING_RISK %)"
box "$(step "$C run --rm seed" "seed reference" 12)" \
    "$(step "$C run --rm seed python -m baskfy_api.seed swing --capital $SWING_CAPITAL --risk $SWING_RISK" "seed swing" 4)"

say "5. up"
box "$(step "$C up -d api worker ingest-worker beat desk swing-monitor web" up 12)" \
    "$(step "$C up -d --force-recreate caddy" "caddy recreate" 3)" \
    "sleep 20; $C ps"

echo
echo "DEPLOYED $TAG — now: bash tools/deploy/verify-swing.sh"
echo "rollback: set BASKFY_{WEB,PY,DESK}_IMAGE in /opt/baskfy/.env.staging.compose back to the previous tag,"
echo "          restore compose.prod.yml.bak-sw13-$STAMP + Caddyfile.bak-sw13-$STAMP, then '$C up -d --force-recreate caddy' and '$C up -d'"
echo "          (migrations do not roll back with the image — runbook §6)"
