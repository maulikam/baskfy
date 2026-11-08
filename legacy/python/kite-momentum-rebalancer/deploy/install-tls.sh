#!/usr/bin/env bash
# Put the desk behind HTTPS on a real hostname, and rate-limit password guessing.
#
# Run as a sudo-capable user on the box:  bash deploy/install-tls.sh
#
# WHAT THIS DOES NOT CHANGE: the app still listens on 127.0.0.1:8420 and is still
# unreachable from the internet. Caddy is the only process on a public port. If Caddy
# stops, the desk becomes unreachable rather than reachable-without-TLS.
#
# PREREQUISITES, both outside this script and both silent failures if missed:
#   1. An A record for $DESK_HOST pointing at this box, resolvable from PUBLIC DNS.
#      Let's Encrypt queries public resolvers, not your registrar, so a record that
#      works at the registrar but is not yet delegated will fail validation.
#   2. Ports 80 AND 443 open in the CLOUD firewall as well as ufw. Port 80 is not
#      optional: it carries the ACME HTTP-01 challenge.
set -euo pipefail

DESK_HOST="${DESK_HOST:-desk.modelbasket.in}"

command -v caddy >/dev/null 2>&1 || {
  sudo apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq caddy
}

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
sudo install -m 644 "$here/caddy/Caddyfile" /etc/caddy/Caddyfile
sudo sed -i "s/desk\.modelbasket\.in/$DESK_HOST/" /etc/caddy/Caddyfile

# The log file must be writable by the caddy user. Validating the config under sudo
# creates it as root:root 0600 first, after which caddy cannot open its own log and the
# service fails to start with a bare "permission denied" that names no cause.
sudo mkdir -p /var/log/caddy
sudo touch /var/log/caddy/desk.log
sudo chown -R caddy:caddy /var/log/caddy
sudo chmod 750 /var/log/caddy

sudo ufw allow 80/tcp  >/dev/null
sudo ufw allow 443/tcp >/dev/null

sudo install -m 644 "$here/fail2ban/filter.d/caddy-desk-auth.conf" \
                    /etc/fail2ban/filter.d/caddy-desk-auth.conf
sudo install -m 644 "$here/fail2ban/jail.d/caddy-desk.conf" \
                    /etc/fail2ban/jail.d/caddy-desk.conf

sudo systemctl enable --now caddy
sudo systemctl restart caddy
sudo systemctl restart fail2ban

echo "waiting for the certificate..."
for _ in $(seq 1 30); do
  sleep 3
  if sudo journalctl -u caddy --since '-3min' --no-pager | grep -q 'certificate obtained successfully'; then
    echo "certificate obtained for $DESK_HOST"; break
  fi
  systemctl is-active --quiet caddy || { echo "caddy failed:"; sudo journalctl -u caddy -n 20 --no-pager; exit 1; }
done

# Prove the jail reads the file. It reports itself enabled either way, so the only
# meaningful check is whether it is monitoring anything.
sudo fail2ban-client get caddy-desk-auth logpath

echo
echo "Remaining, by hand:"
echo "  - set DESK_ALLOWED_HOSTS=$DESK_HOST,127.0.0.1,localhost in .env, restart momentum-web"
echo "  - set DESK_PASSWORD in .env if it is still empty"
echo "  - register https://$DESK_HOST/callback in the Kite developer console"
