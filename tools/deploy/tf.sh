#!/usr/bin/env bash
# Terraform, through the official image, with your SSO credentials actually reaching it.
#
#     bash tools/deploy/tf.sh init
#     bash tools/deploy/tf.sh plan
#     bash tools/deploy/tf.sh apply
#
# WHY A WRAPPER
# -------------
# There is no terraform binary on this machine and installing one is a second thing to keep
# patched, so it runs in a container. But a container sees none of your credentials by default,
# and the failure is confusing rather than obvious: `terraform plan` reports
# "No valid credential sources found" while `aws sts get-caller-identity` works fine in the very
# same shell.
#
# Two things have to cross the boundary, and missing either produces that error:
#
#   1. `~/.aws` — the profile definition (`config`) AND the SSO token cache (`sso/cache`), which
#      is where `aws sso login` puts the short-lived credential. Mounting only `config` gets you
#      a profile the container cannot resolve.
#   2. `AWS_PROFILE` — the container has no shell profile and no default; without this it looks
#      for `[default]`, which an SSO setup does not create.
#
# Read-only on the mount, because Terraform has no business writing to your credential store.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TF_DIR="$ROOT/decile-blueprint/infra/terraform"
TF_IMAGE="${TF_IMAGE:-hashicorp/terraform:1.9}"
PROFILE="${AWS_PROFILE:-baskfy}"
REGION="${AWS_REGION:-ap-south-1}"

[ -d "$HOME/.aws" ] || {
  echo "no ~/.aws — run: aws configure sso" >&2; exit 1;
}

# Fail here rather than three minutes into an apply. An SSO token lasts hours, not days, and the
# expiry message from inside a container is markedly less clear than this one.
if ! aws sts get-caller-identity --profile "$PROFILE" >/dev/null 2>&1; then
  echo "profile '$PROFILE' has no valid session." >&2
  echo "  aws sso login --profile $PROFILE" >&2
  exit 1
fi

# `-t` only when there is a terminal to attach. Under an agent or in CI stdin is a pipe, and
# `docker run -it` fails outright with "cannot attach stdin to a TTY-enabled container".
TTY_FLAGS=(-i)
[ -t 0 ] && TTY_FLAGS+=(-t)

exec docker run --rm "${TTY_FLAGS[@]}" \
  -v "$TF_DIR:/w" -w /w \
  -v "$HOME/.aws:/root/.aws:ro" \
  -e AWS_PROFILE="$PROFILE" \
  -e AWS_REGION="$REGION" \
  -e AWS_DEFAULT_REGION="$REGION" \
  "$TF_IMAGE" "$@"
