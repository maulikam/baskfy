#!/usr/bin/env bash
# Generate the staging gate's credentials, correctly escaped for Docker Compose.
#
#     bash tools/deploy/gate-password.sh            # generate a password
#     bash tools/deploy/gate-password.sh --stdin    # hash one you already have
#
# WHY THIS SCRIPT EXISTS RATHER THAN A LINE IN THE RUNBOOK
# --------------------------------------------------------
# `caddy hash-password` emits bcrypt, and every bcrypt hash contains `$` (`$2a$14$…`). Compose
# interpolates `$` in values it reads from an env file, so pasting the hash straight in delivers a
# mangled hash to Caddy and every login fails with no useful error. Each `$` has to become `$$`.
# That is a five-second fix once you know and an hour when you do not, so it is automated.
#
# The plaintext is printed **once**, to stdout, and stored nowhere. Put it in a password manager
# before closing the terminal.
set -euo pipefail

if [ "${1:-}" = "--stdin" ]; then
  printf 'Password (not echoed): ' >&2
  read -rs PLAINTEXT
  printf '\n' >&2
else
  # 18 bytes of urlsafe base64 ≈ 143 bits. This gate keeps an unreviewed legal page off Google,
  # not a bank vault, but there is no reason to pick a weak one.
  PLAINTEXT="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
fi

[ -n "$PLAINTEXT" ] || { echo "empty password, refusing" >&2; exit 1; }

HASH="$(docker run --rm caddy:2.8-alpine caddy hash-password --plaintext "$PLAINTEXT")"
ESCAPED="${HASH//\$/\$\$}"

cat <<OUT

Add to .env.staging.compose (this file is gitignored — check before you save):

  BASKFY_GATE_USER=baskfy
  BASKFY_GATE_PASSWORD_HASH=${ESCAPED}

The password, shown once. Store it in a password manager now:

  ${PLAINTEXT}

OUT
