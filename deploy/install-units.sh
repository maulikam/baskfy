#!/usr/bin/env bash
# Build the environment and install the schedule.
#
#   ssh <admin-user>@<host> 'cd /home/desk/kite-momentum-rebalancer && ./deploy/install-units.sh'
#
# RUN AS THE ADMIN USER (ubuntu), NOT AS desk. The account that runs an order-placing
# application has no business holding root, so `desk` is deliberately not a sudoer. This
# script does the two kinds of work under the two identities that should own them:
# building the venv and the stylesheet as desk, installing units as root.
set -euo pipefail
cd "$(dirname "$0")/.."
APP_DIR="$(pwd)"
APP_USER="${APP_USER:-desk}"

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this as the admin user (it sudoes where it needs to), not as root." >&2
  exit 1
fi
if ! sudo -n true 2>/dev/null; then
  echo "This user cannot sudo. Run it as the admin account (ubuntu), not as ${APP_USER}." >&2
  exit 1
fi

echo "== python environment (as ${APP_USER})"
# --clear so a re-run rebuilds rather than failing on an existing venv: this script
# has to be idempotent, because it is what you run after every code change.
# requirements.txt is the manifest — there is no pyproject, so `uv run` has nothing to
# resolve. The venv interpreter is invoked directly, exactly as the launchd jobs did.
sudo -u "$APP_USER" bash -lc "cd '$APP_DIR' && export PATH=\$HOME/.local/bin:\$PATH && \
  uv venv --clear --python 3.12 && uv pip install -q -r requirements.txt"

echo "== stylesheet (as ${APP_USER})"
sudo -u "$APP_USER" bash -lc "cd '$APP_DIR' && (npm ci --silent || npm install --silent) && npm run css"

echo "== logs"
sudo -u "$APP_USER" mkdir -p "/home/${APP_USER}/logs"

echo "== units"
sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now momentum-web.service
sudo systemctl enable --now momentum-daily.timer
sudo systemctl enable --now momentum-backup.timer
for i in nifty banknifty sensex; do
  sudo systemctl enable --now "strangle-collect@${i}.timer"
done

echo "== state"
systemctl is-active momentum-web.service | sed 's/^/   web: /'
systemctl list-timers --all 'momentum-*' 'strangle-*' --no-pager | sed 's/^/   /'
