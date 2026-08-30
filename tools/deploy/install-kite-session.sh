#!/usr/bin/env bash
# Give the box its own key for borrowing the desk's Kite session, and print the PUBLIC half.
#
#     bash tools/deploy/install-kite-session.sh
#
# Then paste the public key into the desk's ~desk/.ssh/authorized_keys, bound to a forced command:
#
#     restrict,command="/home/desk/bin/emit-kite-token",from="<box ip>" <public key>
#
# WHY THE KEY IS GENERATED HERE AND NOT ON A LAPTOP
# ------------------------------------------------
# Because then there is nothing to transport. `box.sh` refuses secrets as arguments for a good
# reason — `ssm send-command` parameters live in CloudTrail — and every other channel (S3, a
# SecureString, a paste) leaves the private key in one more place than necessary. A key generated
# on the box and never copied has exactly one copy, on the machine that uses it. Only the public
# half travels, and a public key is public.
#
# What the key can do is bounded on the *desk* side, not here: sshd binds it to a forced command
# that prints one string. It is not a login on the trading box, and it cannot become one — even
# if this file's private key leaked, and even from the one IP it is pinned to.
set -euo pipefail

SECRETS_DIR=/opt/baskfy/secrets/ssh
KEY="$SECRETS_DIR/kite-session"
# uid/gid of the container's `baskfy` user (infra/docker/Dockerfile.python). ssh will not use a
# key it cannot read, and will not use one it considers world-readable, so both matter.
CONTAINER_UID=1001

sudo install -d -m 0700 -o "$CONTAINER_UID" -g "$CONTAINER_UID" "$SECRETS_DIR"

if sudo test -f "$KEY"; then
  echo "== key already present; reusing it (delete $KEY to rotate)"
else
  echo "== generating"
  # Generated as root and chowned, rather than `sudo -u '#1001'`: the container's user exists
  # only inside the image, so the host has no account to become.
  sudo ssh-keygen -t ed25519 -N '' -q \
    -C "baskfy-box kite-session pull (forced command only)" -f "$KEY"
fi
sudo chown "$CONTAINER_UID:$CONTAINER_UID" "$KEY" "$KEY.pub"
sudo chmod 0600 "$KEY"
sudo chmod 0644 "$KEY.pub"

# The desk's host key, pinned. Written here rather than learned on first connection: an
# unattended job has nobody to approve an unfamiliar host.
DESK_HOST="${DESK_HOST:-65.0.226.77}"
sudo bash -c "ssh-keyscan -t ecdsa '$DESK_HOST' 2>/dev/null > '$SECRETS_DIR/known_hosts'"
sudo chown "$CONTAINER_UID:$CONTAINER_UID" "$SECRETS_DIR/known_hosts"
sudo chmod 0644 "$SECRETS_DIR/known_hosts"

echo "== pinned host key"
ssh-keygen -lf "$SECRETS_DIR/known_hosts" | sed 's/^/   /'
echo "== public key — install this on the desk"
sudo cat "$KEY.pub"
