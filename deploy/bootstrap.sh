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

say "ssh hardening"
# The only port open to the internet is this one, and it now guards an account that can
# place orders. Keys only, no root, and a short grace window so a half-open connection
# cannot be parked.
sudo install -d -m 0755 /etc/ssh/sshd_config.d
sudo tee /etc/ssh/sshd_config.d/10-desk.conf >/dev/null <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
LoginGraceTime 30
MaxAuthTries 3
X11Forwarding no
AllowAgentForwarding no
CONF
# Refuse to lock the door with the key inside: if no authorized_keys exists, leave
# password auth alone and say so, rather than producing a box nobody can log into.
if sudo test -s "/home/${APP_USER}/.ssh/authorized_keys" || sudo test -s ~/.ssh/authorized_keys; then
  sudo sshd -t && sudo systemctl reload ssh && echo "   keys only, root login disabled"
else
  sudo rm -f /etc/ssh/sshd_config.d/10-desk.conf
  echo "   SKIPPED: no authorized_keys found. Install your key first, then re-run,"
  echo "            or you will lock yourself out."
fi

say "patching and brute-force protection"
sudo apt-get install -y -qq unattended-upgrades fail2ban
sudo systemctl enable --now fail2ban >/dev/null 2>&1 || true
sudo dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null 2>&1 || true
systemctl is-active fail2ban | sed 's/^/   fail2ban: /'

say "secret file permissions"
# The app tightens these at startup too; doing it here means they are never briefly
# world-readable on a fresh box.
sudo -u "$APP_USER" bash -lc "
  cd '$APP_DIR' 2>/dev/null || exit 0
  chmod 700 . data 2>/dev/null || true
  chmod 600 .env data/.kite_token.json 2>/dev/null || true"
echo "   done"

say "done"
cat <<'NEXT'
   Next, from the laptop:
     ./deploy/sync.sh <user>@<ip>          copy the repo, data and .env
     ssh <user>@<ip> 'cd kite-momentum-rebalancer && ./deploy/install-units.sh'
NEXT
