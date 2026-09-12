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
  # FINAL-REPORT.md is the run's historical record. It has to be able to say what was renamed,
  # and rewording "`SITE_NAME` was \"Decile\"" to satisfy a checker would be the checker editing
  # history -- the same argument that allowlists CLAUDE.md D1's sentence below. Deliberately just
  # this file: RUN-AND-TEST.md and NEEDS-MAULIK.md are live operator documents and stay in scope,
  # because if either of them ever says the old name, that IS a bug.
  ':!FINAL-REPORT.md'
  # frozen/ is outside every gate (CLAUDE.md safety rails, frozen/strangle/README.md).
  # A namespace sweep that "fixed" a file under here would be exactly the edit that is
  # forbidden, so the check does not look.
  ':!frozen/*'
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
#   decile_decile        the Docker Compose volume `decile_decile-pgdata` -- project name plus
#                        volume name, from the stack that existed before the rename. It appears
#                        once, in REMAINING.md's record of what was deleted from Docker on
#                        12 Sep 2026. It is the name of a thing that existed, not a namespace
#                        this repo uses, and rewording it would make the record wrong about
#                        which volume was removed. Same argument as the FINAL-REPORT.md
#                        exclusion above, but scoped to the token rather than the file: if
#                        REMAINING.md ever says `decile_core`, that is still a bug and still
#                        fails. Added 12 Sep 2026; docs/DECISIONS-MERGE.md M2.1.
ALLOWED='^(decile_[1-6]|decile_bucket|DECILE_RANK_KEY|decile_decile)$'

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

# --- The BRAND word, which is a different check and needed its own -------------------
#
# M25, 22 Aug 2026. The check above is token-scoped on purpose: `decile` is domain vocabulary
# and a blanket rename corrupted a public API contract, a code constant and a blog post before
# it was caught (M2.1). But nothing looked for the capitalised BRAND word, so
# `SITE_NAME = "Decile"` sat in the web app for a day after D1 renamed the product — and the
# app faithfully rendered the old name in 35 user-visible strings, its outbound email, its
# invoices and four legal documents.
#
# Two different failures need two different checks. This one looks for `Decile` as a name.
#
#   what-a-decile-actually-measures   a blog post about the STATISTIC, not the brand. Its
#   WhatADecileMeasures               title, slug and import identifier are deliberately kept
#                                     (docs/14 §"The brand word is not the vocabulary word").
#   Decile's product vocabulary       CLAUDE.md D1's own sentence, which names the former brand
#                                     in order to say its vocabulary is kept. Rewriting a
#                                     decision record to satisfy a checker would be the checker
#                                     editing history. Narrow on purpose: the exact phrase.
#   Decile bucketing                  the STATISTICAL OPERATION at the start of a sentence or a
#                                     bullet, which is the only reason it is capitalised:
#                                     "rank the universe by market cap, apply filters only
#                                     within D1-D6". CLAUDE.md D1 keeps this vocabulary, and
#                                     lower-cased `decile bucketing` has never been a hit. Two
#                                     occurrences, NEEDS-MAULIK.md and PITCH-SOURCE.md. Added
#                                     12 Sep 2026; docs/DECISIONS-MERGE.md M2.1.
BRAND_ALLOWED="(what-a-decile-actually-measures|WhatADecileMeasures|Decile's product vocabulary|Decile bucketing)"

brand_hits=0
while IFS= read -r line; do
  [ -n "$line" ] || continue
  printf '%s' "$line" | grep -qE "$BRAND_ALLOWED" && continue
  printf '%s\n' "$line"
  brand_hits=$((brand_hits + 1))
done < <(git grep -I -n -E 'Decile' -- "${EXCLUDES[@]}" 2>/dev/null)

if [ "$brand_hits" -ne 0 ]; then
  echo
  echo "FAIL: $brand_hits occurrence(s) of the old brand name survived."
  echo "The product is Baskfy (CLAUDE.md D1, docs/14). If a hit is genuinely about the"
  echo "STATISTIC rather than the name, add it to BRAND_ALLOWED with a reason and record it"
  echo "in docs/DECISIONS-MERGE.md."
  exit 1
fi

echo "OK: no namespace tokens outside the named exceptions (docs/, the two instruction"
echo "    documents, decile_1..6, DECILE_RANK_KEY, decile_bucket),"
echo "    and no occurrence of the old brand name outside the blog post about deciles."
