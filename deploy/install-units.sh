#!/usr/bin/env bash
# Install and start the schedule. Run ON the box, from the repo root.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== python environment"
# requirements.txt is the manifest here — there is no pyproject, so `uv run` has nothing
# to resolve. The venv interpreter is invoked directly, exactly as the launchd jobs did.
export PATH="$HOME/.local/bin:$PATH"
uv venv --python 3.12
uv pip install -r requirements.txt

echo "== stylesheet"
npm ci --silent 2>/dev/null || npm install --silent
npm run css

echo "== units"
sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now momentum-web.service
sudo systemctl enable --now momentum-daily.timer
for i in nifty banknifty sensex; do
  sudo systemctl enable --now "strangle-collect@${i}.timer"
done

echo "== state"
systemctl is-active momentum-web.service | sed 's/^/   web: /'
systemctl list-timers --all 'momentum-*' 'strangle-*' --no-pager | sed 's/^/   /'
