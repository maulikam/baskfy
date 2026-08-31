#!/usr/bin/env bash
# The DRY_RUN Friday drill, in one command (leaf 1.2.3).
#
# Drives a whole rebalance session — plan → orders → fills → GTT stops → cancel → replay —
# through Baskfy's own `packages/execution` gateway, with a broker that counts every attempt
# and then refuses. Nothing here can place an order:
#
#   * DRY_RUN is exported true and the drill refuses to start if it is ever set otherwise;
#   * the gates callable the drill hands the gateway returns dry_run=True unconditionally, so
#     the environment variable can only ever make it *safer*, never live;
#   * the only broker object in the process is `SpyBroker`, which has no network.
#
# Usage:
#   tools/friday-drill.sh                       run the drill
#   tools/friday-drill.sh --mutate skip-a-stop  inject one defect and watch an assertion bite
#
# Exit code: 0 when every check passed, 1 when any check failed. Known-absent enforcement is
# reported as an OPEN gap and deliberately does NOT fail the run — see the drill's docstring.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(dirname "$here")"

export DRY_RUN=true
cd "$repo/decile-blueprint"
exec uv run python "$here/friday-drill.py" --drill "$@"
