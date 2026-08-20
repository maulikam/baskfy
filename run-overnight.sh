#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# run-overnight.sh — drive Claude Code through PROMPTS.md modules unattended.
#
#   ./run-overnight.sh            # runs prompts 13..21
#   ./run-overnight.sh 13 20      # runs prompts 13..20  (recommended tonight)
#   MODEL=opus ./run-overnight.sh 13 20
#
# One FRESH claude session per module — never one long session, because context
# rot is exactly what makes module 20 quietly contradict module 5.
#
# Verification is done by THIS script running your own test suite. It does not
# trust the model's self-report, because a model marking its own homework is
# the single most likely way you wake up to something that looks finished.
# ---------------------------------------------------------------------------
set -o pipefail

START="${1:-13}"
END="${2:-21}"
TIMEOUT_MIN="${TIMEOUT_MIN:-90}"          # hard cap per claude invocation
MAX_CONSECUTIVE_RED="${MAX_CONSECUTIVE_RED:-2}"
MODEL="${MODEL:-}"                        # e.g. MODEL=opus

command -v claude >/dev/null || { echo "FATAL: 'claude' not on PATH"; exit 1; }
command -v jq     >/dev/null || { echo "FATAL: 'jq' required (apt/brew install jq)"; exit 1; }

# GNU timeout is 'timeout' on Linux, 'gtimeout' on macOS (brew install coreutils)
if command -v timeout  >/dev/null; then TIMEOUT_BIN=timeout
elif command -v gtimeout >/dev/null; then TIMEOUT_BIN=gtimeout
else TIMEOUT_BIN=""; echo "WARN: no timeout binary — modules will run uncapped"; fi

REPO="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "FATAL: not a git repo"; exit 1; }
cd "$REPO" || exit 1

if [ -n "$(git status --porcelain)" ]; then
  echo "FATAL: working tree is dirty. Commit or stash Prompt 12's work first."
  git status --short
  exit 1
fi

OUT="$REPO/.overnight"; mkdir -p "$OUT"
REPORT="$OUT/REPORT.md"
BRANCH="overnight-$(date +%Y%m%d-%H%M)"
TOTAL_COST=0
TEST_RC=1; LINT_RC=1

git checkout -b "$BRANCH" >/dev/null 2>&1 || { echo "FATAL: could not create $BRANCH"; exit 1; }

{
  echo "# Overnight run — $(date '+%Y-%m-%d %H:%M %Z')"
  echo
  echo "Branch: \`$BRANCH\` · Modules: $START..$END · Timeout/module: ${TIMEOUT_MIN}m"
  echo
  echo "| # | Build | make test | make lint | Duration | Cost (USD est.) |"
  echo "|---|---|---|---|---|---|"
} > "$REPORT"

# --- rules injected into every session -------------------------------------
read -r -d '' UNATTENDED_RULES <<'EOS'
You are running UNATTENDED overnight. No human is available. Follow these rules absolutely.

1. Never ask a question and never wait for input. If a decision is genuinely ambiguous, choose the
   option most consistent with docs/, append one line explaining the choice to docs/DECISIONS.md,
   and continue.
2. Never weaken, skip, xfail, delete, or loosen the tolerance of a test to make it pass. If you
   cannot make something pass, LEAVE IT FAILING and append the reason to .overnight/BLOCKERS.md.
   A red test I can see is worth far more than a green test that lies.
3. Never edit anything under docs/ except appending to docs/DECISIONS.md. The specs are the source
   of truth, not something to adjust when the code disagrees with them.
4. Never run destructive git commands: no reset --hard, no clean -fd, no rebase, no branch switch,
   no branch deletion, no force anything, no push to any remote. Commit to the current branch only.
5. Never contact real external services: no live API keys, no real payment endpoints, no deploys to
   real infrastructure, no DNS or domain changes, no emails to real addresses. Test/sandbox
   credentials and local containers only.
6. Do not modify anything in .overnight/ except the files you are explicitly told to write.
7. Stay strictly inside the module you were given. Do not start the next prompt.
EOS

run_claude () {  # $1=tag  $2=prompt   -> returns claude's exit code
  tag="$1"; prompt="$2"
  json="$OUT/$tag.json"

  if [ -n "$MODEL" ]; then
    if [ -n "$TIMEOUT_BIN" ]; then
      "$TIMEOUT_BIN" "${TIMEOUT_MIN}m" claude -p "$prompt" --model "$MODEL" \
        --permission-mode bypassPermissions --append-system-prompt "$UNATTENDED_RULES" \
        --output-format json > "$json" 2> "$OUT/$tag.stderr"
    else
      claude -p "$prompt" --model "$MODEL" \
        --permission-mode bypassPermissions --append-system-prompt "$UNATTENDED_RULES" \
        --output-format json > "$json" 2> "$OUT/$tag.stderr"
    fi
  else
    if [ -n "$TIMEOUT_BIN" ]; then
      "$TIMEOUT_BIN" "${TIMEOUT_MIN}m" claude -p "$prompt" \
        --permission-mode bypassPermissions --append-system-prompt "$UNATTENDED_RULES" \
        --output-format json > "$json" 2> "$OUT/$tag.stderr"
    else
      claude -p "$prompt" \
        --permission-mode bypassPermissions --append-system-prompt "$UNATTENDED_RULES" \
        --output-format json > "$json" 2> "$OUT/$tag.stderr"
    fi
  fi
  rc=$?

  cost=$(jq -r '.total_cost_usd // 0' "$json" 2>/dev/null || echo 0)
  TOTAL_COST=$(awk -v a="$TOTAL_COST" -v b="$cost" 'BEGIN{printf "%.4f", a+b}')
  jq -r '.result // "(no result field — see stderr)"' "$json" > "$OUT/$tag.txt" 2>/dev/null \
    || cp "$json" "$OUT/$tag.txt"
  return $rc
}

