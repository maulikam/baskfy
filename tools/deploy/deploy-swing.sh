#!/usr/bin/env bash
# SW13 — deploy the merged desk (weekly book + swing book) beside api/web/worker/beat on the
# Phase A box, per DECISIONS-SW MD20. Run from the laptop after `aws sso login`, after
# `push-images.sh` has pushed this commit's three images and `tf.sh apply` has added the
# `desk.` A record + the desk ECR pull grant:
#
# THE NAME UNDERSELLS IT: THIS IS THE WHOLE-STACK DEPLOY (noted 11 Sep 2026)
# --------------------------------------------------------------------------
# It ships compose, pins the three image tags, runs the migration and restarts every service, so
# it is what deploys the weekly book, the swing book AND the volume-breakout sleeve — all three
# ride the same three images and the same compose file. There is no `deploy-vbt.sh` and there
# should not be: a second script would be a second place for the image tags and the nightly
# guards to drift. The only sleeve-specific step in here is the swing seed at step 4; VBT's
# equivalent is one settings write and is deliberately Maulik's, not a deploy's
# (DECISIONS-VB VB11.1 — a sleeve with no money cannot trade by accident).
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
#      KEEP_MONITOR=1 leaves `swing-monitor` alone — see "sparing the monitor" below.
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

# ------------------------------------------------------------------------- sparing the monitor
# KEEP_MONITOR=1 deploys everything EXCEPT `swing-monitor` (11 Sep 2026).
#
# The session guard below refuses a deploy during market hours because recreating the monitor
# drops whatever it is holding. That is the right default and it is a blunt one: most deploys do
# not touch swing code at all, and the monitor is the only service with in-session state worth
# protecting.
#
# So there is a third option between "wait until 15:30" and "lose a trigger": ship everything
# else now and leave the monitor on its old image until a natural restart. The monitor keeps
# running, keeps its state, and picks up the new image whenever it is next restarted. Safe
# whenever the deploy does not change swing code — and when it does, this flag is the wrong tool
# and the guard's wait is the right one.
RESTART_SERVICES="api worker ingest-worker beat desk swing-monitor web"
if [ "${KEEP_MONITOR:-0}" = "1" ]; then
  RESTART_SERVICES="api worker ingest-worker beat desk web"
fi

say() { printf '\n── %s\n' "$*"; }
box() { bash "$HERE/box.sh" "$@"; }

