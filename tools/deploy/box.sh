#!/usr/bin/env bash
# Run a command on the Phase A box over SSM, and wait for the result.
#
#     bash tools/deploy/box.sh 'docker compose ps'
#
# There is no SSH (docs/08 §3), so this is how anything gets run. It waits for cloud-init on the
# first call because the SSM agent registers before user-data finishes — a registered instance is
# not a provisioned one, and that difference reads exactly like a broken bootstrap.
#
# NEVER PASS A SECRET AS AN ARGUMENT HERE. `ssm send-command` parameters are retained in command
# history and visible in CloudTrail. Secrets are generated on the box by `provision-box.sh`, or
# passed as a bcrypt hash, which is not reversible.
set -euo pipefail
REGION="${AWS_REGION:-ap-south-1}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ID="${BASKFY_INSTANCE_ID:-$(cd "$ROOT/decile-blueprint/infra/terraform" && \
  docker run --rm -v "$PWD:/w" -w /w -v "$HOME/.aws:/root/.aws:ro" \
  -e AWS_PROFILE="${AWS_PROFILE:-baskfy-poc}" hashicorp/terraform:1.9 \
  output -raw ssm_command 2>/dev/null | grep -oE 'i-[0-9a-f]+')}"
[ -n "$ID" ] || { echo "no instance id; set BASKFY_INSTANCE_ID" >&2; exit 1; }

CMD_JSON="$(python3 -c '
import json,sys
cmds=["cloud-init status --wait >/dev/null 2>&1 || true"]+sys.argv[1:]
print(json.dumps({"commands":cmds}))' "$@")"

CID="$(aws ssm send-command --region "$REGION" --instance-ids "$ID" \
  --document-name AWS-RunShellScript --cli-input-json "{\"Parameters\":$CMD_JSON}" \
  --query 'Command.CommandId' --output text)"

# HOW LONG TO WAIT, AND WHY IT USED TO BE WRONG (9 Sep 2026).
#
# This polled 90 times at 3s — a hard 270-second ceiling — and then ran
# `[ "$ST" = "Success" ] || exit 1`. A command still *running* exits non-zero there, so a
# caller with `set -e` treats a healthy long step as a failure. `deploy-swing.sh` hit that on
# every single deploy: its `run --rm seed` outlives 270s, so the script aborted after applying
# the migration and before `up -d`, leaving the database a version AHEAD of the running code and
# needing the deploy finished by hand. Six deploys were completed that way before anyone traced
# it to the transport rather than the seed.
#
# Two separate fixes, because they were two separate bugs:
#   1. wait long enough — 30 minutes by default, which covers the seed, a migration and a pull;
#   2. never again report "still running" as "failed" — a timeout says so, in those words, and
#      names the command id so it can be followed on the box instead of guessed at.
BOX_TIMEOUT_SECONDS="${BOX_TIMEOUT_SECONDS:-1800}"
DEADLINE=$(( $(date +%s) + BOX_TIMEOUT_SECONDS ))
ST="Pending"
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  sleep 3
  ST="$(aws ssm get-command-invocation --region "$REGION" --command-id "$CID" \
        --instance-id "$ID" --query Status --output text 2>/dev/null || echo Pending)"
  case "$ST" in Success|Failed|Cancelled|TimedOut) break;; esac
done

aws ssm get-command-invocation --region "$REGION" --command-id "$CID" --instance-id "$ID" \
  --query 'StandardOutputContent' --output text
ERR="$(aws ssm get-command-invocation --region "$REGION" --command-id "$CID" --instance-id "$ID" \
  --query 'StandardErrorContent' --output text)"
[ -n "$ERR" ] && [ "$ERR" != "None" ] && { echo "--- stderr ---" >&2; echo "$ERR" >&2; }

case "$ST" in
  Success) exit 0;;
  InProgress|Pending|Delayed)
    cat >&2 <<MSG

STILL RUNNING after ${BOX_TIMEOUT_SECONDS}s — this is NOT a failure, and the command is very
likely still working on the box. Nothing has been rolled back.

  command id : $CID
  instance   : $ID
  follow it  : aws ssm get-command-invocation --region $REGION --command-id $CID --instance-id $ID

Re-run with a longer budget if this is expected:  BOX_TIMEOUT_SECONDS=3600 $0 ...
MSG
    exit 2;;
  *) exit 1;;
esac