verify () {  # $1=module number ; returns 0 if green, sets TEST_RC / LINT_RC
  n="$1"; log="$OUT/verify-$n.log"
  echo "=== make test ===" > "$log"
  make test >> "$log" 2>&1; TEST_RC=$?
  echo "=== make lint ===" >> "$log"
  make lint >> "$log" 2>&1; LINT_RC=$?
  echo "TEST_RC=$TEST_RC LINT_RC=$LINT_RC" >> "$log"
  [ "$TEST_RC" -eq 0 ] && [ "$LINT_RC" -eq 0 ]
}

consecutive_red=0
N=$START
while [ "$N" -le "$END" ]; do
  echo
  echo "======== Prompt $N — $(date '+%H:%M:%S') ========"
  t0=$SECONDS

  BUILD_PROMPT="Execute Prompt $N from PROMPTS.md in this repo.

First read CLAUDE.md and the docs/ files that Prompt $N references. The docs/ folder is the source
of truth — if your instinct conflicts with it, follow the docs and record the disagreement in
docs/DECISIONS.md. Stay within the locked stack in docs/02-tech-stack-adr.md.

Work through that prompt's deliverables in order. Then run its acceptance criteria and report
pass/fail for each one honestly.

Finish by writing .overnight/module-$N.md containing: what you built, every acceptance criterion
with its status, every decision you made under ambiguity, and anything a human must look at in the
morning. Be blunt about what does not work."

  if run_claude "build-$N" "$BUILD_PROMPT"; then BUILD="ok"; else BUILD="rc=$?"; fi

  if verify "$N"; then
    STATUS="green"
  else
    echo "  -> verification failed (test=$TEST_RC lint=$LINT_RC) — one repair attempt"
    REPAIR_PROMPT="Prompt $N was just executed in this repo and verification FAILED.

Read .overnight/verify-$N.log for the exact failures, then fix them.

Do NOT weaken, skip or delete tests. Do NOT edit docs/ specs. Do NOT lower a tolerance to make a
parity or golden test pass. If a failure reveals a genuine problem in the specification rather than
in the code, leave the test failing and write your analysis to .overnight/BLOCKERS.md instead.

When done, re-run the test suite and report what is still failing."
    run_claude "repair-$N" "$REPAIR_PROMPT"
    if verify "$N"; then STATUS="green (after repair)"; else STATUS="RED"; fi
  fi

  dur=$(( SECONDS - t0 ))
  cb=$(jq -r '.total_cost_usd // 0' "$OUT/build-$N.json"  2>/dev/null || echo 0)
  cr=$(jq -r '.total_cost_usd // 0' "$OUT/repair-$N.json" 2>/dev/null || echo 0)
  cost_n=$(awk -v a="$cb" -v b="$cr" 'BEGIN{printf "%.2f", a+b}')

  git add -A
  git commit -q -m "module $N: $STATUS" --allow-empty
  case "$STATUS" in
    RED) git tag -f "module-$N-red"   >/dev/null 2>&1 ;;
    *)   git tag -f "module-$N-green" >/dev/null 2>&1 ;;
  esac

  t_txt=FAIL; [ "$TEST_RC" -eq 0 ] && t_txt=pass
  l_txt=FAIL; [ "$LINT_RC" -eq 0 ] && l_txt=pass
  printf "| %d | %s | %s | %s | %dm%02ds | %s |\n" \
    "$N" "$BUILD" "$t_txt" "$l_txt" $((dur/60)) $((dur%60)) "$cost_n" >> "$REPORT"

  echo "  -> $STATUS in $((dur/60))m — \$$cost_n"

  if [ "$STATUS" = "RED" ]; then
    consecutive_red=$(( consecutive_red + 1 ))
    if [ "$consecutive_red" -ge "$MAX_CONSECUTIVE_RED" ]; then
      echo
      echo "ABORTING: $consecutive_red consecutive red modules. Building further on this is waste."
      { echo; echo "**ABORTED after Prompt $N** — $consecutive_red consecutive red modules."; } >> "$REPORT"
      break
    fi
  else
    consecutive_red=0
  fi

  N=$(( N + 1 ))
done

{
  echo
  echo "## Totals"
  echo
  echo "- Finished: $(date '+%Y-%m-%d %H:%M %Z')"
  echo "- Estimated cost: **\$$TOTAL_COST**"
  echo "- Branch: \`$BRANCH\`"
  echo
  echo "## Read these first"
  echo
  echo '```'
  echo "cat .overnight/BLOCKERS.md        # anything it could not honestly make pass"
  echo "cat docs/DECISIONS.md             # every judgement call made while you slept"
  echo "ls  .overnight/module-*.md        # per-module self-reports"
  echo "git log --oneline $BRANCH         # one commit per module"
  echo '```'
} >> "$REPORT"

echo
echo "======== DONE ========"
cat "$REPORT"