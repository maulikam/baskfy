#!/usr/bin/env bash
# Provision a fresh Ubuntu box to run the desk. Idempotent: safe to re-run.
#
#   ssh ubuntu@<ip> 'bash -s' < deploy/bootstrap.sh
#
# WHAT THIS BOX IS FOR. Two things, and the second is why it is worth the money:
#   1. a STATIC outbound IP, so Kite's order allowlist stops expiring
#   2. a machine that is always awake, so the 09:20 and 18:30 jobs actually run.
#      Three collection days were lost this week to a sleeping laptop, and a missed
#      session is not recoverable: kc.margins() has no history and /trades is flushed
#      nightly.
set -euo pipefail

APP_USER="${APP_USER:-desk}"
APP_DIR="/home/${APP_USER}/kite-momentum-rebalancer"

say() { printf "\n\033[1m== %s\033[0m\n" "$*"; }

say "timezone"
# Every schedule in this system is written in IST. A box on UTC would run the morning
# collect at 14:50 local and record a straddle five hours into the session.
sudo timedatectl set-timezone Asia/Kolkata
timedatectl | sed 's/^/   /'

say "packages"
sudo apt-get update -qq
sudo apt-get install -y -qq git curl rsync sqlite3 ca-certificates build-essential

say "application user"
id -u "$APP_USER" >/dev/null 2>&1 || sudo adduser --disabled-password --gecos "" "$APP_USER"

say "node (for the Claude CLI and the stylesheet build)"
if ! command -v node >/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
  sudo apt-get install -y -qq nodejs
fi
node --version | sed 's/^/   node /'

say "uv (python toolchain, same as the laptop)"
sudo -u "$APP_USER" bash -lc '
  command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh'

say "claude code cli"
# Installed globally so both you and the desk user can run it. Authentication is
# interactive and per-user: run `claude` once over SSH and follow the login URL.
sudo npm install -g @anthropic-ai/claude-code >/dev/null 2>&1 || \
  sudo npm install -g @anthropic-ai/claude-code
claude --version 2>/dev/null | sed 's/^/   claude /' || echo "   claude installed (run 'claude' to sign in)"

say "directories"
sudo -u "$APP_USER" mkdir -p "$APP_DIR" "/home/${APP_USER}/logs"

say "firewall"
# SSH ONLY. The web interface places real orders and must never be reachable from the
# internet — you reach it through an SSH tunnel, so it binds to loopback and nothing else.
sudo ufw --force reset >/dev/null
sudo ufw default deny incoming >/dev/null
sudo ufw default allow outgoing >/dev/null
sudo ufw allow OpenSSH >/dev/null
sudo ufw --force enable >/dev/null
sudo ufw status | sed 's/^/   /'

say "done"
cat <<'NEXT'
   Next, from the laptop:
     ./deploy/sync.sh <user>@<ip>          copy the repo, data and .env
     ssh <user>@<ip> 'cd kite-momentum-rebalancer && ./deploy/install-units.sh'
NEXT
