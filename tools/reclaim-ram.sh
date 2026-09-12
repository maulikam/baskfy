#!/usr/bin/env bash
#
# Find (and optionally kill) the test-runner and dev-server processes this repo leaves behind.
#
# Why this exists: a 16 GB machine running two vitest pools (one fork per core, jsdom in each),
# a `next start`, and the uvicorn servers Playwright's `webServer` block starts will sit at
# several GB of test infrastructure that nothing is waiting on. Every interrupted `pnpm run e2e`
# or Ctrl-C'd `vitest` can leave its children parented to launchd, where they never exit.
#
#   tools/reclaim-ram.sh          # list what is orphaned, kill nothing
#   tools/reclaim-ram.sh --kill   # SIGTERM them, then SIGKILL whatever survives 3s
#
# It only ever lists a process whose working directory is inside this repo, and never the
# claude CLI, VS Code, Docker, or this script's own ancestors.
#
# Note for anyone editing: the process scan writes to a temp file rather than a `$(...)` capture.
# macOS ships bash 3.2, which ends a command substitution at the first unbalanced `)` -- and a
# `case` pattern is exactly that.
set -uo pipefail

KILL=0
[ "${1:-}" = "--kill" ] && KILL=1

REPO=$(cd "$(dirname "$0")/.." && pwd)

# Two conditions, and a process must meet both.
#
# 1. Its command names a runner this repo uses. `.venv/bin/python` is in the list because
#    `make api` runs uvicorn with --reload, and the reloader's worker is a bare
#    `python -c "from multiprocessing.spawn import spawn_main ..."` that no uvicorn-shaped
#    pattern can see.
# 2. Its working directory is inside this repo -- checked with lsof, not guessed from the
#    command line, which is relative as often as not. This is what keeps the script from
#    reaping another project's vitest or a pytest you are running elsewhere.
PATTERNS='vitest|next-server|next/dist/bin/next|baskfy_api|uvicorn|playwright|pytest|\.venv/bin/python'

# Walk up from this script so we never kill the shell, the agent, or the terminal running us.
ANCESTORS=" "
p=$$
while [ -n "$p" ] && [ "$p" != "0" ] && [ "$p" != "1" ]; do
  ANCESTORS="$ANCESTORS$p "
  p=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
done

TMP=$(mktemp -t reclaim-ram)
trap 'rm -f "$TMP" "$TMP.raw"' EXIT

ps -eo pid=,rss=,etime=,command= \
  | grep -E "$PATTERNS" \
  | grep -v -E 'Visual Studio Code|claude --|Docker|reclaim-ram|grep -E' \
  > "$TMP.raw" 2>/dev/null || true

while read -r pid rss etime cmd; do
  [ -n "$pid" ] || continue
  case "$ANCESTORS" in (*" $pid "*) continue ;; esac

  cwd=$(lsof -a -d cwd -p "$pid" -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)
  case "$cwd" in
    ("$REPO"|"$REPO"/*) ;;
    (*)
      # No cwd (lsof declined, or the process is gone). Fall back to an absolute repo path in
      # the command line; if that is absent too, leave the process alone.
      case "$cmd" in (*"$REPO"*) ;; (*) continue ;; esac
      ;;
  esac

  printf '%s\t%s\t%s\t%s\n' "$pid" "$rss" "$etime" "$cmd" >> "$TMP"
done < "$TMP.raw"
rm -f "$TMP.raw"

if [ ! -s "$TMP" ]; then
  echo "Nothing orphaned."
  exit 0
fi

printf '%-7s %9s %9s  %s\n' PID RSS ELAPSED COMMAND
awk -F'\t' '{ printf "%-7s %8dM %9s  %.100s\n", $1, $2/1024, $3, $4; t+=$2; n++ }
            END { printf "\n%d processes, %d MB resident\n", n, t/1024 }' "$TMP"

if [ $KILL -eq 0 ]; then
  echo "Run with --kill to terminate them."
  exit 0
fi

pids=$(cut -f1 "$TMP" | tr '\n' ' ')
echo "SIGTERM ->$pids"
kill $pids 2>/dev/null
sleep 3
survivors=""
for pid in $pids; do
  if kill -0 "$pid" 2>/dev/null; then survivors="$survivors $pid"; fi
done
if [ -n "$survivors" ]; then
  echo "SIGKILL ->$survivors"
  kill -9 $survivors 2>/dev/null
fi
echo "Done."