# ---------------------------------------------------------------------------- the nightly window
# WHY A DEPLOY REFUSES TO RUN BETWEEN 18:40 AND 21:15 IST ON A WEEKDAY (M84)
#
# `up -d` recreates the worker, and the nightly chain runs *inside* it from 18:45 for about an
# hour and fifty minutes. It has been killed that way twice:
#
#   31 Aug 2026 — three deploys in one evening redelivered the nightly; the copies woke past
#                 midnight and ran against a session that had not happened (runs 17, 18).
#   3 Sep 2026  — deploy #8 landed at ~19:55; run 25 was killed mid-chain, reaped at 20:30, and
#                 nothing re-ran it. The product served the 2 Sep session for a day.
#
# M84 makes a missed session heal itself, which is the safety net. This is the part that stops
# needing the net. `DEPLOY_DURING_NIGHTLY=1` overrides it for a deliberate emergency.
nightly_window_guard() {
  local now day hhmm
  now="$(TZ=Asia/Kolkata date '+%u %H%M')"; day="${now%% *}"; hhmm="${now##* }"
  [ "$day" -le 5 ] || return 0                      # Sat/Sun: the chain does not run
  [ "$hhmm" -ge 1840 ] && [ "$hhmm" -le 2115 ] || return 0
  if [ "${DEPLOY_DURING_NIGHTLY:-0}" = "1" ]; then
    printf '\n!! %s IST is inside the nightly window and DEPLOY_DURING_NIGHTLY=1 is set.\n' "$hhmm"
    printf '   Recreating the worker now will kill tonight'"'"'s chain; the 06:45 catch-up will re-run it.\n\n'
    return 0
  fi
  cat >&2 <<MSG

REFUSING TO DEPLOY: it is $hhmm IST, inside the nightly window (18:40-21:15, Mon-Fri).

  \`up -d\` recreates the worker, and the nightly chain runs inside it from 18:45 for about an
  hour and fifty minutes. Deploying now kills tonight's session — that is what happened on
  31 Aug and again on 3 Sep 2026, and the second one served a stale trading session for a day.

  Wait until after 21:15, or deploy tomorrow morning. If this is an emergency and losing the
  session is the lesser cost:  DEPLOY_DURING_NIGHTLY=1 bash tools/deploy/deploy-swing.sh
MSG
  exit 1
}
nightly_window_guard

# --------------------------------------------------------------------------- the trading session
# WHY A DEPLOY ALSO REFUSES TO RUN BETWEEN 09:15 AND 15:30 IST ON A WEEKDAY (11 Sep 2026)
#
# The guard above stops a deploy killing the nightly chain. This one stops it killing a **live
# trading session**, and it exists because the first one did not cover the case.
#
# Step 5 runs `up -d ... swing-monitor`, which recreates the monitor. Since SW26 (5 Sep 2026) the
# swing monitor confirms its own triggers any time between 09:15 and 15:30 with
# `BASKFY_SWING_AUTO_EXECUTE=true` — which is the box's live setting. Recreating it mid-session
# drops whatever it was holding: `docs/swing/STATUS.md` records that a monitor which starts late
# loses the signals from before it started, and there is no replay.
#
# The gap was harmless when the nightly guard was written and stopped being harmless on 5 Sep,
# when auto-execute widened from the opening ninety minutes to the whole session. It was found on
# 11 Sep 2026 by a VBT deploy that checked the box's flags before restarting anything, and the
# check is here so the next one does not have to remember.
#
# Time only, no network call: a guard that has to reach AWS to decide is a guard that fails open
# when the network is slow. `DEPLOY_DURING_SESSION=1` overrides it for a deliberate emergency.
session_window_guard() {
  local now day hhmm
  now="$(TZ=Asia/Kolkata date '+%u %H%M')"; day="${now%% *}"; hhmm="${now##* }"
  [ "$day" -le 5 ] || return 0                      # Sat/Sun: the exchange is shut
  [ "$hhmm" -ge 0915 ] && [ "$hhmm" -le 1530 ] || return 0
  if [ "${DEPLOY_DURING_SESSION:-0}" = "1" ]; then
    printf '\n!! %s IST is inside the trading session and DEPLOY_DURING_SESSION=1 is set.\n' "$hhmm"
    printf '   Recreating swing-monitor now drops whatever it is holding. There is no replay.\n\n'
    return 0
  fi
  cat >&2 <<MSG

REFUSING TO DEPLOY: it is $hhmm IST, inside the trading session (09:15-15:30, Mon-Fri).

  Step 5 recreates \`swing-monitor\`, and since SW26 that monitor confirms its own triggers
  through the whole session when BASKFY_SWING_AUTO_EXECUTE=true — the box's live setting. A
  restart drops what it was holding and the signals from before it are lost; there is no replay.

  Wait until after 15:30, or deploy at the weekend. Between 15:30 and 18:40 is the clear window
  on a weekday — after the close, before the nightly.

  IF THIS DEPLOY DOES NOT CHANGE SWING CODE, there is a third way: ship everything else now and
  leave the monitor on its old image until a natural restart.

      KEEP_MONITOR=1 DEPLOY_DURING_SESSION=1 bash tools/deploy/deploy-swing.sh

  If it DOES change swing code, that flag is the wrong tool — the monitor would keep running the
  old one. Wait.

  If this is an emergency and losing a trigger is the lesser cost:
      DEPLOY_DURING_SESSION=1 bash tools/deploy/deploy-swing.sh
MSG
  exit 1
}
session_window_guard

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
# The box's ECR login is a 12-hour token; a deploy a day after the last one finds it expired and
# `pull` fails with "Your authorization token has expired" (SW13-run, 3 Sep 2026). The login is
# minted ON the box from the instance role — nothing crosses SSM but the registry's hostname.
box "$(step "aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $REG" "ecr login on the box" 1)" \
    "$(step "$C pull -q web api desk" pull 3); echo pulled" \
    "$(step "$C run --rm migrate" "alembic upgrade head" 5)" \
    "$C exec -T postgres psql -U baskfy -d baskfy -tAc 'select version_num from alembic_version'"

say "4. seed reference; seed swing (sleeve ₹$SWING_CAPITAL, risk $SWING_RISK %)"
# THE LONGEST STEP, AND THE ONE THAT USED TO KILL THE DEPLOY (9 Sep 2026).
#
# `run --rm seed` pulls the reference universe and takes several minutes. `box.sh` used to give
# every command a hard 270 seconds and then exit non-zero — reporting a step that was still
# working as a failure — so `set -e` aborted the script here, AFTER the migration and BEFORE
# `up -d`. That leaves the database a version ahead of the running code, which is the worst of
# the three possible outcomes, and it happened on six consecutive deploys.
#
# box.sh now waits 30 minutes by default and distinguishes "still running" (exit 2, with the
# command id to follow) from "failed" (exit 1). This budget is stated anyway: it belongs next to
# the step that needs it, not only in the transport's default.
#
# The seed stays in the per-deploy path deliberately. It is idempotent — re-running writes the
# same rows — and it is what makes a deploy to a FRESH box work without a separate ceremony.
# The cost is minutes on a box that already has the data; the alternative is a first deploy that
# silently comes up with an empty universe.
BOX_TIMEOUT_SECONDS="${SEED_TIMEOUT_SECONDS:-1800}" \
box "$(step "$C run --rm seed" "seed reference" 12)" \
    "$(step "$C run --rm seed python -m baskfy_api.seed swing --capital $SWING_CAPITAL --risk $SWING_RISK" "seed swing" 4)"

say "5. up"
box "$(step "$C up -d $RESTART_SERVICES" up 12)" \
    "$(step "$C up -d --force-recreate caddy" "caddy recreate" 3)" \
    "sleep 20; $C ps"

echo
echo "DEPLOYED $TAG — now: bash tools/deploy/verify-swing.sh"
echo "rollback: set BASKFY_{WEB,PY,DESK}_IMAGE in /opt/baskfy/.env.staging.compose back to the previous tag,"
echo "          restore compose.prod.yml.bak-sw13-$STAMP + Caddyfile.bak-sw13-$STAMP, then '$C up -d --force-recreate caddy' and '$C up -d'"
echo "          (migrations do not roll back with the image — runbook §6)"
