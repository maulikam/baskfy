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

for _ in $(seq 1 90); do
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
[ "$ST" = "Success" ] || exit 1
