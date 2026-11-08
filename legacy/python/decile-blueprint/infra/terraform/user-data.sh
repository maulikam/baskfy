#!/usr/bin/env bash
# Bootstrap for the Phase A box. Runs once, on first boot.
#
# It installs Docker and the SSM agent and creates the deploy directory — and stops there. It does
# NOT clone the repository, write an env file, or start the stack, because all three need secrets,
# and user-data is readable by anything on the box that can reach the metadata service. The deploy
# itself is `docs/runbooks/07-deploy-phase-a.md` §4, run over an SSM session.
set -euxo pipefail

dnf update -y
dnf install -y docker git amazon-ssm-agent

systemctl enable --now amazon-ssm-agent

# Compose v2 as a CLI plugin — `docker compose`, not the retired `docker-compose` binary.
install -d -m 0755 /usr/libexec/docker/cli-plugins
COMPOSE_VERSION=v2.32.4
curl -fsSL -o /usr/libexec/docker/cli-plugins/docker-compose \
  "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-aarch64"
chmod +x /usr/libexec/docker/cli-plugins/docker-compose

systemctl enable --now docker
usermod -aG docker ec2-user

# Everything the deployment needs lives here: the compose file, the Caddyfile, the two env files.
install -d -o ec2-user -g ec2-user -m 0750 /opt/baskfy

# The IST clock, at the host level too. docs/08 §1: the nightly chain, the publish deadline and
# the alerts are all IST wall-clock tied to the NSE session. The containers set TZ themselves; a
# host on UTC makes every log line and every `docker ps` timestamp lie by five and a half hours,
# which is how a 3am incident gets diagnosed backwards.
timedatectl set-timezone Asia/Kolkata

# Docker's default json-file driver has no cap: on a box that runs a Celery worker chattering at
# INFO, the root volume fills in weeks and Postgres stops on a full disk.
cat > /etc/docker/daemon.json <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "5" }
}
JSON
systemctl restart docker

echo "bootstrap complete — deploy with docs/runbooks/07-deploy-phase-a.md" > /opt/baskfy/READY
