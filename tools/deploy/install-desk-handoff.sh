#!/usr/bin/env bash
# The REVERSE leg of the Kite session bridge: give the box its own key for handing the desk a
# request_token, and print the PUBLIC half.
#
#     AWS_PROFILE=baskfy-poc bash tools/deploy/install-desk-handoff.sh
#
# Then paste the public key into the desk's ~desk/.ssh/authorized_keys, bound to a forced command:
#
#     restrict,command="/home/desk/bin/accept-kite-request-token",from="<box ip>" <public key>
#
# WHY A SECOND KEY AND NOT A SECOND VERB ON THE FIRST
# ---------------------------------------------------
# `install-kite-session.sh` already put a key on this box, bound on the desk to
# `emit-kite-token`. Teaching that one script two verbs would make the existing key's capability
# "read the access token OR make the desk log in", and would put the choice in the client's hands
# via SSH_ORIGINAL_COMMAND — which is precisely the property M58's design exists to deny. Two
# keys, two forced commands, two capabilities, revoked independently by deleting one line each.
#
# Everything `install-kite-session.sh` says about generating on the box applies verbatim: the key
# is generated here, only the public half travels, and there is deliberately no step that copies
# a private key anywhere.
#
# WHAT THE KEY CAN DO IS BOUNDED ON THE DESK, NOT HERE
# ----------------------------------------------------
# `tools/deploy/desk/accept-kite-request-token` reads at most 512 bytes of stdin, accepts only
# `[A-Za-z0-9]{8,64}`, refuses a `sim_` stub, rate-limits itself, and drives exactly one
# hard-coded loopback URL — the desk's own `/callback`. It writes no file the desk trades on and
# cannot read the access token back out. A request_token is single-use and worth nothing once
# redeemed, so this key is a strictly *narrower* capability than the pull key beside it.
set -euo pipefail

SECRETS_DIR=/opt/baskfy/secrets/ssh
KEY="$SECRETS_DIR/desk-handoff"
BIN_DIR=/opt/baskfy/bin
# uid/gid of the container's `baskfy` user (infra/docker/Dockerfile.python). The api container
# gets $SECRETS_DIR mounted read-only, and ssh refuses a key it cannot read.
CONTAINER_UID=1001

sudo install -d -m 0700 -o "$CONTAINER_UID" -g "$CONTAINER_UID" "$SECRETS_DIR"
sudo install -d -m 0755 -o root -g root "$BIN_DIR"

if sudo test -f "$KEY"; then
  echo "== key already present; reusing it (delete $KEY to rotate)"
else
  echo "== generating"
  sudo ssh-keygen -t ed25519 -N '' -q \
    -C "baskfy-box kite request_token handoff (forced command only)" -f "$KEY"
fi
sudo chown "$CONTAINER_UID:$CONTAINER_UID" "$KEY" "$KEY.pub"
sudo chmod 0600 "$KEY"
sudo chmod 0644 "$KEY.pub"

# The desk's host key. Shared with the forward leg — it is the same host, and pinning it twice
# would be two places to update when it rotates.
DESK_HOST="${DESK_HOST:-65.0.226.77}"
if ! sudo test -s "$SECRETS_DIR/known_hosts"; then
  sudo bash -c "ssh-keyscan -t ecdsa '$DESK_HOST' 2>/dev/null > '$SECRETS_DIR/known_hosts'"
  sudo chown "$CONTAINER_UID:$CONTAINER_UID" "$SECRETS_DIR/known_hosts"
  sudo chmod 0644 "$SECRETS_DIR/known_hosts"
fi

echo "== pinned host key"
ssh-keygen -lf "$SECRETS_DIR/known_hosts" | sed 's/^/   /'
echo "== public key — install this on the desk, bound to accept-kite-request-token"
sudo cat "$KEY.pub"
