#!/usr/bin/env bash
# Create the integration branch and one worktree per lane for the Go rewrite
# (docs/go-rewrite/04-protocol.md). Idempotent: re-running adds only what is missing.
#
# Usage:  tools/rewrite/setup-worktrees.sh [BASE_BRANCH] [WT_ROOT]
#         BASE_BRANCH defaults to the current branch (expected: developer)
#         WT_ROOT     defaults to ../baskfy-wt
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

BASE=${1:-$(git branch --show-current)}
WT_ROOT=${2:-$(cd .. && pwd)/baskfy-wt}
LANES="0 1 2 3 4 5 6 7 8"

if [ -n "$(git status --porcelain)" ]; then
  echo "refusing: working tree is dirty. Commit or stash first (the lanes branch from a known commit)." >&2
  exit 1
fi

for tool in go; do command -v "$tool" >/dev/null || { echo "missing: $tool (brew install go)" >&2; exit 1; }; done
for tool in sqlc golangci-lint oapi-codegen; do
  command -v "$tool" >/dev/null || echo "warning: $tool not on PATH (L0 needs it; see docs/go-rewrite/README.md §How to start)" >&2
done

if ! git show-ref --verify --quiet refs/heads/go/main; then
  git branch go/main "$BASE"
  echo "created go/main from $BASE ($(git rev-parse --short "$BASE"))"
fi

mkdir -p "$WT_ROOT"
for n in $LANES; do
  br="go/L$n"; dir="$WT_ROOT/L$n"
  git show-ref --verify --quiet "refs/heads/$br" || git branch "$br" go/main
  if [ ! -d "$dir" ]; then
    git worktree add "$dir" "$br" >/dev/null
    echo "worktree $dir  ->  $br"
  else
    echo "exists   $dir"
  fi
done

# Each worktree gets the lane's prompt at hand and a DRY_RUN env, never a real one.
for n in $LANES; do
  dir="$WT_ROOT/L$n"
  printf 'DRY_RUN=true\nBASKFY_ENVIRONMENT=development\n' > "$dir/.env.go-rewrite"
done

cat <<MSG

Done. Base: $BASE @ $(git rev-parse --short go/main). Worktrees under $WT_ROOT.
Open one terminal per lane:
$(for n in $LANES; do echo "  cd $WT_ROOT/L$n && claude     # paste docs/go-rewrite/prompts/L$n.md"; done)
Fill the header of docs/go-rewrite/STATUS.md (T and base commit) in the L0 worktree.
MSG
