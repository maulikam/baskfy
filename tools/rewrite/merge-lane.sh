#!/usr/bin/env bash
# Merge one lane's branch into go/main, keeping go/main green (docs/go-rewrite/04-protocol.md).
# Run from the L0 worktree (which has go/main checked out) at each gate, dependencies first:
#   L1 L4 L5 L2 L3 L6 L7 L8
#
# Usage:  tools/rewrite/merge-lane.sh L<n> [--no-verify]
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
LANE=${1:?lane, e.g. L3}; VERIFY=${2:-}
BR="go/$LANE"

[ "$(git branch --show-current)" = "go/main" ] || { echo "run this from the worktree that has go/main checked out" >&2; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "go/main worktree is dirty; commit or stash first" >&2; exit 1; }
git show-ref --verify --quiet "refs/heads/$BR" || { echo "no branch $BR" >&2; exit 1; }

# The Python trees must be byte-identical on both sides — the rewrite never edits them.
if ! git diff --quiet go/main "$BR" -- decile-blueprint kite-momentum-rebalancer frozen; then
  echo "REFUSED: $BR changes files under the Python trees:" >&2
  git diff --stat go/main "$BR" -- decile-blueprint kite-momentum-rebalancer frozen >&2
  exit 2
fi

before=$(git rev-parse HEAD)
if ! git merge --no-ff -m "merge $BR into go/main" "$BR"; then
  echo "conflict merging $BR. If it is under go/internal/api/server or go/internal/db/gen, regenerate (make -C go oapi sqlc), 'git add', 'git commit'. Otherwise abort with: git merge --abort" >&2
  exit 3
fi

if [ "$VERIFY" != "--no-verify" ] && [ -f go/go.mod ]; then
  if ! (cd go && make build && make test && make lint); then
    echo "go/main went red after merging $BR — reverting the merge and reporting in STATUS.md is the rule." >&2
    git reset --hard "$before"
    exit 4
  fi
fi
echo "merged $BR -> go/main @ $(git rev-parse --short HEAD); go/main green"
