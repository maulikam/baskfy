#!/usr/bin/env bash
# The namespace check for the Baskfy rename (MERGE-PROMPTS.md M2; settled in
# docs/DECISIONS-MERGE.md M2.1).
#
# M2's acceptance was written as "zero occurrences of decile_, DECILE_ or @decile/ outside
# docs/". Taken literally that is unachievable and wrong, because `decile` is this product's
# domain vocabulary as well as its former brand -- a decile is a statistical bucket, and
# CLAUDE.md D1 keeps that vocabulary deliberately. A blanket rename corrupted a public API
# contract, a code constant and a blog post before it was caught.
#
# So the rule is token-scoped, not prefix-scoped: no *namespace* token may survive in code,
# and every exception below is named, justified and finite. This script is the check M21's
# final sweep runs -- not the raw grep -- so the same rule is applied every time by the same
# code, rather than re-argued from memory.
#
# Usage:  tools/check-namespace.sh          (from the repository root)
# Exit:   0 clean, 1 violations found (printed, one per line, with file and line).

set -uo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 2

# --- Paths that are records, not code -------------------------------------------------
# Both trees' docs/ stay verbatim (M2 step 4), and the two documents that *instruct* the
# rename cannot be subject to it: rewriting them would make each read
# "rename baskfy_core -> baskfy_core".
EXCLUDES=(
  ':!docs/*'
  ':!*/docs/*'
  ':!MERGE-PROMPTS.md'
  ':!decile-blueprint/PROMPTS.md'
  ':!tools/check-namespace.sh'
)

# --- Tokens that are vocabulary, not namespace ----------------------------------------
#   decile_1..decile_6   the D1-D6 values of apply_filters_on. A PUBLIC API CONTRACT:
#                        openapi.json, the generated TS client, the JSON Schema, URL state
#                        and seed data. Renaming them changes every saved screen and every
#                        stored URL. CLAUDE.md D1 keeps this vocabulary.
#   DECILE_RANK_KEY      baskfy_core.universes: the key deciles are ranked by
#                        (= "marketcap_cr"). A code constant, never an environment variable.
#                        docs/06 defines it.
#   decile_bucket        as screen_run_decile_bucket, a query-plan name; `decile` is the
#                        statistic being bucketed.
ALLOWED='^(decile_[1-6]|decile_bucket|DECILE_RANK_KEY)$'

violations=0
while IFS= read -r line; do
  [ -n "$line" ] || continue
  file=${line%%:*}
  rest=${line#*:}
  lineno=${rest%%:*}
  # Pull every candidate token out of the matched line and test each one.
  while IFS= read -r tok; do
    [ -n "$tok" ] || continue
    if ! printf '%s' "$tok" | grep -qE "$ALLOWED"; then
      printf '%s:%s: %s\n' "$file" "$lineno" "$tok"
      violations=$((violations + 1))
    fi
  done < <(printf '%s' "${rest#*:}" | grep -oE '(decile_[A-Za-z0-9_]+|DECILE_[A-Z0-9_]+|@decile/[a-z-]+)' | sort -u)
done < <(git grep -I -n -E '(decile_[A-Za-z0-9_]+|DECILE_[A-Z0-9_]+|@decile/[a-z-]+)' -- "${EXCLUDES[@]}" 2>/dev/null)

if [ "$violations" -ne 0 ]; then
  echo
  echo "FAIL: $violations namespace token(s) survived the rename."
  echo "If one of them is genuinely vocabulary, add it to ALLOWED above WITH a reason —"
  echo "and record it in docs/DECISIONS-MERGE.md. Never widen the pattern to silence a hit."
  exit 1
fi

echo "OK: no namespace tokens outside the named exceptions (docs/, the two instruction"
echo "    documents, decile_1..6, DECILE_RANK_KEY, decile_bucket)."
