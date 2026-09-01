#!/usr/bin/env bash
# Derive the SES SMTP password from the IAM secret key and write it onto the box.
#
#     AWS_PROFILE=baskfy-poc bash tools/deploy/ses-credentials.sh
#
# WHY A DERIVATION AND NOT A PASSWORD
# -----------------------------------
# SES has no separate SMTP password. The username is an IAM access key id; the password is that
# key's secret run through an HMAC-SHA256 chain over five fixed strings, prefixed with a version
# byte and base64'd. AWS documents it precisely because it must be reproducible offline — and it
# means the credential can be regenerated from Terraform state rather than stored twice.
#
# The password is never printed and never passed as an SSM parameter (those are retained in
# command history). It goes into the box's env file over a single here-doc and stays there.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TF_DIR="$ROOT/decile-blueprint/infra/terraform"
REGION="${AWS_REGION:-ap-south-1}"

tf() { bash "$ROOT/tools/deploy/tf.sh" "$@"; }

USERNAME="$(tf output -raw ses_smtp_username 2>/dev/null | tr -d '\r')"
SECRET="$(cd "$TF_DIR" && docker run --rm -v "$PWD:/w" -w /w -v "$HOME/.aws:/root/.aws:ro" \
  -e AWS_PROFILE="${AWS_PROFILE:-baskfy-poc}" hashicorp/terraform:1.9 \
  output -json 2>/dev/null | python3 -c '
import json,sys
print(json.load(sys.stdin).get("ses_smtp_password_source",{}).get("value",""))' )"

# The secret is not an output (deliberately — outputs get printed). Read it from state instead.
if [ -z "$SECRET" ]; then
  SECRET="$(cd "$TF_DIR" && docker run --rm -v "$PWD:/w" -w /w -v "$HOME/.aws:/root/.aws:ro" \
    -e AWS_PROFILE="${AWS_PROFILE:-baskfy-poc}" hashicorp/terraform:1.9 \
    show -json 2>/dev/null | python3 -c '
import json,sys
doc = json.load(sys.stdin)
for r in doc.get("values",{}).get("root_module",{}).get("resources",[]):
    if r.get("type") == "aws_iam_access_key":
        print(r["values"]["secret"]); break
')"
fi
[ -n "$USERNAME" ] && [ -n "$SECRET" ] || { echo "could not read SES credentials from state" >&2; exit 1; }

# AWS's documented derivation. Any deviation produces a password SES rejects with a 535 that says
# nothing about why.
PASSWORD="$(python3 - "$SECRET" "$REGION" <<'PY'
import base64, hmac, hashlib, sys
secret, region = sys.argv[1], sys.argv[2]
def sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()
sig = sign(("AWS4" + secret).encode(), "11111111")
for part in (region, "ses", "aws4_request", "SendRawEmail"):
    sig = sign(sig, part)
print(base64.b64encode(bytes([0x04]) + sig).decode())
PY
)"

echo "  username derived : ${USERNAME}"
echo "  password derived : (not printed)"
echo "$USERNAME" > /tmp/ses.user
printf '%s' "$PASSWORD" > /tmp/ses.pass
chmod 600 /tmp/ses.pass
echo "  written to /tmp/ses.user and /tmp/ses.pass for the deploy step"
