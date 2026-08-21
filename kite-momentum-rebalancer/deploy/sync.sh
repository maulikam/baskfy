#!/usr/bin/env bash
# Copy the repo and its state to the box. Run from the laptop, from the repo root.
#
#   ./deploy/sync.sh desk@1.2.3.4
#
# Code goes every time. STATE — the database, the journals, the token, .env — is copied
# only when it is missing on the far side, because after the first cutover the box is the
# one making the record and the laptop's copy is a stale fork. Pass --force-state to
# overwrite deliberately.
set -euo pipefail
TARGET="${1:?usage: deploy/sync.sh user@host [--force-state]}"
FORCE="${2:-}"
REMOTE="kite-momentum-rebalancer"

echo "== code"
rsync -az --delete \
  --exclude '.venv/' --exclude 'node_modules/' --exclude '__pycache__/' \
  --exclude '.git/' --exclude 'data/' --exclude '.env' \
  ./ "${TARGET}:${REMOTE}/"

echo "== state"
if [ "$FORCE" = "--force-state" ]; then
  echo "   forcing: the box's data/ and .env will be overwritten"
  rsync -az data/ "${TARGET}:${REMOTE}/data/"
  rsync -az .env  "${TARGET}:${REMOTE}/.env"
else
  # --ignore-existing: never clobber a record the box has been keeping.
  rsync -az --ignore-existing data/ "${TARGET}:${REMOTE}/data/"
  rsync -az --ignore-existing .env  "${TARGET}:${REMOTE}/.env"
fi

echo "== done"
cat <<NEXT
   ssh ${TARGET} 'cd ${REMOTE} && ./deploy/install-units.sh'
   ssh -L 8420:127.0.0.1:8420 ${TARGET}      # then open http://127.0.0.1:8420
NEXT
